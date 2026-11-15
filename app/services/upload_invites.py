"""Inviting creators whose video never uploaded to post it again (Admin >
Failed uploads). Nothing is sent until an admin presses Send.

Who: creators with a post marked failed by the stuck-upload rescue
(app/services/stuck_uploads.py: upload_check.state "failed") in the last
DAYS days, who have not had a post go live since that failure, whose account
is active (not banned or deleted), and who were never invited before (one
invite per person, ever: upload_invites). The push opens their profile,
which every app version understands, where the + button posts.

The video itself can't be recovered: it never reached Cloudflare. The
original is usually still in the creator's gallery.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException

logger = logging.getLogger(__name__)

DAYS = 60
TITLE = "Your video didn't finish uploading"
MESSAGE = "We've fixed what stopped it. Post it again, it's still in your gallery."
SEND_BATCH = 100


async def _failed_by_creator(db, now: datetime, days: int) -> Dict[ObjectId, dict]:
    since = now - timedelta(days=days)
    out: Dict[ObjectId, dict] = {}
    async for v in db.videos.find({"upload_check.state": "failed", "created_at": {"$gte": since},
                                   "creator_id": {"$type": "objectId"}},
                                  {"creator_id": 1, "created_at": 1}):
        c = out.setdefault(v["creator_id"], {"posts": 0, "last": v.get("created_at")})
        c["posts"] += 1
        if v.get("created_at") and (c["last"] is None or v["created_at"] > c["last"]):
            c["last"] = v["created_at"]
    return out


async def eligible(db, now: Optional[datetime] = None, days: int = DAYS) -> dict:
    """Who would be invited now, and why the others won't be."""
    now = now or datetime.utcnow()
    failed = await _failed_by_creator(db, now, days)
    posts = sum(c["posts"] for c in failed.values())
    ids = list(failed)
    invited = {r["_id"] async for r in db.upload_invites.find({"_id": {"$in": ids}}, {"_id": 1})} if ids else set()
    people = {}
    if ids:
        async for u in db.users.find({"_id": {"$in": ids}},
                                     {"is_active": 1, "is_banned": 1, "deleted_at": 1, "expo_push_tokens": 1}):
            people[u["_id"]] = u
    posted_since, inactive, ready = 0, 0, []
    for cid, c in failed.items():
        if cid in invited:
            continue
        u = people.get(cid)
        if not u or u.get("is_active") is False or u.get("is_banned") is True or u.get("deleted_at"):
            inactive += 1
            continue
        live = await db.videos.find_one({"creator_id": cid, "isReadyToStream": True, "is_active": True,
                                         "created_at": {"$gt": c["last"]}}, {"_id": 1})
        if live:
            posted_since += 1
            continue
        ready.append((cid, [t for t in (u.get("expo_push_tokens") or []) if isinstance(t, str) and t]))
    return {"days": days, "failed_posts": posts, "creators": len(failed), "posted_since": posted_since,
            "already_invited": len(invited), "inactive": inactive, "to_invite": len(ready),
            "reachable_by_push": sum(1 for _, t in ready if t), "title": TITLE, "message": MESSAGE,
            "_ready": ready}


def public(summary: dict) -> dict:
    return {k: v for k, v in summary.items() if not k.startswith("_")}


async def send(db, admin: str, now: Optional[datetime] = None) -> dict:
    """Invite everyone eligible, once each. Marks each person before their push
    goes out, so pressing Send twice (or two admins at once) never sends twice."""
    from pymongo.errors import DuplicateKeyError
    from app.services.expo import send_push_batch_data
    now = now or datetime.utcnow()
    s = await eligible(db, now)
    if not s["_ready"]:
        raise HTTPException(status_code=409, detail="Nobody to invite right now")
    messages: List[dict] = []
    invited = 0
    for cid, tokens in s["_ready"]:
        try:
            await db.upload_invites.insert_one({"_id": cid, "at": now, "by": ObjectId(admin), "pushed": bool(tokens)})
        except DuplicateKeyError:
            continue
        invited += 1
        for t in tokens[:5]:
            messages.append({"to": t, "title": TITLE, "body": MESSAGE, "sound": "default",
                             "data": {"screen": "profile_v2", "type": "upload_invite"}})
    sent_batches = 0
    for i in range(0, len(messages), SEND_BATCH):
        try:
            await asyncio.to_thread(send_push_batch_data, messages[i:i + SEND_BATCH])
            sent_batches += 1
        except Exception:
            logger.exception("Upload invite push batch failed")
    return {"invited": invited, "pushes": len(messages), "batches_sent": sent_batches}
