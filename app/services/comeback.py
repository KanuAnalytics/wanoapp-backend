"""Comeback reminders ("comeback" switch, app 2.3.61).

Google Play showed only 6% of monthly users open Wano on a given day. This
sends one helpful push to people who have drifted away for a few days, never
a generic "we miss you":
- a live challenge they haven't seen ("#whereimfrom is live: show us home"),
  else
- new posts from people they follow since they were last here
  ("Amina and 2 others posted 7 new videos").
Nothing to say means nothing is sent.

Who can get one:
- only apps 2.3.61+ say when they were opened (POST /comeback/open, at most
  once a day), so older apps are never part of this;
- last opened between AWAY_MIN and AWAY_MAX ago, an active account with a
  push token, "new posts" notifications not turned off, never staff;
- in their local evening (SEND_FROM-SEND_TO), outside quiet hours;
- at most one every EVERY, and it uses the day's "new_posts" budget so it
  never adds to an already busy day.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId

logger = logging.getLogger(__name__)

AWAY_MIN = timedelta(days=3)
AWAY_MAX = timedelta(days=14)
EVERY = timedelta(days=7)
OPEN_WRITE_EVERY = timedelta(hours=6)
SEND_FROM, SEND_TO = 18, 21           # local hours [from, to)
BATCH = 500
MAX_PER_RUN = 5000


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def on(db) -> bool:
    from app.services import creator_push
    return await creator_push.on(db, "comeback")


async def opened(db, me: str, now: Optional[datetime] = None) -> dict:
    """The app was opened. Written at most every OPEN_WRITE_EVERY (cheap)."""
    now = now or _now()
    if not await on(db) or not ObjectId.is_valid(me):
        return {"ok": False}
    await db.users.update_one(
        {"_id": ObjectId(me), "$or": [{"last_open": {"$lt": now - OPEN_WRITE_EVERY}}, {"last_open": {"$exists": False}}]},
        {"$set": {"last_open": now}})
    return {"ok": True}


def _name(u: dict) -> str:
    for k in ("display_name", "username"):
        v = u.get(k)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:40]
    return "Someone"


async def _live_challenge(db, now: datetime) -> Optional[dict]:
    return await db.challenges.find_one({"starts_at": {"$lte": now}, "ends_at": {"$gt": now + timedelta(hours=12)}},
                                        sort=[("starts_at", -1)])


async def message_for(db, user: dict, now: datetime, challenge: Optional[dict]) -> Optional[dict]:
    """What to say to this person, or None (then nothing is sent)."""
    from app.services.growth_notifications import _clip, _visible_post, BODY_MAX, TITLE_MAX
    last = user.get("last_open")
    if challenge and (not isinstance(last, datetime) or last < challenge["starts_at"]):
        tag = challenge.get("hashtag") or ""
        title = _clip(f"#{tag} is live", TITLE_MAX)
        desc = challenge.get("description") or challenge.get("title") or "Join the challenge on Wano."
        return {"title": title, "body": _clip(desc, BODY_MAX),
                "data": {"type": "challenge", "challenge_id": str(challenge["_id"])}}
    following = [f for f in (user.get("following") or []) if isinstance(f, ObjectId)][:2000]
    if not following or not isinstance(last, datetime):
        return None
    rows = await db.videos.aggregate([
        {"$match": {**_visible_post(), "creator_id": {"$in": following}, "created_at": {"$gt": last},
                    "trial": {"$ne": True}}},
        {"$group": {"_id": "$creator_id", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
        {"$limit": 50},
    ]).to_list(length=50)
    rows = [r for r in rows if r["_id"] not in set(user.get("blocked_users") or []) | set(user.get("blocked_by") or [])]
    if not rows:
        return None
    total = sum(int(r["n"]) for r in rows)
    top = await db.users.find_one({"_id": rows[0]["_id"], "is_active": {"$ne": False}},
                                  {"display_name": 1, "username": 1})
    if not top:
        return None
    who = _name(top)
    others = len(rows) - 1
    videos = "1 new video" if total == 1 else f"{total} new videos"
    body = f"{who} posted {videos} since you were last here." if others == 0 else \
        f"{who} and {others} other{'s' if others > 1 else ''} you follow posted {videos} since you were last here."
    return {"title": "New on Wano", "body": _clip(body, BODY_MAX), "data": {"screen": "feed", "type": "comeback"}}


async def comeback_nudges(db, now: Optional[datetime] = None) -> dict:
    from app.services import growth_notifications as gn, staff
    out = {"checked": 0, "sent": 0}
    if not await on(db):
        return out
    now = now or _now()
    aware = now.replace(tzinfo=timezone.utc)
    challenge = await _live_challenge(db, now)
    q = {"last_open": {"$lte": now - AWAY_MIN, "$gte": now - AWAY_MAX}, "is_active": {"$ne": False},
         "expo_push_tokens.0": {"$exists": True},
         "$or": [{"comeback_at": {"$lt": now - EVERY}}, {"comeback_at": {"$exists": False}}]}
    proj = {"expo_push_tokens": 1, "localization": 1, "notification_prefs": 1, "last_open": 1, "following": 1,
            "blocked_users": 1, "blocked_by": 1, "user_type": 1, "staff_role": 1}
    cursor = db.users.find(q, proj).limit(MAX_PER_RUN)
    batch = []
    async for u in cursor:
        out["checked"] += 1
        if staff.is_staff(u) or not gn._prefs(u)["new_posts"] or not gn._tokens(u):
            continue
        h = gn._local(u, aware).hour
        if not (SEND_FROM <= h < SEND_TO) or gn._quiet(u, aware):
            continue
        batch.append(u)
        if len(batch) >= BATCH:
            out["sent"] += await _send(db, batch, now, aware, challenge)
            batch = []
    if batch:
        out["sent"] += await _send(db, batch, now, aware, challenge)
    return out


async def _send(db, users, now, aware, challenge) -> int:
    from app.services import growth_notifications as gn
    sent = 0
    for u in users:
        msg = await message_for(db, u, now, challenge)
        if not msg:
            continue
        # Claim first, so two servers or runs never send twice.
        r = await db.users.update_one({"_id": u["_id"], "$or": [{"comeback_at": {"$lt": now - EVERY}},
                                                                {"comeback_at": {"$exists": False}}]},
                                      {"$set": {"comeback_at": now}})
        if not r.modified_count:
            continue
        if not await gn._take_budget(db, u, aware, "new_posts", gn.NEW_POSTS_PER_DAY):
            continue
        messages = [{"to": t, "title": msg["title"], "body": msg["body"], "data": msg["data"], "sound": "default"}
                    for t in gn._tokens(u)]
        if messages and await gn._send_batch(messages):
            sent += 1
    return sent
