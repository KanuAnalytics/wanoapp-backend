"""How many posts one account can make ("post_limits" switch).

Without a limit a script on one account could post thousands of videos: the
feed and followers get flooded, and every video sits on our Cloudflare bill.
The limits are far above what a busy creator posts (a creator posting a
series rarely passes 10 a day): POSTS_PER_HOUR and POSTS_PER_DAY, counting
ordinary, photo, scheduled and multi-clip posts together. Admins are exempt.
"""

from datetime import datetime, timedelta

from bson import ObjectId
from fastapi import HTTPException

POSTS_PER_HOUR = 30
POSTS_PER_DAY = 100


async def _made(db, oid: ObjectId, since: datetime) -> int:
    videos = await db.videos.count_documents({"creator_id": oid, "created_at": {"$gte": since}})
    jobs = await db.pending_videos.count_documents({"creator_id": oid, "created_at": {"$gte": since},
                                                    "status": {"$ne": "completed"}})
    return videos + jobs


async def check(db, user_id: str) -> None:
    from app.services import creator_push
    if not await creator_push.on(db, "post_limits"):
        return
    oid = ObjectId(user_id)
    u = await db.users.find_one({"_id": oid}, {"user_type": 1})
    if u and u.get("user_type") == "admin":
        return
    now = datetime.utcnow()
    if await _made(db, oid, now - timedelta(hours=1)) >= POSTS_PER_HOUR \
            or await _made(db, oid, now - timedelta(days=1)) >= POSTS_PER_DAY:
        raise HTTPException(status_code=429,
                            detail="You've posted a lot in a short time. Please try again a little later.")
