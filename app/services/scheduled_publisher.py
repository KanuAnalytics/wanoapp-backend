"""
app/services/scheduled_publisher.py

Scheduled posts. A scheduled post is saved up front with is_active=False, so
every existing feed / profile / search query (they all filter on is_active)
ignores it. This module validates the schedule and, once a post is due, makes
it live: a loop started from the app's lifespan picks up due posts, and
"post now" in the scheduled posts API shares the same publish step.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
from bson import ObjectId
from fastapi import HTTPException, status
from recombee_api_client.api_requests import SetItemValues

from app.core.database import get_database
from app.services.expo import send_push_message
from app.services.recombee_service import recombee_send

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30
MIN_LEAD_TIME = timedelta(minutes=5)
MAX_LEAD_TIME = timedelta(days=365)
# A claim older than this means the process died mid-publish; hand it back.
STALE_CLAIM = timedelta(minutes=10)

SCHEDULED = "scheduled"
PUBLISHING = "publishing"
PUBLISHED = "published"
CANCELLED = "cancelled"


def normalize_schedule(
    scheduled_at: Optional[datetime],
    timezone_name: Optional[str],
    require_lead_time: bool = True,
) -> tuple[Optional[datetime], Optional[str]]:
    """
    Returns (naive UTC datetime, IANA zone) for a requested schedule, or
    (None, None) when the post isn't scheduled. The rest of the app stores
    naive UTC timestamps, so this does too. The zone is kept only so the app
    can show the time the way the user picked it.

    A new post is created only after the app has compressed (and, for a
    stitch, uploaded) the video, so its time may have come close or passed
    by then. Those pass require_lead_time=False: the app already checked the
    lead time when the user picked it, and a time that has passed simply
    publishes on the next tick.
    """
    if scheduled_at is None:
        return None, None

    if timezone_name:
        if timezone_name not in pytz.all_timezones_set:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown time zone")
    else:
        timezone_name = None

    if scheduled_at.tzinfo is not None:
        scheduled_at = scheduled_at.astimezone(timezone.utc).replace(tzinfo=None)

    now = datetime.utcnow()
    if require_lead_time and scheduled_at < now + MIN_LEAD_TIME:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Schedule the post at least 5 minutes from now",
        )
    if scheduled_at > now + MAX_LEAD_TIME:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Posts can be scheduled at most a year ahead",
        )
    return scheduled_at, timezone_name


async def publish_post(video_id) -> bool:
    """
    Makes a scheduled post live. Returns False when it wasn't published: it
    isn't scheduled any more, or its video is still processing. Only one
    caller can win the claim, so a double tick or a second worker can't
    publish the same post twice.
    """
    db = get_database()
    now = datetime.utcnow()

    video = await db.videos.find_one_and_update(
        {
            "_id": ObjectId(video_id),
            "status": SCHEDULED,
            "isReadyToStream": {"$ne": False},
        },
        {"$set": {"status": PUBLISHING, "claimed_at": now}},
    )
    if not video:
        return False

    try:
        went_live = await _go_live(video, now)
    except Exception:
        # The post never went live; back to scheduled so the next tick retries.
        logger.exception("Publishing scheduled post %s failed", video["_id"])
        await db.videos.update_one(
            {"_id": video["_id"], "status": PUBLISHING},
            {"$set": {"status": SCHEDULED}, "$unset": {"claimed_at": ""}},
        )
        return False
    if not went_live:
        return False

    # The post is live from here on. Nothing below sends it back to
    # scheduled, or a retry would count it on the user a second time.
    try:
        await db.users.update_one({"_id": video["creator_id"]}, {"$inc": {"videos_count": 1}})
    except Exception:
        logger.exception("Counting scheduled post %s on its creator failed", video["_id"])
    await _sync_recombee(video, now)
    await _notify_creator(video, now)
    return True


async def _go_live(video: dict, now: datetime) -> bool:
    """
    Flips the post live. False when it is no longer ours to publish: it was
    deleted, or its claim went stale and was handed back, after the claim.
    """
    db = get_database()

    result = await db.videos.update_one(
        {"_id": video["_id"], "status": PUBLISHING},
        {
            "$set": {
                "is_active": True,
                "status": PUBLISHED,
                "published_at": now,
                # It joins the feed as a new post, not one dated when it was drafted.
                "created_at": now,
                "updated_at": now,
            },
            "$unset": {"claimed_at": ""},
        },
    )
    return result.modified_count == 1


async def _sync_recombee(video: dict, now: datetime) -> None:
    # The same item values create_video_post sends for an ordinary post; a
    # scheduled post was held back from Recombee until now.
    try:
        urls = video.get("urls") or {}
        values = {
            "creator_id": str(video["creator_id"]),
            "description": video.get("description") or "",
            "video_type": video.get("video_type") or "",
            "duration": float(video.get("duration") or 0.0),
            "thumbnail": urls.get("thumbnail") or "",
            "hashtags": video.get("hashtags") or [],
            "is_active": True,
            "supports_landscape": bool(video.get("supports_landscape", False)),
            "privacy": video.get("privacy") or "public",
            "created_at": now.isoformat(),
            "is_ready_to_stream": bool(video.get("isReadyToStream", False)),
            "media_type": video.get("media_type", "video"),
        }
        req = SetItemValues(str(video["_id"]), values, cascade_create=True)
        req.timeout = 10000
        await recombee_send(req)
        await get_database().videos.update_one({"_id": video["_id"]}, {"$set": {"recombee": True}})
    except Exception:
        # The post is live; a failed sync only delays it reaching recommendations.
        logger.exception("Recombee sync failed for scheduled post %s", video["_id"])


async def _notify_creator(video: dict, now: datetime) -> None:
    db = get_database()
    try:
        creator = await db.users.find_one({"_id": video["creator_id"]}, {"expo_push_tokens": 1})
        tokens = (creator or {}).get("expo_push_tokens") or []
        thumbnail = (video.get("urls") or {}).get("thumbnail")
        for token in tokens:
            # send_push_message is a blocking HTTP call.
            await asyncio.to_thread(
                send_push_message,
                token,
                "Your scheduled post is now live!",
                {"screen": "profile_v2"},
                None,
                thumbnail,
            )
        await db.notifications.insert_one({
            "recipient_id": video["creator_id"],
            "type": "video_ready",
            "post_id": video["_id"],
            "date": now,
        })
    except Exception:
        logger.exception("Could not notify creator of scheduled post %s", video["_id"])


async def publish_due_posts() -> int:
    """One pass: release stale claims, then publish every post that is due."""
    db = get_database()
    now = datetime.utcnow()

    await db.videos.update_many(
        {"status": PUBLISHING, "claimed_at": {"$lt": now - STALE_CLAIM}},
        {"$set": {"status": SCHEDULED}, "$unset": {"claimed_at": ""}},
    )

    published = 0
    due = db.videos.find(
        {"status": SCHEDULED, "scheduled_at": {"$lte": now}, "isReadyToStream": {"$ne": False}},
        {"_id": 1},
    ).limit(50)
    async for doc in due:
        if await publish_post(doc["_id"]):
            published += 1
    return published


class ScheduledPublisher:
    def __init__(self, interval: int = POLL_INTERVAL_SECONDS):
        self.interval = interval
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        self._task = asyncio.create_task(self._run())
        logger.info("ScheduledPublisher started, polling every %ss", self.interval)

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ScheduledPublisher stopped")

    async def _run(self):
        # The first pass runs at once, so posts that came due while the
        # server was down go live on startup.
        while True:
            try:
                count = await publish_due_posts()
                if count:
                    logger.info("Published %s scheduled post(s)", count)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduled publisher pass failed")
            await asyncio.sleep(self.interval)


scheduled_publisher = ScheduledPublisher()
