"""Link-preview pictures with a play button ("share_play" switch).

WhatsApp, iMessage, Facebook and X show a shared post's og:image. TikTok,
Instagram and YouTube previews carry a ▶ so people see it's a video; ours were
a still picture. /og/<id>.jpg takes the post's own thumbnail and draws a play
button on it. Nothing is stored: pictures are kept in memory (a few hundred)
and the response is cacheable for a day.

Only thumbnails on Wano's own hosts are fetched (Cloudflare Stream and our
DigitalOcean Spaces), at most 6 MB, with a short timeout.
"""

import io
import logging
from collections import OrderedDict
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MAX_BYTES = 6 * 1024 * 1024
MAX_SIDE = 720
CACHE_ITEMS = 300
_cache: "OrderedDict[str, bytes]" = OrderedDict()
_ALLOWED = ("videodelivery.net", ".cloudflarestream.com", ".digitaloceanspaces.com")


def allowed(url: Optional[str]) -> bool:
    try:
        p = urlparse(url or "")
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme == "https" and not p.username and bool(host) and (
        host == "videodelivery.net" or any(host.endswith(s) for s in _ALLOWED if s.startswith(".")))


def draw(data: bytes) -> bytes:
    """The picture, at most MAX_SIDE on its longer side, with a play button."""
    from PIL import Image, ImageDraw
    img = Image.open(io.BytesIO(data))
    img.draft("RGB", (MAX_SIDE, MAX_SIDE))
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    w, h = img.size
    r = max(18, int(min(w, h) * 0.14))
    cx, cy = w // 2, h // 2
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(0, 0, 0, 120), outline=(255, 255, 255, 235), width=max(2, r // 12))
    t = int(r * 0.45)
    d.polygon([(cx - t * 0.7, cy - t), (cx - t * 0.7, cy + t), (cx + t * 1.05, cy)], fill=(255, 255, 255, 245))
    out = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=82, optimize=True, progressive=True)
    return buf.getvalue()


async def _fetch(url: str) -> Optional[bytes]:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as c:
            async with c.stream("GET", url) as r:
                if r.status_code != 200:
                    return None
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
    except Exception:
        logger.warning("preview thumbnail fetch failed", exc_info=True)
        return None


async def picture(key: str, thumb_url: str) -> Optional[bytes]:
    """The play-button picture for this post, or None (then the plain thumbnail is used)."""
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    if not allowed(thumb_url):
        return None
    data = await _fetch(thumb_url)
    if not data:
        return None
    try:
        import asyncio
        out = await asyncio.to_thread(draw, data)
    except Exception:
        logger.warning("preview picture could not be drawn", exc_info=True)
        return None
    _cache[key] = out
    while len(_cache) > CACHE_ITEMS:
        _cache.popitem(last=False)
    return out
