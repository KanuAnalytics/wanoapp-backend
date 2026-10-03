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

from app.core.database import get_database
from app.services.recombee_service import recombee_send

logger = logging.getLogger(__name__)

DEFAULT_THUMBNAIL = "https://wano-africadev.lon1.digitaloceanspaces.com/wanoafrica-dospaces-key/profile-pictures/thumbnail_placeholder.png"


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
) -> str:
    """Saves the post, counts it on the user, and syncs it to Recombee. Returns the new video's id."""
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
        "media_type": media_type,
        "images": images or [],
        "type": media_type,
        # Standard fields
        "hashtags": hashtags,
        "categories": [],
        "remix_enabled": True,
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
        "is_remix": False,
        "remix_count": 0,
        "country": user.get("localization", {}).get("country", "NG"),
        "language": user.get("localization", {}).get("languages", ["en"])[0]
    }

    if supports_landscape is not None:
        video_doc["supports_landscape"] = supports_landscape

    result = await db.videos.insert_one(video_doc)

    await db.users.update_one(
        {"_id": creator_id},
        {"$inc": {"videos_count": 1}}
    )

    try:
        values = {
            "creator_id": str(video_doc["creator_id"]),
            "description": video_doc.get("description") or "",
            "video_type": video_doc.get("video_type") or "",
            "duration": float(video_doc.get("duration") or 0.0),
            "thumbnail": video_doc.get("urls", {}).get("thumbnail") or "",
            "hashtags": video_doc.get("hashtags") or [],
            "is_active": True,
            "supports_landscape": bool(video_doc.get("supports_landscape", False)),
            "privacy": video_doc.get("privacy") or "public",
            "created_at": video_doc["created_at"].isoformat(),
            # v2 (Recombee) feed filters on 'is_ready_to_stream' == true --
            # photo posts need this true immediately since nothing else
            # (e.g. a stream-ready webhook) will ever flip it later.
            "is_ready_to_stream": bool(video_doc.get("isReadyToStream", False)),
            "media_type": video_doc.get("media_type", "video"),
        }
        req = SetItemValues(str(result.inserted_id), values, cascade_create=True)
        req.timeout = 10000
        await recombee_send(req)
        await db.videos.update_one({"_id": result.inserted_id}, {"$set": {"recombee": True}})
    except Exception:
        # The post itself is saved; a failed sync only delays it reaching recommendations.
        logger.exception("Recombee sync failed for video %s", result.inserted_id)

    return str(result.inserted_id)
