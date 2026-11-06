"""
Growth notifications, built so they never feel like spam.

Followers: new posts (notify_new_posts, every 2 min)
- A post that has gone live (public, playable, not held) in the last day
  triggers one alert to the creator's followers: "Amina just posted".
- Per creator: one follower alert per 24 h (their first post of the day).
- Per follower: at most NEW_POSTS_PER_DAY alerts a day, none in their quiet
  hours (QUIET_START-QUIET_END local time, from their country), none if either
  side blocked the other, none if they turned new_posts off.
- Each post is claimed before fanning out, so several servers or runs never
  send twice. A failed Expo batch is logged, not retried in a loop.
- Posts created before the server started (minus a few minutes) are skipped,
  so a deploy never blasts yesterday's posts.

Creators: weekly / monthly recap (posting_recaps, every 30 min): Monday 9-12 local,
"Last week you posted 5 videos, 2 more than the week before, and got 1,240 views";
a nudge after a quiet week that followed an active one; on the 1st, last month's
posts. Once each per creator, off with creator_tips.

Creators: tips (at most CREATOR_TIPS_PER_DAY a day, none in quiet hours, off with creator_tips)
- First-viewer result: when a post finishes its first-viewer test
  (app/services/creators.py): "Your new video's first 30 viewers watched 78%".
- Streak reminder (streak_reminders, every 15 min): creators with a 2+ day
  streak who haven't posted today, once, between STREAK_FROM-STREAK_TO local.

Push only: nothing is written to the in-app notifications list, so older app
builds that don't know these types aren't affected.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from bson import ObjectId

logger = logging.getLogger(__name__)

QUIET_START = 22
QUIET_END = 8
NEW_POSTS_PER_DAY = 5
CREATOR_TIPS_PER_DAY = 1
CREATOR_ALERT_EVERY = timedelta(hours=24)
POST_WINDOW = timedelta(hours=24)
START_GRACE = timedelta(minutes=10)
STREAK_FROM, STREAK_TO = 19, 22          # local hours [from, to)
STREAK_SCAN_DAYS = 3
FANOUT_BATCH = 500
EXPO_BATCH = 100
MAX_FANOUT = 100_000
TITLE_MAX = 65
BODY_MAX = 178

# Standard offset from UTC by country (hours). Unknown countries use UTC.
UTC_OFFSET = {
    "ug": 3, "ke": 3, "tz": 3, "et": 3, "so": 3, "dj": 3, "er": 3, "mg": 3, "km": 3,
    "rw": 2, "bi": 2, "ss": 2, "sd": 2, "za": 2, "zm": 2, "zw": 2, "mw": 2, "mz": 2, "bw": 2,
    "ls": 2, "sz": 2, "na": 2, "eg": 2, "ly": 2,
    "ng": 1, "cd": 1, "cm": 1, "ao": 1, "cg": 1, "ga": 1, "gq": 1, "cf": 1, "td": 1, "ne": 1,
    "bj": 1, "tn": 1, "dz": 1, "ma": 1, "fr": 1, "de": 1, "it": 1, "es": 1, "be": 1, "nl": 1,
    "gh": 0, "ci": 0, "sn": 0, "ml": 0, "bf": 0, "gn": 0, "sl": 0, "lr": 0, "tg": 0, "gm": 0,
    "gw": 0, "mr": 0, "gb": 0, "ie": 0, "pt": 0,
    "ae": 4, "sa": 3, "qa": 3, "in": 5.5, "cn": 8,
    "us": -6, "ca": -5, "jm": -5, "ht": -5, "br": -3, "tt": -4, "bb": -4,
}

_started_at: Optional[datetime] = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def set_started(at: Optional[datetime] = None) -> None:
    global _started_at
    _started_at = at or _utcnow()


def _naive(dt):
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _local(user: dict, now: datetime) -> datetime:
    loc = user.get("localization") if isinstance(user.get("localization"), dict) else {}
    country = loc.get("country") if isinstance(loc.get("country"), str) else ""
    return now + timedelta(hours=UTC_OFFSET.get(country.lower(), 0))


def _quiet(user: dict, now: datetime) -> bool:
    h = _local(user, now).hour
    return h >= QUIET_START or h < QUIET_END


def _prefs(user: dict) -> dict:
    p = user.get("notification_prefs") if isinstance(user.get("notification_prefs"), dict) else {}
    return {"new_posts": p.get("new_posts") is not False, "creator_tips": p.get("creator_tips") is not False}


def _tokens(user: dict) -> List[str]:
    t = user.get("expo_push_tokens")
    return [x for x in t if isinstance(x, str) and x] if isinstance(t, list) else []


def _blocked(follower: dict, creator: ObjectId) -> bool:
    for key in ("blocked_users", "blocked_by"):
        v = follower.get(key)
        if isinstance(v, list) and creator in v:
            return True
    return False


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _day_key(user: dict, now: datetime) -> str:
    return f"{user['_id']}:{_local(user, now).date().isoformat()}"


async def _take_budget(db, user: dict, now: datetime, field: str, limit: int) -> bool:
    """Atomically use one of today's (local day) sends of this kind."""
    key = _day_key(user, now)
    try:
        res = await db.notif_daily.update_one(
            {"_id": key, "$or": [{field: {"$lt": limit}}, {field: {"$exists": False}}]}, {"$inc": {field: 1}})
        if res.modified_count:
            return True
        await db.notif_daily.insert_one({"_id": key, "user_id": user["_id"], field: 1, "at": _naive(now)})
        return True
    except Exception:
        return False     # duplicate key: the day's doc exists and is at the limit


async def _send_batch(messages: List[dict]) -> bool:
    from app.services import expo
    try:
        await asyncio.to_thread(expo.send_push_batch_data, messages)
        return True
    except Exception:
        logger.exception("Expo batch of %d failed", len(messages))
        return False


async def _send_one(token: str, body: str, data: dict, title: str) -> None:
    from app.services import expo
    try:
        await asyncio.to_thread(expo.send_push_message, token, body, data, title, None)
    except Exception:
        logger.warning("Push did not send")


# ---------------------------------------------------------------- followers: new posts

def _visible_post() -> dict:
    return {"is_active": True, "privacy": "public", "isReadyToStream": {"$ne": False},
            "status": {"$nin": ["scheduled", "publishing", "cancelled"]},
            "moderation.state": {"$nin": ["held", "removed"]}}


async def notify_new_posts(db) -> dict:
    now = _utcnow()
    since = now - POST_WINDOW
    if _started_at is not None:
        since = max(since, _started_at - START_GRACE)
    out = {"posts": 0, "sent": 0}
    q = {**_visible_post(), "created_at": {"$gte": _naive(since), "$lte": _naive(now + timedelta(minutes=10))},
         "followers_notified_at": {"$exists": False}}
    posts = await db.videos.find(q, {"creator_id": 1, "description": 1, "title": 1}).sort(
        "created_at", 1).limit(200).to_list(200)
    for post in posts:
        # Claim the post: only one run fans it out.
        claimed = await db.videos.update_one({"_id": post["_id"], "followers_notified_at": {"$exists": False}},
                                             {"$set": {"followers_notified_at": _naive(now)}})
        if not claimed.modified_count:
            continue
        creator_id = post.get("creator_id")
        if not isinstance(creator_id, ObjectId):
            continue
        # One follower alert per creator per day.
        creator = await db.users.find_one_and_update(
            {"_id": creator_id, "is_active": {"$ne": False},
             "$or": [{"last_follower_alert_at": {"$exists": False}},
                     {"last_follower_alert_at": {"$lte": _naive(now - CREATOR_ALERT_EVERY)}}]},
            {"$set": {"last_follower_alert_at": _naive(now)}},
            projection={"followers": 1, "display_name": 1, "username": 1})
        if not creator:
            continue
        out["posts"] += 1
        out["sent"] += await _fan_out(db, post, creator, now)
    return out


async def _fan_out(db, post: dict, creator: dict, now: datetime) -> int:
    followers = [f for f in (creator.get("followers") or []) if isinstance(f, ObjectId)][:MAX_FANOUT]
    name = creator.get("display_name") if isinstance(creator.get("display_name"), str) else None
    name = name or (creator.get("username") if isinstance(creator.get("username"), str) else None) or "Someone you follow"
    title = _clip(f"{name} just posted", TITLE_MAX)
    text = post.get("description") if isinstance(post.get("description"), str) else ""
    text = text or (post.get("title") if isinstance(post.get("title"), str) else "") or "New video on Wano"
    body = _clip(text, BODY_MAX)
    data = {"video_id": str(post["_id"]), "type": "new_post"}
    sent = 0
    pending: List[dict] = []
    for i in range(0, len(followers), FANOUT_BATCH):
        batch = followers[i:i + FANOUT_BATCH]
        users = await db.users.find(
            {"_id": {"$in": batch}, "is_active": {"$ne": False}},
            {"expo_push_tokens": 1, "localization": 1, "notification_prefs": 1,
             "blocked_users": 1, "blocked_by": 1}).to_list(length=len(batch))
        users = [u for u in users if _tokens(u) and _prefs(u)["new_posts"] and not _quiet(u, now)
                 and not _blocked(u, creator["_id"])]
        if not users:
            continue
        # Daily caps, read for the whole batch then taken one by one.
        keys = {_day_key(u, now): u for u in users}
        full = {d["_id"] async for d in db.notif_daily.find(
            {"_id": {"$in": list(keys)}, "new_posts": {"$gte": NEW_POSTS_PER_DAY}}, {"_id": 1})}
        for key, u in keys.items():
            if key in full or not await _take_budget(db, u, now, "new_posts", NEW_POSTS_PER_DAY):
                continue
            for t in _tokens(u):
                pending.append({"to": t, "title": title, "body": body, "data": data, "sound": "default"})
            sent += 1
            while len(pending) >= EXPO_BATCH:
                await _send_batch(pending[:EXPO_BATCH])
                pending = pending[EXPO_BATCH:]
    if pending:
        await _send_batch(pending)
    return sent


# ---------------------------------------------------------------- creators: tips

async def _creator_tip(db, creator_id, title: str, body: str, data: dict, now: datetime) -> bool:
    if not isinstance(creator_id, ObjectId):
        return False
    user = await db.users.find_one({"_id": creator_id, "is_active": {"$ne": False}},
                                   {"expo_push_tokens": 1, "localization": 1, "notification_prefs": 1})
    if not user or not _tokens(user) or not _prefs(user)["creator_tips"] or _quiet(user, now):
        return False
    if not await _take_budget(db, user, now, "creator_tips", CREATOR_TIPS_PER_DAY):
        return False
    for t in _tokens(user):
        await _send_one(t, _clip(body, BODY_MAX), data, _clip(title, TITLE_MAX))
    return True


async def first_viewers_result(db, video_id: ObjectId, reach: dict, passed: bool, audience: int) -> None:
    """Called once when a post finishes its first-viewer test."""
    now = _utcnow()
    video = await db.videos.find_one({"_id": video_id}, {"creator_id": 1})
    if not video:
        return
    from app.services.creators import _num
    seen = _num(reach.get("fy_views")) or 1
    pct = round(100 * _num(reach.get("fy_portion_sum")) / seen)
    if passed:
        body = f"Your new video's first {audience} viewers watched {pct}% on average. It's being shown to more people 🚀"
    else:
        body = (f"Your new video's first {audience} viewers watched {pct}% on average. "
                "Tip: grab attention in the first 2 seconds.")
    await _creator_tip(db, video.get("creator_id"), "Your first viewers are in", body,
                       {"video_id": str(video_id), "type": "first_viewers", "screen": "creator_stats"}, now)


async def streak_reminders(db) -> dict:
    from app.services import creators
    now = _utcnow()
    out = {"checked": 0, "sent": 0}
    since = creators._day(now) - timedelta(days=STREAK_SCAN_DAYS)
    rows = await db.videos.aggregate([
        {"$match": {**_visible_post(), "created_at": {"$gte": since}}},
        {"$group": {"_id": "$creator_id"}},
    ]).to_list(length=200_000)
    ids = [r["_id"] for r in rows if isinstance(r.get("_id"), ObjectId)]
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        users = await db.users.find(
            {"_id": {"$in": chunk}, "is_active": {"$ne": False}, "expo_push_tokens.0": {"$exists": True}},
            {"expo_push_tokens": 1, "localization": 1, "notification_prefs": 1}).to_list(length=len(chunk))
        users = [u for u in users if STREAK_FROM <= _local(u, now).hour < STREAK_TO
                 and _prefs(u)["creator_tips"] and _tokens(u)]
        if not users:
            continue
        streak = await creators.streaks(db, [u["_id"] for u in users], now=now)
        for u in users:
            s = streak.get(u["_id"]) or {}
            out["checked"] += 1
            if s.get("posted_today") or (s.get("current") or 0) < 2:
                continue
            key = f"{_day_key(u, now)}:streak"
            try:
                await db.notif_daily.insert_one({"_id": key, "user_id": u["_id"], "at": _naive(now)})
            except Exception:
                continue            # already reminded today
            if not await _take_budget(db, u, now, "creator_tips", CREATOR_TIPS_PER_DAY):
                continue
            body = f"🔥 Your {s['current']}-day streak ends tonight. Post something to keep it going."
            for t in _tokens(u):
                await _send_one(t, body, {"type": "streak", "screen": "create"}, "Keep your streak")
            out["sent"] += 1
    return out


# ---------------------------------------------------------------- weekly / monthly posting recap

WEEKLY_HOURS = (9, 12)       # Monday, creator's local time [from, to)
MONTHLY_HOURS = (10, 13)     # 1st of the month
RECAP_LOOKBACK_DAYS = 70
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def _offset(user: dict) -> timedelta:
    return _local(user, datetime(2000, 1, 1)) - datetime(2000, 1, 1)


def counted_posts() -> dict:
    """Posts that count as "posted": live, public or unlisted, playable."""
    return {"is_active": True, "privacy": {"$in": ["public", "unlisted"]}, "isReadyToStream": {"$ne": False},
            "status": {"$nin": ["scheduled", "publishing", "cancelled"]}}


async def hourly_posts(db, creator_ids: List[ObjectId], start: datetime, end: datetime) -> Dict[ObjectId, Dict[datetime, int]]:
    """{creator: {utc hour: posts}} in one grouped query."""
    out: Dict[ObjectId, Dict[datetime, int]] = {c: {} for c in creator_ids}
    if not creator_ids:
        return out
    rows = await db.videos.aggregate([
        {"$match": {**counted_posts(), "creator_id": {"$in": creator_ids},
                    "created_at": {"$gte": _naive(start), "$lt": _naive(end)}}},
        {"$group": {"_id": {"c": "$creator_id", "h": {"$dateToString": {"format": "%Y-%m-%dT%H", "date": "$created_at"}}},
                    "n": {"$sum": 1}}},
    ]).to_list(length=None)
    for r in rows:
        key = r.get("_id") or {}
        c, h = key.get("c"), key.get("h")
        if c in out and isinstance(h, str):
            try:
                out[c][datetime.strptime(h, "%Y-%m-%dT%H")] = int(r.get("n") or 0)
            except ValueError:
                continue
    return out


def count_between(hours: Dict[datetime, int], offset: timedelta, start_local: datetime, end_local: datetime) -> int:
    """Posts whose local time (UTC hour + offset) falls in [start_local, end_local)."""
    return sum(n for h, n in hours.items() if start_local <= h + offset < end_local)


def week_start(local: datetime) -> datetime:
    d = datetime(local.year, local.month, local.day)
    return d - timedelta(days=d.weekday())


def month_start(local: datetime, back: int = 0) -> datetime:
    """First day of the month `back` months before local's (negative = ahead)."""
    y, m = local.year, local.month - back
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return datetime(y, m, 1)


async def _views_between(db, creator: ObjectId, start: datetime, end: datetime) -> int:
    from app.services.creators import _num
    total = 0.0
    async for row in db.creator_daily.find({"creator_id": creator, "day": {"$gte": start, "$lt": end}}, {"views": 1, "day": 1}):
        if isinstance(row.get("day"), datetime):
            total += _num(row.get("views"))
    return int(total)


def _fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n:,}"


def _videos(n: int) -> str:
    return f"{n} video" if n == 1 else f"{n} videos"


async def _once(db, user: dict, key: str, now: datetime) -> bool:
    try:
        await db.notif_daily.insert_one({"_id": f"{user['_id']}:{key}", "user_id": user["_id"], "at": _naive(now)})
        return True
    except Exception:
        return False


def _in_recap_window(local: datetime) -> bool:
    return (local.day == 1 and MONTHLY_HOURS[0] <= local.hour < MONTHLY_HOURS[1]) or \
        (local.weekday() == 0 and WEEKLY_HOURS[0] <= local.hour < WEEKLY_HOURS[1])


async def posting_recaps(db) -> dict:
    """Monday mornings: last week's posts (or a nudge after a quiet week).
    The 1st of the month: last month's posts. Creator's own time zone."""
    now = _utcnow()
    out = {"weekly": 0, "monthly": 0, "nudges": 0}
    # Most runs no country is in its Monday-morning or 1st-of-month window: skip the scan.
    if not any(_in_recap_window(now + timedelta(hours=h)) for h in set(UTC_OFFSET.values()) | {0}):
        return out
    since = now - timedelta(days=RECAP_LOOKBACK_DAYS)
    rows = await db.videos.aggregate([
        {"$match": {**counted_posts(), "created_at": {"$gte": _naive(since)}}},
        {"$group": {"_id": "$creator_id"}},
    ]).to_list(length=None)
    ids = [r["_id"] for r in rows if isinstance(r.get("_id"), ObjectId)]
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        users = await db.users.find(
            {"_id": {"$in": chunk}, "is_active": {"$ne": False}, "expo_push_tokens.0": {"$exists": True}},
            {"expo_push_tokens": 1, "localization": 1, "notification_prefs": 1}).to_list(length=len(chunk))
        due = []
        for u in users:
            if not _tokens(u) or not _prefs(u)["creator_tips"]:
                continue
            local = _local(u, now)
            monthly = local.day == 1 and MONTHLY_HOURS[0] <= local.hour < MONTHLY_HOURS[1]
            weekly = local.weekday() == 0 and WEEKLY_HOURS[0] <= local.hour < WEEKLY_HOURS[1]
            if monthly or weekly:
                due.append((u, local, monthly))
        if not due:
            continue
        hours = await hourly_posts(db, [u["_id"] for u, _, _ in due], since - timedelta(days=1), now + timedelta(hours=1))
        for u, local, monthly in due:
            off, h = _offset(u), hours.get(u["_id"], {})
            if monthly:
                start, end = month_start(local, 1), month_start(local)
                n = count_between(h, off, start, end)
                if n < 1 or not await _once(db, u, f"recap:m:{start:%Y-%m}", now):
                    continue
                name = MONTHS[start.month - 1]
                views = await _views_between(db, u["_id"], start, end)
                body = f"In {name} you posted {_videos(n)}" + (f" and got {_fmt(views)} views" if views else "") + " 🎉"
                for t in _tokens(u):
                    await _send_one(t, _clip(body, BODY_MAX), {"type": "monthly_recap", "screen": "creator_stats"},
                                    _clip(f"Your {name} on Wano", TITLE_MAX))
                out["monthly"] += 1
                continue
            this_week = week_start(local)
            start, end = this_week - timedelta(days=7), this_week
            n = count_between(h, off, start, end)
            before = count_between(h, off, start - timedelta(days=7), start)
            if n < 1 and before < 1:
                continue
            if not await _once(db, u, f"recap:w:{start:%G-W%V}", now):
                continue
            if n < 1:
                body = "You didn't post last week. Your followers are waiting — share something today 🎬"
                kind = "nudges"
            else:
                diff = n - before
                body = f"Last week you posted {_videos(n)}"
                if diff > 0:
                    body += f", {diff} more than the week before"
                elif diff < 0:
                    body += f", {-diff} fewer than the week before"
                views = await _views_between(db, u["_id"], start, end)
                if views:
                    body += f", and got {_fmt(views)} views"
                body += " 🎉"
                kind = "weekly"
            for t in _tokens(u):
                await _send_one(t, _clip(body, BODY_MAX), {"type": "weekly_recap", "screen": "creator_stats"},
                                "Your week on Wano")
            out[kind] += 1
    return out


# ---------------------------------------------------------------- loops

async def _award_badges(db):
    from app.services import leaderboard
    return await leaderboard.award_badges(db)


async def _settle_points(db):
    from app.services import points
    return await points.settle(db)


class GrowthLoop:
    def __init__(self):
        self._tasks: List[asyncio.Task] = []

    async def _every(self, seconds: int, fn, name: str):
        from app.core.database import get_database
        while True:
            try:
                await fn(get_database())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s failed", name)
            await asyncio.sleep(seconds)

    def start(self):
        set_started()
        self._tasks = [asyncio.create_task(self._every(120, notify_new_posts, "notify_new_posts")),
                       asyncio.create_task(self._every(900, streak_reminders, "streak_reminders")),
                       asyncio.create_task(self._every(1800, posting_recaps, "posting_recaps")),
                       asyncio.create_task(self._every(1800, _award_badges, "award_badges")),
                       asyncio.create_task(self._every(900, _settle_points, "settle_points"))]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        self._tasks = []


growth_loop = GrowthLoop()
