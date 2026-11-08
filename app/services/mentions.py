"""@mentions (behind the "mentions" switch): name suggestions while typing
"@", resolving a tapped @name to a profile, and one push to each person
mentioned in a post or comment.

Rules for the push: at most MAX_PER_TEXT people per post/comment, never
yourself, never across a block, only active accounts, once per person per
post/comment, at most DAILY_PER_PERSON mention pushes a day per person, and
not for trial or scheduled posts (they aren't out yet). People can turn them
off (notification_prefs.mentions = false)."""

import asyncio
import logging
import re
from datetime import datetime
from typing import List, Optional

from bson import ObjectId

logger = logging.getLogger(__name__)

MENTION = re.compile(r"(?:^|(?<=\s))@([A-Za-z0-9_.]{3,30})")
USERNAME = re.compile(r"^[A-Za-z0-9_.]{1,30}$")
MAX_PER_TEXT = 5
DAILY_PER_PERSON = 10
SUGGEST_MAX = 8

_tasks = set()


async def enabled(db) -> bool:
    from app.services import features
    return (await features.current(db)).get("mentions") is True


def names_in(text) -> List[str]:
    if not isinstance(text, str):
        return []
    out, seen = [], set()
    for m in MENTION.finditer(text):
        n = m.group(1).rstrip(".")
        if len(n) >= 3 and n.casefold() not in seen:
            seen.add(n.casefold())
            out.append(n)
    return out


def _blocked(a: dict, b_id: ObjectId) -> bool:
    for k in ("blocked_users", "blocked_by"):
        for x in a.get(k) or []:
            if x == b_id or (isinstance(x, str) and x == str(b_id)):
                return True
    return False


async def by_username(db, name: str, projection: Optional[dict] = None) -> Optional[dict]:
    if not isinstance(name, str) or not USERNAME.match(name):
        return None
    proj = projection or {"username": 1}
    q = {"is_active": True}
    return (await db.users.find_one({"username": name, **q}, proj)
            or await db.users.find_one({"username": {"$regex": f"^{re.escape(name)}$", "$options": "i"}, **q}, proj))


async def suggest(db, q: str, viewer: Optional[str]) -> List[dict]:
    q = (q or "").strip().lstrip("@")
    q = "".join(c for c in q if c.isalnum() or c in "_. ")[:30]
    me = None
    if viewer and ObjectId.is_valid(str(viewer)):
        me = await db.users.find_one({"_id": ObjectId(str(viewer))},
                                     {"following": 1, "blocked_users": 1, "blocked_by": 1, "home_ground": 1})
    following = [f for f in (me or {}).get("following") or [] if isinstance(f, ObjectId)]
    hidden = set()
    for k in ("blocked_users", "blocked_by"):
        for x in (me or {}).get(k) or []:
            if isinstance(x, ObjectId):
                hidden.add(x)
            elif isinstance(x, str) and ObjectId.is_valid(x):
                hidden.add(ObjectId(x))
    if me:
        hidden.add(me["_id"])
    proj = {"username": 1, "display_name": 1, "profile_picture": 1, "tier": 1, "followers_count": 1}
    base = {"is_active": True, "_id": {"$nin": list(hidden)}}
    if q:
        rx = {"$regex": f"^{re.escape(q)}", "$options": "i"}
        base["$or"] = [{"username": rx}, {"display_name": rx}]
    rows: List[dict] = []
    seen = set()
    if following:
        async for u in db.users.find({**base, "_id": {"$in": following[:5000], "$nin": list(hidden)}}, proj) \
                .sort("followers_count", -1).limit(SUGGEST_MAX):
            rows.append(u)
            seen.add(u["_id"])
    # Then people from your Home Ground city: local creators before famous strangers.
    city = (me or {}).get("home_ground", {}).get("city") if isinstance((me or {}).get("home_ground"), dict) else None
    if len(rows) < SUGGEST_MAX and q and isinstance(city, str):
        async for u in db.users.find({**base, "home_ground.city": city, "_id": {"$nin": list(hidden | seen)}}, proj) \
                .sort("followers_count", -1).limit(SUGGEST_MAX - len(rows)):
            rows.append(u)
            seen.add(u["_id"])
    if len(rows) < SUGGEST_MAX and q:
        async for u in db.users.find({**base, "_id": {"$nin": list(hidden | seen)}}, proj) \
                .sort("followers_count", -1).limit(SUGGEST_MAX - len(rows)):
            rows.append(u)
    return [{"id": str(u["_id"]), "username": u.get("username"), "display_name": u.get("display_name"),
             "profile_picture": u.get("profile_picture"),
             "tier": u.get("tier") if u.get("tier") in ("rising", "star", "gold", "legend") else None,
             "following": u["_id"] in set(following)}
            for u in rows if isinstance(u.get("username"), str)]


def notify_later(db, author: str, text, video_id, comment_id=None) -> None:
    """Fire-and-forget: posting never waits for (or fails because of) pushes."""
    try:
        t = asyncio.get_running_loop().create_task(notify(db, author, text, video_id, comment_id))
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
    except RuntimeError:
        pass


async def notify(db, author: str, text, video_id, comment_id=None) -> int:
    from app.services import growth_notifications as g
    try:
        if not await enabled(db):
            return 0
        names = names_in(text)[:MAX_PER_TEXT]
        if not names or not ObjectId.is_valid(str(author)):
            return 0
        me = await db.users.find_one({"_id": ObjectId(str(author)), "is_active": True},
                                     {"username": 1, "display_name": 1})
        if not me:
            return 0
        who = me.get("display_name") if isinstance(me.get("display_name"), str) and me["display_name"].strip() \
            else f"@{me.get('username')}"
        now = datetime.utcnow()
        sent = 0
        for n in names:
            u = await by_username(db, n, {"expo_push_tokens": 1, "notification_prefs": 1, "blocked_users": 1,
                                          "blocked_by": 1, "localization": 1})
            if not u or u["_id"] == me["_id"] or _blocked(u, me["_id"]):
                continue
            prefs = u.get("notification_prefs") if isinstance(u.get("notification_prefs"), dict) else {}
            if prefs.get("mentions") is False:
                continue
            key = f"{comment_id or video_id}:{u['_id']}"
            try:
                await db.mention_log.insert_one({"_id": key, "at": now, "user_id": u["_id"]})
            except Exception:
                continue  # already told about this one
            if not await g._take_budget(db, u, now, "mentions", DAILY_PER_PERSON):
                continue
            body = "mentioned you in a comment" if comment_id else "mentioned you in a post"
            data = {"type": "mention", "video_id": str(video_id)}
            if comment_id:
                data["comment_id"] = str(comment_id)
            for t in g._tokens(u):
                await g._send_one(t, f"{who} {body}", data, "New mention")
            sent += 1
        return sent
    except Exception:
        logger.exception("mention notify failed")
        return 0
