"""
Weekly creator leaderboard and badges.

Week: Monday 00:00 to Monday 00:00 UTC. A creator is on the board when they
posted at least one public post that week and earned something from viewers.
Score uses creator_daily (views once per viewer, own views and engagement never
count; see creators.py):

    views + 2 x finished + 3 x likes + 5 x comments + 8 x shares

Badges (user_badges, one document per badge, awarded once):
- weekly_top10: top 10 of the global board, awarded AWARD_DELAY after the week
  ends (late events settle first). Winners get one push, in their daytime, if
  they have creator tips on; it is retried until it goes out (up to 3 days).
- streak_7 / streak_30: posting streaks (creators.streaks).
"""

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from bson import ObjectId

from app.services import creators

logger = logging.getLogger(__name__)

WEIGHTS = {"views": 1, "finished": 2, "likes": 3, "comments": 5, "shares": 8}
TOP_BADGES = 10
AWARD_DELAY = timedelta(hours=1)
NOTIFY_WITHIN = timedelta(days=3)
STREAK_BADGES = (7, 30)
CACHE_SECONDS = 300
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_cache: dict = {}


def clear_cache():
    _cache.clear()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def week_start(now: datetime, back: int = 0) -> datetime:
    """Naive UTC Monday 00:00 of the week containing now, `back` weeks earlier."""
    d = creators._day(now)
    return d - timedelta(days=d.weekday() + 7 * back)


def _num(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return 0.0
    return max(0.0, float(v))


def _visible_post(start: datetime, end: datetime) -> dict:
    return {"is_active": True, "privacy": "public", "isReadyToStream": {"$ne": False},
            "status": {"$nin": ["scheduled", "publishing", "cancelled"]},
            "moderation.state": {"$nin": ["held", "removed"]},
            "created_at": {"$gte": start, "$lt": end}}


async def ranking(db, start: datetime, country: Optional[str] = None) -> List[dict]:
    """Full ranking for the week starting `start` (cached)."""
    key = (start, country)
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    end = start + timedelta(days=7)
    posts: Dict[ObjectId, int] = {}
    async for row in db.videos.aggregate([
        {"$match": _visible_post(start, end)},
        {"$group": {"_id": "$creator_id", "n": {"$sum": 1}}},
    ]):
        if isinstance(row["_id"], ObjectId):
            posts[row["_id"]] = row["n"]
    totals: Dict[ObjectId, dict] = {}
    if posts:
        async for row in db.creator_daily.find(
                {"creator_id": {"$in": list(posts)}, "day": {"$gte": start, "$lt": end}},
                {"creator_id": 1, **{k: 1 for k in WEIGHTS}}):
            t = totals.setdefault(row["creator_id"], {k: 0.0 for k in WEIGHTS})
            for k in WEIGHTS:
                t[k] += _num(row.get(k))
    scored = {c: t for c, t in totals.items() if sum(WEIGHTS[k] * t[k] for k in WEIGHTS) > 0}
    users = {}
    if scored:
        q = {"_id": {"$in": list(scored)}, "is_active": True}
        if country:
            q["localization.country"] = {"$in": [country.lower(), country.upper()]}
        users = {u["_id"]: u async for u in db.users.find(
            q, {"username": 1, "display_name": 1, "profile_picture": 1})}
    rows = []
    for c, u in users.items():
        t = scored[c]
        rows.append({
            "user": {"id": str(c), "username": u.get("username"), "display_name": u.get("display_name"),
                     "profile_picture": u.get("profile_picture")},
            "score": int(round(sum(WEIGHTS[k] * t[k] for k in WEIGHTS))),
            "views": int(t["views"]), "likes": int(t["likes"]), "comments": int(t["comments"]),
            "shares": int(t["shares"]), "posts": posts.get(c, 0),
        })
    rows.sort(key=lambda r: (-r["score"], -r["views"], r["user"]["id"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    _cache[key] = (time.monotonic() + CACHE_SECONDS, rows)
    return rows


# ---------------------------------------------------------------- badges

def label(badge: dict) -> str:
    kind = badge.get("kind")
    from app.services import milestones
    if milestones.is_milestone(kind):
        return milestones.label(kind)
    if kind == "weekly_top10":
        try:
            d = datetime.strptime(badge.get("week", ""), "%Y-%m-%d")
            when = f"week of {d.day} {MONTHS[d.month - 1]} {d.year}"
        except ValueError:
            when = "this week"
        return f"#{badge.get('rank')} on Wano, {when}"
    if kind == "challenge_top3":
        return f"#{badge.get('rank')} in {badge.get('challenge') or 'a Wano challenge'}"
    if kind and kind.startswith("streak_"):
        return f"{kind.split('_')[1]}-day posting streak"
    return str(kind)


async def _award(db, user_id: ObjectId, kind: str, now: datetime, **extra) -> bool:
    week = extra.get("week")
    _id = f"{user_id}:{kind}:{week}" if week else f"{user_id}:{kind}"
    try:
        await db.user_badges.insert_one({"_id": _id, "user_id": user_id, "kind": kind,
                                         "at": creators._naive(now), "notified": False, **extra})
        return True
    except Exception:
        return False


async def _tell(db, badge: dict, now: datetime) -> bool:
    """One push about a badge, in the creator's daytime. True when done (sent,
    or nothing to send: no tokens / tips off)."""
    from app.services import growth_notifications as g
    user = await db.users.find_one({"_id": badge["user_id"], "is_active": {"$ne": False}},
                                   {"expo_push_tokens": 1, "localization": 1, "notification_prefs": 1})
    if not user or not g._tokens(user) or not g._prefs(user)["creator_tips"]:
        return True
    if g._quiet(user, now):
        return False
    if not await g._take_budget(db, user, now, "badges", 1):
        return False
    from app.services import milestones
    if milestones.is_milestone(badge["kind"]):
        title, body = milestones.push_text(badge["kind"])
    elif badge["kind"] == "weekly_top10":
        title, body = "You made Wano's Top 10", f"You finished #{badge.get('rank')} last week. Keep posting!"
    else:
        title, body = "New badge", f"You earned a {label(badge)} badge. Keep it going!"
    for t in g._tokens(user):
        await g._send_one(t, body, {"type": "badge", "kind": badge["kind"]}, title)
    return True


async def award_badges(db) -> dict:
    now = _utcnow()
    out = {"weekly": 0, "streak": 0, "told": 0}
    # Last week's top 10, once the week has settled.
    this_week = week_start(now)
    if creators._naive(now) >= this_week + AWARD_DELAY:
        last = this_week - timedelta(days=7)
        tag = last.strftime("%Y-%m-%d")
        if not await db.leaderboard_runs.find_one({"_id": tag}):
            for row in (await ranking(db, last))[:TOP_BADGES]:
                if await _award(db, ObjectId(row["user"]["id"]), "weekly_top10", now, week=tag, rank=row["rank"],
                                score=row["score"]):
                    out["weekly"] += 1
            try:
                await db.leaderboard_runs.insert_one({"_id": tag, "at": creators._naive(now)})
            except Exception:
                pass
    # Streaks of anyone who posted in the last two days.
    since = creators._naive(now) - timedelta(days=2)
    recent = await db.videos.distinct("creator_id", {**_visible_post(since, creators._naive(now) + timedelta(hours=1))})
    recent = [c for c in recent if isinstance(c, ObjectId)]
    if recent:
        for c, s in (await creators.streaks(db, recent, now=now, max_days=max(STREAK_BADGES) + 1)).items():
            for n in STREAK_BADGES:
                if s.get("current", 0) >= n and await _award(db, c, f"streak_{n}", now):
                    out["streak"] += 1
    # Tell winners who haven't heard yet.
    pending = await db.user_badges.find(
        {"notified": False, "at": {"$gte": creators._naive(now) - NOTIFY_WITHIN}}, {"_id": 1}).to_list(None)
    for p in pending:
        # Claim it first so two servers running this job don't both push.
        b = await db.user_badges.find_one_and_update({"_id": p["_id"], "notified": False},
                                                    {"$set": {"notified": "sending"}})
        if not b:
            continue
        done = False
        try:
            done = await _tell(db, b, now)
        except Exception:
            logger.exception("Could not announce badge %s", b["_id"])
        await db.user_badges.update_one({"_id": b["_id"]}, {"$set": {"notified": bool(done)}})
        out["told"] += int(done)
    return out


async def badges_of(db, user_id: ObjectId, limit: int = 50) -> List[dict]:
    if not await db.users.find_one({"_id": user_id, "is_active": True}, {"_id": 1}):
        return []
    rows = await db.user_badges.find({"user_id": user_id}).sort([("at", -1), ("_id", 1)]).limit(limit).to_list(limit)
    return [{"kind": b["kind"], "label": label(b), "week": b.get("week"), "rank": b.get("rank"),
             "awarded_at": b.get("at")} for b in rows]
