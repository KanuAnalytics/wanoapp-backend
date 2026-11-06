"""Everything Wano tells Recombee: who is watching, what each video is, and how
people engage with it.

Fixes for the For You feed live here:
  1. Logged-out viewers get their own Recombee profile per device
     ("anon-<device id>"), merged into their account when they sign in,
     instead of all sharing one "temp-user".
  2. One builder (item_values) for every video sent to Recombee, so no video
     is missing `is_ready_to_stream` (which silently hid it from the feed).
     sync_items() backfills the whole catalog.
  3. Views, likes, comments and bookmarks are pushed to Recombee as they
     change (sync_dirty_item_stats), so it can tell a hit from a dud.
  5. Engagement goes in as the right interaction: comments are cart
     additions (not bookmarks), shares are purchases, "not interested" is
     a negative rating.
  6. A user's interest tags and country go to Recombee as user properties,
     so new users don't start blank.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Iterable, Optional

from bson import ObjectId
from recombee_api_client.api_requests import (
    AddCartAddition, AddItemProperty, AddPurchase, AddRating, AddUserProperty,
    Batch, DeleteCartAddition, MergeUsers, SetItemValues, SetUserValues,
)

from app.services.recombee_service import recombee_send
from app.services.upload_DO import extract_stream_uid

logger = logging.getLogger(__name__)

ANON_PREFIX = "anon-"
LEGACY_ANON_USER = "temp-user"   # old app builds that send no device id
BATCH_SIZE = 500
STATS_SYNC_SECONDS = 300

ITEM_PROPERTIES = {
    "creator_id": "string", "description": "string", "video_type": "string",
    "duration": "double", "thumbnail": "string", "hashtags": "set",
    "is_active": "boolean", "supports_landscape": "boolean", "privacy": "string",
    "created_at": "timestamp", "is_ready_to_stream": "boolean", "media_type": "string",
    "views_count": "int", "likes_count": "int", "comments_count": "int", "bookmarks_count": "int",
}
USER_PROPERTIES = {"tags": "set", "country": "string"}
STAT_FIELDS = ("views_count", "likes_count", "comments_count", "bookmarks_count")


# ------------------------------------------------------------------ who is watching

def anon_id(device_id: Optional[str]) -> Optional[str]:
    """Recombee id for a logged-out device; None when no usable device id."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", device_id or "")[:64]
    return f"{ANON_PREFIX}{cleaned}" if cleaned else None


def viewer_id(current_user: Optional[str], device_id: Optional[str]) -> str:
    return current_user or anon_id(device_id) or LEGACY_ANON_USER


async def merge_device_into_user(device_id: Optional[str], user_id: str) -> None:
    """Carry what a device watched while logged out into the account."""
    source = anon_id(device_id)
    if not source or not user_id:
        return
    try:
        req = MergeUsers(user_id, source, cascade_create=True)
        req.timeout = 5000
        await recombee_send(req)
    except Exception:
        # A device that never watched anything has no Recombee user yet.
        logger.info("Recombee merge %s -> %s skipped", source, user_id)


async def set_user_profile(user: dict) -> None:
    """Interest tags and country, used to shape a new user's first feed."""
    if not user or not user.get("_id"):
        return
    tags = [t for t in (user.get("tags") or []) if isinstance(t, str) and t][:50]
    country = ((user.get("localization") or {}).get("country") or "")
    try:
        req = SetUserValues(str(user["_id"]), {"tags": tags, "country": str(country)[:8]}, cascade_create=True)
        req.timeout = 5000
        await recombee_send(req)
    except Exception:
        logger.warning("Recombee profile update failed for %s", user.get("_id"))


# ------------------------------------------------------------------ what each video is

def is_ready_to_stream(video: dict) -> bool:
    """True when the post can play. Photo posts always can. Older videos were
    created before the flag existed; they count as ready when they have a
    playable link."""
    if (video.get("media_type") == "photo") or (video.get("images") and not video.get("remoteUrl_CF")):
        return True
    flag = video.get("isReadyToStream")
    if flag is not None:
        return bool(flag)
    return bool(extract_stream_uid(video.get("remoteUrl_CF") or "") or video.get("remoteUrl"))


def _iso(dt) -> Optional[str]:
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _num(value, cast=int, default=0):
    try:
        return cast(value or default)
    except (TypeError, ValueError):
        return cast(default)


def _thumbnail(video: dict) -> str:
    from app.services.thumbnails import best_thumbnail
    try:
        return best_thumbnail(video) or ""
    except Exception:
        return ""


def item_values(video: dict) -> dict:
    """The one definition of a video's Recombee properties."""
    media_type = video.get("media_type") or ("photo" if video.get("images") and not video.get("remoteUrl_CF") else "video")
    duration = video.get("duration") or (video.get("metadata") or {}).get("duration") or 0.0
    values = {
        "creator_id": str(video.get("creator_id") or ""),
        "description": (video.get("description") or "")[:2000],
        "video_type": video.get("video_type") or "",
        "duration": _num(duration, float, 0.0),
        "thumbnail": _thumbnail(video),
        "hashtags": [h for h in (video.get("hashtags") or []) if isinstance(h, str)][:50],
        "is_active": bool(video.get("is_active", True)),
        "supports_landscape": bool(video.get("supports_landscape", False)),
        "privacy": video.get("privacy") or "public",
        "is_ready_to_stream": is_ready_to_stream(video),
        "media_type": media_type,
    }
    created = _iso(video.get("published_at") or video.get("created_at"))
    if created:
        values["created_at"] = created
    for f in STAT_FIELDS:
        values[f] = max(0, _num(video.get(f)))
    return values


async def push_item(video: dict) -> bool:
    try:
        req = SetItemValues(str(video["_id"]), item_values(video), cascade_create=True)
        req.timeout = 10000
        await recombee_send(req)
        return True
    except Exception:
        logger.exception("Recombee sync failed for video %s", video.get("_id"))
        return False


async def ensure_properties() -> None:
    """Create the item/user properties Recombee needs (existing ones are left alone)."""
    for name, kind in ITEM_PROPERTIES.items():
        try:
            await recombee_send(AddItemProperty(name, kind))
        except Exception:
            pass  # already exists
    for name, kind in USER_PROPERTIES.items():
        try:
            await recombee_send(AddUserProperty(name, kind))
        except Exception:
            pass


async def sync_items(db, batch_size: int = BATCH_SIZE) -> dict:
    """Send every video's current properties to Recombee in batches."""
    await ensure_properties()
    sent = failed = ready = 0
    batch = []

    async def flush():
        nonlocal sent, failed, batch
        if not batch:
            return
        try:
            req = Batch(batch)
            req.timeout = 60000
            await recombee_send(req)
            sent += len(batch)
        except Exception:
            logger.exception("Recombee batch of %d failed", len(batch))
            failed += len(batch)
        batch = []

    async for video in db.videos.find({}):
        values = item_values(video)
        ready += values["is_ready_to_stream"] and values["is_active"] and values["privacy"] == "public"
        batch.append(SetItemValues(str(video["_id"]), values, cascade_create=True))
        if len(batch) >= batch_size:
            await flush()
    await flush()
    await db.videos.update_many({}, {"$set": {"recombee": True}})
    return {"sent": sent, "failed": failed, "showable_in_feed": int(ready)}


# ------------------------------------------------------------------ engagement counts

_dirty: set = set()


def mark_stats_dirty(video_ids: Iterable[str]) -> None:
    _dirty.update(v for v in video_ids if ObjectId.is_valid(v))


async def sync_dirty_item_stats() -> int:
    """Push current counts for videos whose engagement changed since last time."""
    from app.core.database import get_database
    if not _dirty:
        return 0
    ids = list(_dirty)
    _dirty.clear()
    db = get_database()
    projection = {f: 1 for f in STAT_FIELDS}
    reqs = []
    async for v in db.videos.find({"_id": {"$in": [ObjectId(i) for i in ids]}}, projection):
        reqs.append(SetItemValues(str(v["_id"]), {f: max(0, _num(v.get(f))) for f in STAT_FIELDS},
                                  cascade_create=False))
    try:
        for i in range(0, len(reqs), BATCH_SIZE):
            req = Batch(reqs[i:i + BATCH_SIZE])
            req.timeout = 30000
            await recombee_send(req)
    except Exception:
        logger.warning("Recombee stats sync failed; will retry %d videos", len(ids))
        _dirty.update(ids)
        return 0
    return len(reqs)


class StatsSyncLoop:
    def __init__(self):
        self._task = None

    async def _run(self):
        while True:
            try:
                await asyncio.sleep(STATS_SYNC_SECONDS)
                await sync_dirty_item_stats()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Recombee stats sync loop error")

    async def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None


stats_sync = StatsSyncLoop()


# ------------------------------------------------------------------ interactions

async def _send(req) -> None:
    try:
        req.timeout = 5000
        await recombee_send(req)
    except Exception:
        pass


async def report_comment(user_id: str, video_id: str, recomm_id: Optional[str] = None) -> None:
    kw = {"cascade_create": True}
    if recomm_id:
        kw["recomm_id"] = recomm_id
    await _send(AddCartAddition(user_id, video_id, **kw))


async def report_comment_deleted(user_id: str, video_id: str) -> None:
    await _send(DeleteCartAddition(user_id, video_id))


async def report_share(user_id: str, video_id: str, recomm_id: Optional[str] = None) -> None:
    kw = {"cascade_create": True}
    if recomm_id:
        kw["recomm_id"] = recomm_id
    await _send(AddPurchase(user_id, video_id, **kw))


async def report_not_interested(user_id: str, video_id: str, recomm_id: Optional[str] = None) -> None:
    kw = {"cascade_create": True}
    if recomm_id:
        kw["recomm_id"] = recomm_id
    await _send(AddRating(user_id, video_id, -1.0, **kw))
