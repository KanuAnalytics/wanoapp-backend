"""Activity read state and combined badges; no historical notification flood.

Activity starts counting at the first badge-aware request for an account.
Read acknowledgements use explicit delivered IDs, so arrivals during a read
cannot be accidentally cleared. Message read pointers stay independent.
"""
import asyncio
from datetime import datetime
from bson import ObjectId
from app.core.database import get_database

TYPES = ["follow", "like", "comment", "comment_reply", "video_ready"]


async def baseline(db, uid):
    oid = ObjectId(uid)
    user = await db.users.find_one({"_id": oid}, {"notification_badges_since": 1}) or {}
    if isinstance(user.get("notification_badges_since"), datetime):
        return user["notification_badges_since"]
    await db.users.update_one({"_id": oid, "notification_badges_since": {"$exists": False}},
                              {"$set": {"notification_badges_since": datetime.utcnow()}})
    user = await db.users.find_one({"_id": oid}, {"notification_badges_since": 1}) or {}
    return user.get("notification_badges_since", datetime.utcnow())


async def summary(db, uid, dm=None):
    since = await baseline(db, uid)
    activity = await db.notifications.count_documents({"recipient_id": ObjectId(uid), "type": {"$in": TYPES},
        "date": {"$gte": since}, "read_at": {"$exists": False}})
    from app.services import creator_push, messages
    if dm is None:
        dm = await messages.unread(db, uid) if await creator_push.on(db, "messages") else {"unread": 0, "requests": 0}
    unread, requests = max(0, int(dm["unread"])), max(0, int(dm["requests"]))
    return {"activity": activity, "messages": unread, "requests": requests, "total": activity + unread + requests}


async def read(db, uid, ids):
    await baseline(db, uid)
    await db.notifications.update_many({"recipient_id": ObjectId(uid), "_id": {"$in": [ObjectId(i) for i in ids]},
        "read_at": {"$exists": False}}, {"$set": {"read_at": datetime.utcnow()}})
    return await summary(db, uid)


async def send_user_push(uid, token, body, data=None, title=None, image_url=None):
    """Recheck ownership when the queued task runs, then attach a real count.
    A failed badge count must not prevent the underlying notification delivery.
    """
    db = get_database()
    user = await db.users.find_one({"_id": ObjectId(uid), "is_active": True, "expo_push_tokens": token}, {"badge_capable_tokens": 1})
    if not user:
        return
    try:
        badge = min(9999, (await summary(db, str(uid)))["total"]) if token in user.get("badge_capable_tokens", []) else None
    except Exception:
        badge = None
    from app.services import expo
    await asyncio.to_thread(expo.send_push_message, token, body, data, title, image_url, badge=badge)
