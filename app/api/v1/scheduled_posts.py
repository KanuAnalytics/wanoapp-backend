"""
Scheduled posts: the creator's own list, reschedule, post now and cancel.

app/api/v1/scheduled_posts.py

Registered before the videos router, so /videos/scheduled is not read as a video id.
"""
from datetime import datetime
from typing import Literal, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.api.deps import get_verified_user
from app.core.database import get_database
from app.services.scheduled_publisher import (
    PUBLISHED,
    PUBLISHING,
    SCHEDULED,
    normalize_schedule,
    publish_post,
)
from app.services.video_deletion import soft_delete_video

router = APIRouter()


class RescheduleBody(BaseModel):
    scheduled_at: datetime
    timezone: Optional[str] = None


def _object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Post not found")


def _utc_iso(value: Optional[datetime]) -> Optional[str]:
    # Stored as naive UTC; the trailing Z lets the app parse it as UTC.
    return value.isoformat() + "Z" if value else None


def _item(video: dict) -> dict:
    urls = video.get("urls") or {}
    images = video.get("images") or []
    return {
        "id": str(video["_id"]),
        "title": video.get("title"),
        "description": video.get("description") or "",
        "hashtags": video.get("hashtags") or [],
        "thumbnail": urls.get("thumbnail") or (images[0] if images else None),
        "duration": video.get("duration") or 0,
        "media_type": video.get("media_type") or "video",
        "privacy": video.get("privacy") or "public",
        "status": video.get("status"),
        "scheduled_at": _utc_iso(video.get("scheduled_at")),
        "scheduled_timezone": video.get("scheduled_timezone"),
        "published_at": _utc_iso(video.get("published_at")),
    }


async def _own_scheduled_post(video_id: str, user_id: str) -> dict:
    db = get_database()
    video = await db.videos.find_one({
        "_id": _object_id(video_id),
        "creator_id": ObjectId(user_id),
        "status": SCHEDULED,
    })
    if not video:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scheduled post not found")
    return video


@router.get("/scheduled")
async def list_scheduled_posts(
    tab: Literal["scheduled", "published"] = "scheduled",
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
    current_user: str = Depends(get_verified_user),
):
    db = get_database()
    creator_id = ObjectId(current_user)

    if tab == "scheduled":
        query = {"creator_id": creator_id, "status": {"$in": [SCHEDULED, PUBLISHING]}}
        sort = [("scheduled_at", 1)]
    else:
        query = {"creator_id": creator_id, "status": PUBLISHED, "scheduled_at": {"$ne": None}}
        sort = [("published_at", -1)]

    cursor = db.videos.find(query).sort(sort).skip((page - 1) * limit).limit(limit + 1)
    videos = [video async for video in cursor]
    has_more = len(videos) > limit

    scheduled_count = await db.videos.count_documents(
        {"creator_id": creator_id, "status": {"$in": [SCHEDULED, PUBLISHING]}}
    )
    return {
        "items": [_item(video) for video in videos[:limit]],
        "has_more": has_more,
        "scheduled_count": scheduled_count,
    }


@router.patch("/{video_id}/schedule")
async def reschedule_post(
    video_id: str,
    body: RescheduleBody,
    current_user: str = Depends(get_verified_user),
):
    video = await _own_scheduled_post(video_id, current_user)
    scheduled_at, scheduled_timezone = normalize_schedule(body.scheduled_at, body.timezone)

    db = get_database()
    # The status filter again, in case the publisher claimed it since the read above.
    result = await db.videos.update_one(
        {"_id": video["_id"], "status": SCHEDULED},
        {"$set": {
            "scheduled_at": scheduled_at,
            "scheduled_timezone": scheduled_timezone,
            "updated_at": datetime.utcnow(),
        }},
    )
    if not result.modified_count:
        raise HTTPException(status.HTTP_409_CONFLICT, "This post is already being published")
    return {
        "message": "Post rescheduled",
        "scheduled_at": _utc_iso(scheduled_at),
        "scheduled_timezone": scheduled_timezone,
    }


@router.post("/{video_id}/publish-now")
async def publish_now(
    video_id: str,
    current_user: str = Depends(get_verified_user),
):
    video = await _own_scheduled_post(video_id, current_user)
    if video.get("isReadyToStream") is False:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This video is still processing. Try again in a moment.",
        )
    if not await publish_post(video["_id"]):
        raise HTTPException(status.HTTP_409_CONFLICT, "Could not publish this post")
    return {"message": "Post published"}


@router.delete("/{video_id}/schedule")
async def cancel_scheduled_post(
    video_id: str,
    current_user: str = Depends(get_verified_user),
):
    video = await _own_scheduled_post(video_id, current_user)
    # Cancels it and removes its media, the same way DELETE /videos/{id} does.
    # If the publisher made it live in the meantime, it is deleted as a live post.
    await soft_delete_video(get_database(), video)
    return {"message": "Scheduled post deleted"}
