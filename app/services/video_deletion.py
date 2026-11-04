"""Shared video deletion, used by DELETE /videos/{id} and by banning a user.

`soft_delete_video` hides a video and removes its media everywhere it lives:
the Cloudflare Stream video, the Recombee item, and its files on DigitalOcean
Spaces (photo-post images, the thumbnail, and for videos uploaded before the
move to Stream, the original file). Callers handle permissions.

Files on Spaces are only deleted when nothing else still uses them, because
photos, thumbnails and profile pictures all live in the same folder and a few
files (like the default thumbnail) are shared.
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Optional

from bson import ObjectId
from recombee_api_client.api_requests import DeleteItem

from app.services.recombee_service import recombee_send
from app.services.scheduled_publisher import CANCELLED, PUBLISHING, SCHEDULED
from app.services.upload_DO import (
    DELETED_VIDEO_PLACEHOLDER_UID,
    DELETED_VIDEO_PLACEHOLDER_URL,
    delete_from_spaces,
    delete_stream_video,
    is_server_upload_path,
)
from app.services.video_posts import DEFAULT_THUMBNAIL, post_stream_uid, stream_uid_reference_query

logger = logging.getLogger(__name__)

# Files that must never be deleted however they're referenced.
_PROTECTED_SPACES_URLS = frozenset({DEFAULT_THUMBNAIL})


# Where a Spaces URL can be referenced from outside videos/users.
_SONG_URL_FIELDS = ("uri", "thumbnail", "artwork", "artwork_url", "cover_url", "cover_art_url", "audio_url")
_TV_TITLE_URL_FIELDS = (
    "poster", "backdrop", "video_url", "hls", "trailer", "trailer.thumbnail",
    "trailer.video_url", "captions.url",
)
_DRAFT_URL_FIELDS = ("audioUrl", "clips.remoteUrl", "clips.trimmedUri")


async def _any_references(collection, fields, url, extra: Optional[dict] = None) -> bool:
    query: dict[str, Any] = {"$or": [{f: url} for f in fields]}
    if extra:
        query.update(extra)
    return await collection.find_one(query, {"_id": 1}) is not None


async def is_spaces_file_referenced(
    db,
    url: str,
    *,
    exclude_video_id: Optional[ObjectId] = None,
    exclude_user_id: Optional[ObjectId] = None,
) -> bool:
    """True if anything other than the excluded video/user still uses `url`:
    a video (media, thumbnail, images, sound), a user picture, a draft, a
    library song, or a TV title/genre."""
    video_query: dict[str, Any] = {
        "$or": [{"remoteUrl": url}, {"urls.thumbnail": url}, {"images": url}, {"sound.uri": url}]
    }
    if exclude_video_id is not None:
        video_query["_id"] = {"$ne": exclude_video_id}
    if await db.videos.find_one(video_query, {"_id": 1}):
        return True

    user_query: dict[str, Any] = {
        "$or": [{"profile_picture": url}, {"cover_picture": url}]
    }
    if exclude_user_id is not None:
        user_query["_id"] = {"$ne": exclude_user_id}
    if await db.users.find_one(user_query, {"_id": 1}) is not None:
        return True

    if await _any_references(db.drafts, _DRAFT_URL_FIELDS, url, {"is_active": {"$ne": False}}):
        return True
    if await _any_references(db.songs, _SONG_URL_FIELDS, url):
        return True
    if await _any_references(db.tv_titles, _TV_TITLE_URL_FIELDS, url):
        return True
    return await _any_references(db.tv_genres, ("image",), url)


async def is_stream_uid_referenced(db, stream_uid: str, *, exclude_video_id: Optional[ObjectId] = None) -> bool:
    """True if another video, an active draft, or an unfinished stitch job
    uses this Cloudflare Stream UID. remoteUrl_CF comes from the client, so
    two posts can point at the same stream; deleting one must not take the
    other's video down."""
    loose = {"$regex": re.escape(stream_uid)}
    video_query = stream_uid_reference_query(stream_uid)
    if exclude_video_id is not None:
        video_query["_id"] = {"$ne": exclude_video_id}
    if await db.videos.find_one(video_query, {"_id": 1}):
        return True

    if await db.drafts.find_one(
        {"is_active": {"$ne": False}, "$or": [{"clips.remoteUrl": loose}, {"clips.trimmedUri": loose}]},
        {"_id": 1},
    ):
        return True

    return await db.pending_videos.find_one(
        {"status": {"$in": ["waiting", "processing"]},
         "$or": [{"clips.stream_uid": stream_uid}, {"clips.url": loose}]},
        {"_id": 1},
    ) is not None


async def delete_spaces_file_if_unused(
    db,
    url: Optional[str],
    *,
    exclude_video_id: Optional[ObjectId] = None,
    exclude_user_id: Optional[ObjectId] = None,
) -> bool:
    """Delete a Spaces file unless it's protected or still in use elsewhere.

    Returns True when a delete was attempted. Failures are logged, never
    raised, so cleanup can't break the request that triggered it.
    """
    if not url or url in _PROTECTED_SPACES_URLS:
        return False
    # Only files our own uploads wrote; never music, TV or other library folders.
    if not is_server_upload_path(url):
        return False
    if await is_spaces_file_referenced(
        db, url, exclude_video_id=exclude_video_id, exclude_user_id=exclude_user_id
    ):
        return False
    try:
        # boto3 is blocking; keep it off the event loop.
        await asyncio.to_thread(delete_from_spaces, url)
    except Exception as e:
        logger.warning("Failed to delete Spaces file %s: %s", url, e)
    return True


async def soft_delete_video(db, video: dict) -> bool:
    """Hide a video and delete its media. No-op if it's already deleted.

    Returns True if the video was deleted by this call. An already-deleted
    video is skipped entirely: re-reading its links would point at the shared
    placeholder stream, and its media was cleaned up the first time.

    A scheduled post is hidden (is_active=False) but not deleted, so it is
    cancelled here first, which keeps the publisher from ever making it live.
    """
    was_scheduled = False
    if video.get("status") in (SCHEDULED, PUBLISHING):
        cancelled = await db.videos.update_one(
            {"_id": video["_id"], "status": {"$in": [SCHEDULED, PUBLISHING]}},
            {"$set": {"status": CANCELLED}},
        )
        was_scheduled = cancelled.modified_count == 1
        if not was_scheduled:
            # The publisher made it live since it was read; delete it as a live post.
            video = await db.videos.find_one({"_id": video["_id"]}) or video

    if video.get("is_active") is False and not was_scheduled:
        return False

    video_id = video["_id"]
    # Captured before the update below overwrites the stream links.
    original_remote_url = video.get("remoteUrl")
    original_remote_url_cf = video.get("remoteUrl_CF")
    images = video.get("images") or []
    thumbnail_url = (video.get("urls") or {}).get("thumbnail")
    stream_uid = video.get("stream_uid") or post_stream_uid(original_remote_url_cf, original_remote_url)

    # Soft delete, and swap in the placeholder stream so stale references
    # (feeds, bookmarks, likes) never resolve to a dead link.
    await db.videos.update_one(
        {"_id": video_id},
        {"$set": {
            "is_active": False,
            "updated_at": datetime.utcnow(),
            "remoteUrl": DELETED_VIDEO_PLACEHOLDER_URL,
            "remoteUrl_CF": DELETED_VIDEO_PLACEHOLDER_URL,
            "stream_uid": DELETED_VIDEO_PLACEHOLDER_UID,
        }},
    )
    # A scheduled post was never counted on the user.
    if not was_scheduled:
        await db.users.update_one(
            {"_id": video["creator_id"]},
            {"$inc": {"videos_count": -1}},
        )

    # Cloudflare Stream (delete_stream_video itself refuses the placeholder).
    if stream_uid and stream_uid != DELETED_VIDEO_PLACEHOLDER_UID:
        try:
            if await is_stream_uid_referenced(db, stream_uid, exclude_video_id=video_id):
                logger.info("Keeping Cloudflare Stream video %s: still referenced elsewhere", stream_uid)
            else:
                await asyncio.to_thread(delete_stream_video, stream_uid)
        except Exception as e:
            logger.warning("Failed to delete Cloudflare Stream video %s: %s", stream_uid, e)

    # Spaces: photo-post images, the pre-Stream original file (a Stream link
    # here is ignored by delete_from_spaces), and the thumbnail. A photo post's
    # thumbnail is one of its images, so it's only deleted once.
    seen = set()
    for url in [*images, original_remote_url, thumbnail_url]:
        if not url or url in seen:
            continue
        seen.add(url)
        await delete_spaces_file_if_unused(db, url, exclude_video_id=video_id)

    try:
        req = DeleteItem(str(video_id))
        req.timeout = 5000
        await recombee_send(req)
    except Exception:
        pass

    return True
