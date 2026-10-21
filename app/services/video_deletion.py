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
from datetime import datetime
from typing import Any, Optional

from bson import ObjectId
from recombee_api_client.api_requests import DeleteItem

from app.services.recombee_service import recombee_send
from app.services.upload_DO import (
    DELETED_VIDEO_PLACEHOLDER_URL,
    delete_from_spaces,
    delete_stream_video,
    extract_stream_uid,
)
from app.services.video_posts import DEFAULT_THUMBNAIL

# Files that must never be deleted however they're referenced.
_PROTECTED_SPACES_URLS = frozenset({DEFAULT_THUMBNAIL})


async def is_spaces_file_referenced(
    db,
    url: str,
    *,
    exclude_video_id: Optional[ObjectId] = None,
    exclude_user_id: Optional[ObjectId] = None,
) -> bool:
    """True if any video or user, other than the excluded ones, uses `url`."""
    video_query: dict[str, Any] = {
        "$or": [{"remoteUrl": url}, {"urls.thumbnail": url}, {"images": url}]
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
    return await db.users.find_one(user_query, {"_id": 1}) is not None


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
    if await is_spaces_file_referenced(
        db, url, exclude_video_id=exclude_video_id, exclude_user_id=exclude_user_id
    ):
        return False
    try:
        # boto3 is blocking; keep it off the event loop.
        await asyncio.to_thread(delete_from_spaces, url)
    except Exception as e:
        print(f"Failed to delete Spaces file {url}: {e}")
    return True


async def soft_delete_video(db, video: dict) -> bool:
    """Hide a video and delete its media. No-op if it's already deleted.

    Returns True if the video was deleted by this call. An already-deleted
    video is skipped entirely: re-reading its links would point at the shared
    placeholder stream, and its media was cleaned up the first time.
    """
    if video.get("is_active") is False:
        return False

    video_id = video["_id"]
    # Captured before the update below overwrites the stream links.
    original_remote_url = video.get("remoteUrl")
    original_remote_url_cf = video.get("remoteUrl_CF")
    images = video.get("images") or []
    thumbnail_url = (video.get("urls") or {}).get("thumbnail")

    # Soft delete, and swap in the placeholder stream so stale references
    # (feeds, bookmarks, likes) never resolve to a dead link.
    await db.videos.update_one(
        {"_id": video_id},
        {"$set": {
            "is_active": False,
            "updated_at": datetime.utcnow(),
            "remoteUrl": DELETED_VIDEO_PLACEHOLDER_URL,
            "remoteUrl_CF": DELETED_VIDEO_PLACEHOLDER_URL,
        }},
    )
    await db.users.update_one(
        {"_id": video["creator_id"]},
        {"$inc": {"videos_count": -1}},
    )

    # Cloudflare Stream (delete_stream_video itself refuses the placeholder).
    stream_uid = extract_stream_uid(original_remote_url_cf) or extract_stream_uid(original_remote_url)
    if stream_uid:
        try:
            await asyncio.to_thread(delete_stream_video, stream_uid)
        except Exception as e:
            print(f"Failed to delete Cloudflare Stream video {stream_uid}: {e}")

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
