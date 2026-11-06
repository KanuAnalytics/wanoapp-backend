"""
Notifications endpoints

app/api/v1/notifications.py
"""
import asyncio
import json
import logging
from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from bson import ObjectId
from bson.json_util import dumps
from app.core.database import get_database
from app.api.deps import get_current_active_user, require_admin
from app.services.expo import send_push_batch
from fastapi import Depends, Query
from typing import List, Optional
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

router = APIRouter()

# Only one broadcast may run at a time per process; a second request while
# one is in flight gets 409 instead of pushing every device twice.
_broadcast_lock = asyncio.Lock()

BROADCAST_TITLE = "Meet the new Wano 👀"
BROADCAST_BODY = "A whole new look. A whole new experience.\n\nUpdate Wano now →"
PUSH_BATCH_SIZE = 100  # Expo's max messages per request


async def _run_broadcast():
    db = get_database()
    cursor = db.users.find(
        {"expo_push_tokens": {"$exists": True, "$ne": []}},
        {"expo_push_tokens": 1},
    )

    tokens: List[str] = []
    async for user in cursor:
        tokens.extend(user.get("expo_push_tokens") or [])

    for i in range(0, len(tokens), PUSH_BATCH_SIZE):
        # send_push_batch is a blocking `requests` call - run it off the event
        # loop so a 10k-user broadcast doesn't stall the whole server for
        # everyone else while it works through ~100 sequential HTTP requests.
        await asyncio.to_thread(
            send_push_batch, tokens[i:i + PUSH_BATCH_SIZE], BROADCAST_BODY, BROADCAST_TITLE
        )

    logger.info("broadcast_notification: done, total_tokens=%d", len(tokens))


async def _run_broadcast_locked():
    """Run the broadcast; the caller already acquired _broadcast_lock."""
    try:
        await _run_broadcast()
    except Exception:
        logger.exception("broadcast_notification failed")
    finally:
        _broadcast_lock.release()


@router.post("/broadcast")
async def broadcast_notification(
    background_tasks: BackgroundTasks,
    current_user: str = Depends(require_admin),
):
    """
    Push-only broadcast to every registered device across all users (admin only).
    Does NOT write to db.notifications - this never shows up in the app's
    in-app notification list, it's a push notification only.
    """
    if _broadcast_lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A broadcast is already running",
        )
    # Uncontended acquire does not yield, so check-and-take is atomic on the loop.
    await _broadcast_lock.acquire()
    try:
        background_tasks.add_task(_run_broadcast_locked)
    except Exception:
        _broadcast_lock.release()
        raise
    return {"message": "Broadcast started in background"}

class NotificationPrefs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    new_posts: Optional[bool] = None       # "Amina just posted" from people you follow
    creator_tips: Optional[bool] = None    # first-viewer results, streak reminders


def _prefs_out(user: dict) -> dict:
    p = user.get("notification_prefs") if isinstance(user.get("notification_prefs"), dict) else {}
    return {"new_posts": p.get("new_posts") is not False, "creator_tips": p.get("creator_tips") is not False}


@router.get("/preferences")
async def get_notification_prefs(current_user: str = Depends(get_current_active_user)):
    db = get_database()
    user = await db.users.find_one({"_id": ObjectId(current_user)}, {"notification_prefs": 1}) or {}
    return _prefs_out(user)


@router.put("/preferences")
async def set_notification_prefs(body: NotificationPrefs, current_user: str = Depends(get_current_active_user)):
    """Turn growth notifications on or off. Only these two switches are stored."""
    db = get_database()
    changes = {f"notification_prefs.{k}": v for k, v in (("new_posts", body.new_posts),
                                                         ("creator_tips", body.creator_tips)) if v is not None}
    user = await db.users.find_one({"_id": ObjectId(current_user)}, {"notification_prefs": 1}) or {}
    if not isinstance(user.get("notification_prefs"), dict):
        await db.users.update_one({"_id": ObjectId(current_user)}, {"$set": {"notification_prefs": {}}})
    if changes:
        await db.users.update_one({"_id": ObjectId(current_user)}, {"$set": changes})
    user = await db.users.find_one({"_id": ObjectId(current_user)}, {"notification_prefs": 1}) or {}
    return _prefs_out(user)


@router.get("/")
async def get_notifications(
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=50),
    type: Optional[str] = Query(None, max_length=50, description="Filter by notification type"),
    current_user: str = Depends(get_current_active_user),
):
    """Get notifications for the current user (fresh user/post info via lookup)"""
    db = get_database()

    match = {"recipient_id": ObjectId(current_user)}
    if type:
        match["type"] = type

    pipeline = [
        {"$match": match},
        {"$sort": {"date": -1}},
        {"$skip": skip},
        {"$limit": limit},
        {"$lookup": {
            "from": "users",
            "localField": "user_id",
            "foreignField": "_id",
            "as": "user",
        }},
        {"$unwind": {"path": "$user", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {
            "from": "videos",
            "localField": "post_id",
            "foreignField": "_id",
            "as": "post",
        }},
        {"$unwind": {"path": "$post", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {
            "from": "comments",
            "localField": "comment_id",
            "foreignField": "_id",
            "as": "comment",
        }},
        {"$unwind": {"path": "$comment", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {
            "from": "comments",
            "localField": "comment.parent_id",
            "foreignField": "_id",
            "as": "parent_comment",
        }},
        {"$unwind": {"path": "$parent_comment", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "_id": {"$toString": "$_id"},
            "type": 1,
            "date": {"$dateToString": {"format": "%Y-%m-%dT%H:%M:%S.%LZ", "date": "$date"}},
            "user": {
                "id": {"$toString": "$user._id"},
                "name": {"$ifNull": ["$user.display_name", "$user.username"]},
                "username": "$user.username",
                "profile_picture": "$user.profile_picture",
            },
            "is_following": {
                "$cond": [
                    {"$eq": ["$type", "follow"]},
                    {"$in": ["$recipient_id", {"$ifNull": ["$user.followers", []]}]},
                    "$$REMOVE",
                ]
            },
            "post": {
                "id": {"$toString": "$post._id"},
                "thumbnail": "$post.urls.thumbnail",
                "comment": {
                    "$cond": [
                        {"$eq": ["$type", "comment_reply"]},
                        "$parent_comment.content",
                        "$comment.content",
                    ]
                },
                "reply": {
                    "$cond": [
                        {"$eq": ["$type", "comment_reply"]},
                        "$comment.content",
                        None,
                    ]
                },
            },
        }},
    ]

    cursor = db.notifications.aggregate(pipeline)
    docs = await cursor.to_list(length=limit)
    return json.loads(dumps(docs))
