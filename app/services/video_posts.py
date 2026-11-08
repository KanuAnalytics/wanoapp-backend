"""
app/services/video_posts.py

Saving a video as a post. Used by POST /api/v1/videos/post and by stitch jobs.
"""

import logging
import re
from datetime import datetime
from typing import List, Optional

from bson import ObjectId
from recombee_api_client.api_requests import SetItemValues
from app.services.recommendations import item_values

from app.core.database import get_database
from app.services.recombee_service import recombee_send
from app.services.upload_DO import extract_stream_uid

logger = logging.getLogger(__name__)

DEFAULT_THUMBNAIL = "https://wano-africadev.lon1.digitaloceanspaces.com/wanoafrica-dospaces-key/profile-pictures/thumbnail_placeholder.png"


def post_stream_uid(remote_url_cf: Optional[str], remote_url: Optional[str] = None) -> Optional[str]:
    """The Cloudflare Stream UID a post plays, stored on the video as `stream_uid`."""
    return extract_stream_uid(remote_url_cf) or extract_stream_uid(remote_url)


def stream_uid_exact_query(stream_uid: str) -> dict:
    """Videos whose stored stream_uid is this UID, or (older docs without
    stream_uid) whose playback URL has it as its first path segment.
    The URL fallback only looks at docs with no stream_uid ({"stream_uid": None}
    matches missing or null and uses the stream_uid_1_creator_id_1 index), so
    once backfill_stream_uids has run it reads a handful of docs, not all of them."""
    anchored = {"$regex": f"^https?://[^/]+/{re.escape(stream_uid)}(/|$)"}
    return {"$or": [
        {"stream_uid": stream_uid},
        {"stream_uid": None, "$or": [{"remoteUrl_CF": anchored}, {"remoteUrl": anchored}]},
    ]}


def stream_uid_reference_query(stream_uid: str) -> dict:
    """Anything that mentions the UID anywhere, for deciding whether deleting
    the stream is safe (errs towards keeping it)."""
    loose = {"$regex": re.escape(stream_uid)}
    return {"$or": [
        {"stream_uid": stream_uid},
        {"stream_uid": None, "$or": [{"remoteUrl_CF": loose}, {"remoteUrl": loose}]},
    ]}


async def backfill_stream_uids(db, apply: bool = False, limit: int = 500_000) -> dict:
    """Store stream_uid on older posts that only have it inside their URL, so
    Stream lookups (webhook, post creation, deletion) stay on the index.
    Dry run unless apply. Photo posts and URLs that aren't Stream are left alone."""
    from pymongo import UpdateOne
    would, fixed, batch = 0, 0, []
    cursor = db.videos.find({"stream_uid": None, "media_type": {"$ne": "photo"}},
                            {"remoteUrl_CF": 1, "remoteUrl": 1}).limit(limit)
    async for d in cursor:
        uid = post_stream_uid(d.get("remoteUrl_CF") if isinstance(d.get("remoteUrl_CF"), str) else None,
                              d.get("remoteUrl") if isinstance(d.get("remoteUrl"), str) else None)
        if not uid:
            continue
        would += 1
        if apply:
            batch.append(UpdateOne({"_id": d["_id"], "stream_uid": None}, {"$set": {"stream_uid": uid}}))
            if len(batch) >= 500:
                fixed += (await db.videos.bulk_write(batch, ordered=False)).modified_count
                batch = []
    if apply and batch:
        fixed += (await db.videos.bulk_write(batch, ordered=False)).modified_count
    return {"would_fix": 0 if apply else would, "fixed": fixed, "applied": apply}


async def create_video_post(
    user_id,
    *,
    media_type: str = "video",
    images: Optional[List[str]] = None,
    remote_url: Optional[str] = None,
    remote_url_cf: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    privacy="public",
    thumbnail: Optional[str] = DEFAULT_THUMBNAIL,
    duration: Optional[float] = 0.0,
    end: Optional[float] = None,
    is_ready_to_stream: Optional[bool] = False,
    width: Optional[int] = None,
    height: Optional[int] = None,
    comments_enabled: bool = True,
    category_id: Optional[str] = None,
    subcategory_id: Optional[str] = None,
    supports_landscape: Optional[bool] = None,
    sound: Optional[dict] = None,
    scheduled_at: Optional[datetime] = None,
    scheduled_timezone: Optional[str] = None,
    reply_to: Optional[dict] = None,
    remix_enabled: bool = True,
    points_category: Optional[str] = None,
    trial: bool = False,
) -> str:
    """
    Saves the post, counts it on the user, and syncs it to Recombee. Returns the new video's id.

    With scheduled_at (naive UTC) the post is saved hidden instead -- not counted and not synced --
    and app/services/scheduled_publisher.py does that part when it comes due.
    """
    db = get_database()
    creator_id = ObjectId(user_id)
    user = await db.users.find_one({"_id": creator_id})

    description = (description or "").strip()
    hashtags = re.findall(r"#(\w+)", description)
    now = datetime.utcnow()
    is_photo_post = media_type == "photo"

    video_doc = {
        "creator_id": creator_id,
        "title": title,  # Can be updated later by user
        "description": description,
        "video_type": "regular",
        "privacy": privacy,
        # Photo posts have no Cloudflare stream to ever flip this later, so
        # they must be ready immediately or they'd never surface in any feed.
        "isReadyToStream": True if is_photo_post else is_ready_to_stream,
        "metadata": {
            "duration": duration,
            "width": width if width is not None else 1080,
            "height": height if height is not None else 1920,
            "fps": 30.0,
            "file_size": 0  # You can calculate this during upload
        },
        "urls": {
            "original": None if is_photo_post else remote_url,
            "hls_playlist": None if is_photo_post else remote_url,  # In production, generate HLS separately
            # No custom thumbnail for photo posts -- always the first image.
            "thumbnail": images[0] if is_photo_post else thumbnail,
            "download": None if is_photo_post else remote_url
        },
        "categoryId": category_id,
        "subcategoryId": subcategory_id,
        # Additional fields for compatibility
        "FEid": None,
        "start": 0,
        "end": end,
        "duration": duration,
        "remoteUrl": remote_url,
        "remoteUrl_CF": remote_url_cf,
        # Exact-match key for the Cloudflare webhook and stream cleanup.
        "stream_uid": None if is_photo_post else post_stream_uid(remote_url_cf, remote_url),
        "media_type": media_type,
        "images": images or [],
        "type": media_type,
        # Standard fields
        "hashtags": hashtags,
        "categories": [],
        "remix_enabled": bool(remix_enabled),
        "comments_enabled": comments_enabled,
        "created_at": now,
        "updated_at": now,
        "is_active": True,
        "views_count": 0,
        "likes_count": 0,
        "comments_count": 0,
        "shares_count": 0,
        "bookmarks_count": 0,
        "is_approved": True,
        "is_flagged": False,
        "report_count": 0,
        "is_remix": bool(reply_to),
        "remix_count": 0,
        "country": user.get("localization", {}).get("country", "NG"),
        "language": user.get("localization", {}).get("languages", ["en"])[0]
    }

    if supports_landscape is not None:
        video_doc["supports_landscape"] = supports_landscape
    if reply_to:
        video_doc["reply_to"] = reply_to
    # Same field PUT /points/videos/{id}/category sets; only known categories.
    from app.services.points import CATEGORIES as _POINTS_CATEGORIES
    if trial is True:
        video_doc["trial"] = True
    if isinstance(points_category, str) and points_category in _POINTS_CATEGORIES:
        video_doc["category"] = points_category

    # A copy of the song ({id, title, uri, artist}), so the feed can show its
    # name -- and play it, for photo posts -- without looking the song up.
    if sound:
        video_doc["sound"] = sound

    if scheduled_at is not None:
        # Every feed, profile and search query filters on is_active, so this
        # alone keeps the post hidden until it is published.
        video_doc["is_active"] = False
        video_doc["status"] = "scheduled"
        video_doc["scheduled_at"] = scheduled_at
        video_doc["scheduled_timezone"] = scheduled_timezone

    result = await db.videos.insert_one(video_doc)

    if scheduled_at is not None:
        return str(result.inserted_id)

    await db.users.update_one(
        {"_id": creator_id},
        {"$inc": {"videos_count": 1}}
    )

    try:
        values = item_values({**video_doc, "_id": result.inserted_id})
        req = SetItemValues(str(result.inserted_id), values, cascade_create=True)
        req.timeout = 10000
        await recombee_send(req)
        await db.videos.update_one({"_id": result.inserted_id}, {"$set": {"recombee": True}})
    except Exception:
        # The post itself is saved; a failed sync only delays it reaching recommendations.
        logger.exception("Recombee sync failed for video %s", result.inserted_id)

    return str(result.inserted_id)
