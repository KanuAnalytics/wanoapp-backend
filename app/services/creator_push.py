"""Pushing creators to post more. Each part has its own switch.

- picks ("picks"): Wano Picks, posts an admin features on Discover for
  PICK_DAYS. The creator gets a "wano_pick" badge (and its push) once per pick.
- road: the "Road to Wano TV" ladder shown on Achievements: Rising tier ->
  a Home Ground crown -> a Wano Pick -> featured on Wano TV (a "wano_tv"
  badge an admin gives).
- missions ("missions"): weekly tasks (Monday 00:00 UTC weeks). A finished
  mission is claimed once for points that can be spent on boosts. They are
  ledger kind "mission", never "earned", so they never count toward the cash
  pool: posting junk to finish missions can't earn money.
- stats ("creator_stats"): your views, likes, comments and shares per day over
  7 or 28 days (creator_daily: views once per viewer, own views never count),
  your best posts in that time and the hours your posts do best.
- trial posts ("trial_posts"): a post marked trial isn't announced to
  followers and isn't on the profile for others, so creators can experiment.
  It goes out to followers when they choose (or stays a normal post that was
  just never announced)."""

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException, status

PICK_DAYS = 7
MAX_PICKS = 20

MISSIONS = [
    # key, title, target, points
    ("post_3", "Post 3 times this week", 3, 30),
    ("reply_1", "Reply to a post with a video", 1, 20),
    ("challenge_1", "Join a challenge", 1, 20),
    ("comment_5", "Comment on 5 other creators' posts", 5, 10),
    ("home_1", "Post for your Home Ground", 1, 10),
]
MISSION = {k: (t, n, p) for k, t, n, p in MISSIONS}

VIDEO_IN_LINK = re.compile(r"(?:videoId=|/v/|/embed/)([0-9a-fA-F]{24})")


async def on(db, key: str) -> bool:
    from app.services import features
    return (await features.current(db)).get(key) is True


async def require(db, key: str):
    if not await on(db, key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _visible() -> dict:
    from app.services.creators import _showable
    return {**_showable(), "moderation.state": {"$nin": ["held", "removed"]}}


def _week_start(now: Optional[datetime] = None) -> datetime:
    from app.services import leaderboard
    return leaderboard.week_start(now or datetime.now(timezone.utc))


# ---------------------------------------------------------------- picks

def video_id_from(text: str) -> Optional[ObjectId]:
    text = (text or "").strip()
    if ObjectId.is_valid(text) and len(text) == 24:
        return ObjectId(text)
    m = VIDEO_IN_LINK.search(text)
    return ObjectId(m.group(1)) if m else None


async def add_pick(db, admin: str, video_text: str, note: str) -> dict:
    from app.services import leaderboard
    vid = video_id_from(video_text)
    if not vid:
        raise HTTPException(status_code=422, detail="Paste a post link or post ID")
    v = await db.videos.find_one({"_id": vid, **_visible()}, {"creator_id": 1})
    if not v:
        raise HTTPException(status_code=404, detail="That post isn't public (or doesn't exist)")
    creator = await db.users.find_one({"_id": v.get("creator_id"), "is_active": True}, {"_id": 1})
    if not creator:
        raise HTTPException(status_code=404, detail="That creator's account isn't active")
    now = datetime.utcnow()
    if await db.picks.find_one({"video_id": vid, "active": True, "until": {"$gt": now}}):
        raise HTTPException(status_code=409, detail="That post is already a Wano Pick")
    doc = {"video_id": vid, "creator_id": creator["_id"], "note": (note or "").strip()[:140],
           "at": now, "until": now + timedelta(days=PICK_DAYS), "active": True, "by": ObjectId(admin)}
    r = await db.picks.insert_one(doc)
    await leaderboard._award(db, creator["_id"], "wano_pick", now, week=str(r.inserted_id), video_id=vid)
    return {"id": str(r.inserted_id)}


async def remove_pick(db, pick_id: str) -> None:
    if not ObjectId.is_valid(pick_id):
        raise HTTPException(status_code=404, detail="Not found")
    r = await db.picks.update_one({"_id": ObjectId(pick_id), "active": True}, {"$set": {"active": False}})
    if not r.matched_count:
        raise HTTPException(status_code=404, detail="Not found")


async def current_picks(db, admin: bool = False) -> List[dict]:
    from app.services import thumbnails
    now = datetime.utcnow()
    rows = await db.picks.find({"active": True, "until": {"$gt": now}}).sort("at", -1).limit(MAX_PICKS * 2).to_list(
        MAX_PICKS * 2)
    if not rows:
        return []
    vids = {v["_id"]: v async for v in db.videos.find({"_id": {"$in": [r["video_id"] for r in rows]}, **_visible()})}
    users = {u["_id"]: u async for u in db.users.find(
        {"_id": {"$in": [r["creator_id"] for r in rows]}, "is_active": True},
        {"username": 1, "display_name": 1, "profile_picture": 1})}
    out = []
    for r in rows:
        v, u = vids.get(r["video_id"]), users.get(r["creator_id"])
        if not v or not u:
            continue
        out.append({"id": str(r["_id"]), "video_id": str(v["_id"]), "note": r.get("note") or "",
                    "thumbnail": thumbnails.best_thumbnail(v), "views_count": v.get("views_count", 0),
                    "title": v.get("title") if isinstance(v.get("title"), str) else None,
                    "until": r["until"],
                    "creator": {"id": str(u["_id"]), "username": u.get("username"),
                                "display_name": u.get("display_name"), "profile_picture": u.get("profile_picture")}})
        if len(out) >= MAX_PICKS:
            break
    return out


async def give_wano_tv(db, admin: str, creator_text: str) -> dict:
    from app.api.v1.admin import _find_creator
    from app.services import leaderboard
    u = await _find_creator(db, creator_text)
    if not u:
        raise HTTPException(status_code=404, detail="No account found")
    ok = await leaderboard._award(db, u["_id"], "wano_tv", datetime.utcnow())
    return {"user_id": str(u["_id"]), "awarded": ok}


async def road(db, me: ObjectId) -> List[dict]:
    kinds = set(await db.user_badges.distinct("kind", {"user_id": me}))
    u = await db.users.find_one({"_id": me}, {"tier": 1}) or {}
    return [
        {"key": "rising", "label": "Rising creator", "hint": "Reach 1K real followers",
         "done": u.get("tier") in ("rising", "star", "gold", "legend")},
        {"key": "crowned", "label": "Crowned", "hint": "Be the week's top creator in your city or country",
         "done": bool(kinds & {"crown_city", "crown_country"})},
        {"key": "picked", "label": "Wano Pick", "hint": "Get a post picked by the Wano team",
         "done": "wano_pick" in kinds},
        {"key": "wano_tv", "label": "On Wano TV", "hint": "Featured on Wano TV", "done": "wano_tv" in kinds},
    ]


# ---------------------------------------------------------------- missions

async def _live_challenge_tags(db, start: datetime, end: datetime) -> List[str]:
    return [c["hashtag"] async for c in db.challenges.find(
        {"starts_at": {"$lt": end}, "ends_at": {"$gt": start}}, {"hashtag": 1}) if isinstance(c.get("hashtag"), str)]


async def mission_progress(db, me: ObjectId, now: Optional[datetime] = None) -> List[dict]:
    from app.services import home_ground
    start = _week_start(now)
    end = start + timedelta(days=7)
    posts = await db.videos.find({"creator_id": me, **_visible(), "created_at": {"$gte": start, "$lt": end}},
                                 {"reply_to": 1, "hashtags": 1}).to_list(500)
    tags = {t.casefold().lstrip("#") for t in await _live_challenge_tags(db, start, end)}
    in_challenge = sum(1 for p in posts if any(isinstance(h, str) and h.casefold().lstrip("#") in tags
                                               for h in (p.get("hashtags") or [])))
    commented = await db.comments.distinct("video_id", {"user_id": me, "created_at": {"$gte": start, "$lt": end}})
    others = await db.videos.count_documents({"_id": {"$in": commented[:200]}, "creator_id": {"$ne": me}}) \
        if commented else 0
    u = await db.users.find_one({"_id": me}, {"home_ground": 1}) or {}
    home_on = await on(db, "home_ground")
    have = {"post_3": len(posts), "reply_1": sum(1 for p in posts if isinstance(p.get("reply_to"), dict)),
            "challenge_1": in_challenge, "comment_5": others,
            "home_1": len(posts) if home_ground.city_of(u) else 0}
    week = start.strftime("%Y-%m-%d")
    claimed = set(await db.points_ledger.distinct("mission", {"user_id": me, "kind": "mission", "week": week}))
    out = []
    for key, title, target, pts in MISSIONS:
        if key == "home_1" and not home_on:
            continue
        out.append({"key": key, "title": title, "target": target, "points": pts,
                    "progress": min(have[key], target), "done": have[key] >= target, "claimed": key in claimed})
    return out


async def claim(db, me: ObjectId, key: str) -> dict:
    from app.services import points
    if key not in MISSION:
        raise HTTPException(status_code=404, detail="No such mission")
    rows = {m["key"]: m for m in await mission_progress(db, me)}
    m = rows.get(key)
    if not m:
        raise HTTPException(status_code=404, detail="No such mission")
    if not m["done"]:
        raise HTTPException(status_code=400, detail="Finish the mission first")
    week = _week_start().strftime("%Y-%m-%d")
    now = datetime.utcnow()
    if not await points._ledger(db, f"mission:{me}:{week}:{key}", me, "mission", m["points"], now,
                                mission=key, week=week):
        raise HTTPException(status_code=409, detail="Already claimed")
    await points._wallet(db, me, m["points"])
    return {"points": m["points"]}


# ---------------------------------------------------------------- stats

async def stats(db, me: ObjectId, days: int) -> dict:
    from app.services import creators, growth_notifications as g
    today = creators._day(datetime.now(timezone.utc))
    start = today - timedelta(days=days - 1)
    by_day: Dict[datetime, dict] = {}
    async for r in db.creator_daily.find({"creator_id": me, "day": {"$gte": start, "$lte": today}}):
        d = r.get("day")
        if not isinstance(d, datetime):
            continue
        row = by_day.setdefault(d, {"views": 0, "likes": 0, "comments": 0, "shares": 0})
        for k in row:
            v = r.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf"):
                row[k] += max(0, int(v))
    series, totals = [], {"views": 0, "likes": 0, "comments": 0, "shares": 0}
    for i in range(days):
        d = start + timedelta(days=i)
        row = by_day.get(d, {"views": 0, "likes": 0, "comments": 0, "shares": 0})
        series.append({"day": d.strftime("%Y-%m-%d"), **row})
        for k in totals:
            totals[k] += row[k]
    posts = await db.videos.find({"creator_id": me, **_visible(), "created_at": {"$gte": start}},
                                 {"title": 1, "description": 1, "views_count": 1, "likes_count": 1,
                                  "created_at": 1, "urls": 1, "thumbnail": 1, "remoteUrl_CF": 1}
                                 ).sort("views_count", -1).limit(3).to_list(3)
    from app.services import thumbnails
    best = [{"id": str(p["_id"]), "title": (p.get("title") or p.get("description") or "")[:80]
             if isinstance(p.get("title") or p.get("description") or "", str) else "",
             "views": int(p.get("views_count") or 0) if isinstance(p.get("views_count"), (int, float)) else 0,
             "thumbnail": thumbnails.best_thumbnail(p)} for p in posts]
    # When your posts do best: average views by the local hour they went up (last 28 days).
    user = await db.users.find_one({"_id": me}, {"localization": 1}) or {}
    offset = g._offset(user)
    hours: Dict[int, list] = {}
    async for p in db.videos.find({"creator_id": me, **_visible(),
                                   "created_at": {"$gte": today - timedelta(days=27)}},
                                  {"created_at": 1, "views_count": 1}):
        c, v = p.get("created_at"), p.get("views_count")
        if isinstance(c, datetime) and isinstance(v, (int, float)) and not isinstance(v, bool):
            hours.setdefault(((c.replace(tzinfo=None) + offset).hour // 3) * 3, []).append(max(0, v))
    best_hours = None
    if sum(len(x) for x in hours.values()) >= 5:
        h = max(hours, key=lambda k: (sum(hours[k]) / len(hours[k]), len(hours[k])))
        best_hours = {"from": h, "to": (h + 3) % 24}
    posts_month = await _posts_month(db, me, offset)
    return {"days": days, "totals": totals, "series": series, "best_posts": best, "best_hours": best_hours,
            "posts_month": posts_month,
            "followers": int((await db.users.find_one({"_id": me}, {"followers_count": 1}) or {})
                             .get("followers_count") or 0)}


async def _posts_month(db, me: ObjectId, offset: timedelta) -> dict:
    """Posts you put up this calendar month (your local time), on how many
    different days, and last month's count to compare. Only posts people can
    see count: private, removed, held and still-processing ones don't."""
    local_now = datetime.now(timezone.utc).replace(tzinfo=None) + offset
    this_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_start = (this_start - timedelta(days=1)).replace(day=1)
    # Back to UTC for the query (created_at is stored in UTC).
    q_from = last_start - offset
    count = last = 0
    days = set()
    async for p in db.videos.find({"creator_id": me, **_visible(), "created_at": {"$gte": q_from}},
                                  {"created_at": 1}):
        c = p.get("created_at")
        if not isinstance(c, datetime):
            continue
        local = c.replace(tzinfo=None) + offset
        if local >= this_start:
            if local <= local_now + timedelta(minutes=5):
                count += 1
                days.add(local.date())
        elif local >= last_start:
            last += 1
    return {"month": this_start.strftime("%Y-%m"), "count": count, "days_posted": len(days),
            "last_month": last}


# ---------------------------------------------------------------- trial posts

async def share_trial(db, me: ObjectId, video_id: str) -> dict:
    if not ObjectId.is_valid(video_id):
        raise HTTPException(status_code=404, detail="Post not found")
    now = datetime.utcnow()
    r = await db.videos.update_one({"_id": ObjectId(video_id), "creator_id": me, "trial": True},
                                   {"$set": {"trial": False, "trial_shared_at": now}})
    if not r.matched_count:
        raise HTTPException(status_code=404, detail="Post not found")
    return {"shared": True}


async def my_trials(db, me: ObjectId) -> List[dict]:
    from app.services import thumbnails
    rows = await db.videos.find({"creator_id": me, "trial": True, "is_active": True}).sort("created_at", -1) \
        .limit(30).to_list(30)
    return [{"id": str(v["_id"]), "title": (v.get("title") or v.get("description") or "")[:80]
             if isinstance(v.get("title") or v.get("description") or "", str) else "",
             "thumbnail": thumbnails.best_thumbnail(v),
             "views_count": v.get("views_count", 0) if isinstance(v.get("views_count"), int) else 0,
             "created_at": v.get("created_at")} for v in rows]
