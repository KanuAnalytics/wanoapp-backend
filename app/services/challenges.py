"""
Weekly challenges: a hashtag, a time window (max MAX_DAYS), an optional points
prize. Entries are public posts with the hashtag made inside the window; a
creator's best post is their entry. Score (same signals as the leaderboard):

    views + 2 x finished + 3 x likes + 5 x comments + 8 x shares

An hour after a challenge ends the top 3 get a challenge_top3 badge (announced
like other badges) and the prize points, once.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import List

from bson import ObjectId

from app.services.creators import _showable

MAX_DAYS = 14
TOP = 3
FINISH_DELAY = timedelta(hours=1)
SCAN = 5000
HASHTAG = re.compile(r"^#?([A-Za-z0-9_]{2,40})$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v > 0 else 0.0


def state(ch: dict, now: datetime) -> str:
    now = _naive(now)
    if now < ch["starts_at"]:
        return "upcoming"
    return "live" if now < ch["ends_at"] else "ended"


def public(ch: dict, now: datetime) -> dict:
    return {"id": str(ch["_id"]), "hashtag": ch["hashtag"], "title": ch["title"],
            "description": ch.get("description", ""), "starts_at": ch["starts_at"], "ends_at": ch["ends_at"],
            "prize_points": ch.get("prize_points", 0), "state": state(ch, now)}


async def ranking(db, ch: dict) -> List[dict]:
    q = {**_showable(), "moderation.state": {"$nin": ["held", "removed"]},
         "hashtags": {"$regex": f"^#?{re.escape(ch['hashtag'])}$", "$options": "i"},
         "created_at": {"$gte": ch["starts_at"], "$lt": ch["ends_at"]}}
    docs = await db.videos.find(q, {"creator_id": 1, "created_at": 1, "likes_count": 1, "comments_count": 1,
                                    "shares_count": 1, "urls": 1, "remoteUrl_CF": 1, "media_type": 1,
                                    "images": 1}).limit(SCAN).to_list(SCAN)
    if not docs:
        return []
    reach = {r["_id"]: r async for r in db.video_reach.find({"_id": {"$in": [d["_id"] for d in docs]}},
                                                            {"views": 1, "finished": 1})}
    users = {u["_id"]: u async for u in db.users.find(
        {"_id": {"$in": list({d.get("creator_id") for d in docs})}, "is_active": True},
        {"username": 1, "display_name": 1, "profile_picture": 1})}
    best = {}
    for d in docs:
        if d.get("creator_id") not in users:
            continue
        r = reach.get(d["_id"], {})
        score = (_num(r.get("views")) + 2 * _num(r.get("finished")) + 3 * _num(d.get("likes_count"))
                 + 5 * _num(d.get("comments_count")) + 8 * _num(d.get("shares_count")))
        key = (-score, d.get("created_at") or datetime.max, str(d["_id"]))
        cur = best.get(d["creator_id"])
        if cur is None or key < cur[0]:
            best[d["creator_id"]] = (key, d, score)
    from app.services import thumbnails
    rows = sorted(best.values(), key=lambda t: t[0])
    out = []
    for i, (_, d, score) in enumerate(rows, 1):
        u = users[d["creator_id"]]
        out.append({"rank": i, "video_id": str(d["_id"]), "score": int(round(score)),
                    "thumbnail": thumbnails.best_thumbnail(d),
                    "user": {"id": str(u["_id"]), "username": u.get("username"),
                             "display_name": u.get("display_name"), "profile_picture": u.get("profile_picture")}})
    return out


async def finish(db) -> int:
    """Award ended challenges (idempotent)."""
    from app.services import leaderboard, points
    now = _utcnow()
    done = 0
    async for ch in db.challenges.find({"ends_at": {"$lte": _naive(now) - FINISH_DELAY}, "finished": {"$ne": True}}):
        for row in (await ranking(db, ch))[:TOP]:
            uid = ObjectId(row["user"]["id"])
            if await leaderboard._award(db, uid, "challenge_top3", now, week=str(ch["_id"]), rank=row["rank"],
                                        challenge=ch["title"]):
                if ch.get("prize_points"):
                    await points.grant(db, uid, int(ch["prize_points"]), f"challenge {ch['_id']}")
        await db.challenges.update_one({"_id": ch["_id"]}, {"$set": {"finished": True}})
        done += 1
    return done
