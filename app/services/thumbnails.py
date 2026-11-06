"""
Real thumbnails. Posts saved without a thumbnail, or with the grey placeholder,
show the frame Cloudflare Stream makes for every video instead. Only for
videos on Cloudflare's own hosts (videodelivery.net, customer-*.cloudflarestream.com)
with a real 32-hex video id; anything else keeps what it has.

Used when feeds, stats and Recombee read a post, when a video becomes ready
(upload_video.mark_stream_ready), and by a one-off backfill (POST
/api/v1/recombee/fix-thumbnails, admin, dry run by default).
"""

import logging
import re
from typing import Optional, Tuple
from urllib.parse import urlparse

from app.services.video_posts import DEFAULT_THUMBNAIL

logger = logging.getLogger(__name__)

STREAM_HOSTS = ("videodelivery.net",)
STREAM_HOST_SUFFIX = ".cloudflarestream.com"
UID_RE = re.compile(r"^[a-f0-9]{32}$")
_PATH_UID = re.compile(r"^/([a-f0-9]{32})(/|$)")


def stream_ref(video: dict) -> Tuple[Optional[str], Optional[str]]:
    """(host, uid) of the post's Cloudflare Stream video, or (None, None)."""
    for url in (video.get("remoteUrl_CF"), video.get("remoteUrl")):
        if not isinstance(url, str):
            continue
        try:
            p = urlparse(url)
        except ValueError:
            continue
        host = (p.hostname or "").lower()
        if p.scheme != "https" or p.username or p.password or p.port:
            continue
        if not (host in STREAM_HOSTS or (host.endswith(STREAM_HOST_SUFFIX) and host.count(".") == 2)):
            continue
        m = _PATH_UID.match(p.path or "")
        if m:
            return host, m.group(1)
    return None, None


def stream_thumbnail(host: str, uid: str) -> str:
    return f"https://{host}/{uid}/thumbnails/thumbnail.jpg?time=1s&height=720"


def is_placeholder(url) -> bool:
    return not isinstance(url, str) or not url.strip() or url == DEFAULT_THUMBNAIL \
        or "thumbnail_placeholder" in url


def best_thumbnail(video: dict, current=None) -> Optional[str]:
    """The thumbnail to show: the post's own unless it is missing or the
    placeholder and Cloudflare has a frame. `current` defaults to urls.thumbnail."""
    if current is None:
        urls = video.get("urls") if isinstance(video.get("urls"), dict) else {}
        current = urls.get("thumbnail")
    current = current if isinstance(current, str) else None
    if not is_placeholder(current):
        return current
    if video.get("media_type") == "photo":
        images = video.get("images")
        first = images[0] if isinstance(images, list) and images and isinstance(images[0], str) else None
        return first or current
    host, uid = stream_ref(video)
    return stream_thumbnail(host, uid) if uid else current


async def backfill(db, apply: bool = False, limit: int = 200_000) -> dict:
    """Store the real frame on posts that still have the placeholder or none."""
    q = {"media_type": {"$ne": "photo"},
         "$or": [{"urls.thumbnail": DEFAULT_THUMBNAIL}, {"urls.thumbnail": None},
                 {"urls.thumbnail": {"$exists": False}}, {"urls.thumbnail": ""},
                 {"urls.thumbnail": {"$regex": "thumbnail_placeholder"}}]}
    from pymongo import UpdateOne
    would, fixed, errors = 0, 0, 0
    batch = []

    async def flush():
        nonlocal fixed, errors, batch
        if not batch:
            return
        try:
            res = await db.videos.bulk_write(batch, ordered=False)
            fixed += res.modified_count
        except Exception:
            errors += len(batch)
            logger.exception("Thumbnail backfill batch failed")
        batch = []

    async for v in db.videos.find(q, {"urls": 1, "remoteUrl_CF": 1, "remoteUrl": 1, "media_type": 1}).limit(limit):
        try:
            urls = v.get("urls") if isinstance(v.get("urls"), dict) else {}
            if not is_placeholder(urls.get("thumbnail")):
                continue
            host, uid = stream_ref(v)
            if not uid:
                continue
            would += 1
            if apply:
                batch.append(UpdateOne({"_id": v["_id"]}, {"$set": {"urls": {**urls, "thumbnail": stream_thumbnail(host, uid)}}}))
                if len(batch) >= 500:
                    await flush()
        except Exception:
            errors += 1
            logger.exception("Thumbnail backfill failed for %s", v.get("_id"))
    await flush()
    return {"would_fix": would, "fixed": fixed, "errors": errors, "applied": apply}
