"""Photo posts the rules would refuse, counted but not refused (yet).

The app lets people pick up to 10 photos, but the server never checked the
number or where the photos are stored. Refusing straight away could break an
older app's uploads, so for now each such post is noted here, with the reasons
and the web addresses' hosts only (no full links), and admins read the counts.
"""

from datetime import datetime, timedelta
from typing import List
from urllib.parse import urlparse

from bson import ObjectId

MAX_PHOTOS = 10
KEEP_DAYS = 60


def _hosts() -> set:
    from app.core.config import settings
    out = set()
    for u in (getattr(settings, "DO_SPACES_CDN_URL", None), getattr(settings, "DO_SPACES_ENDPOINT", None)):
        h = urlparse(u).hostname if isinstance(u, str) else None
        if h:
            out.add(h)
            bucket = getattr(settings, "DO_SPACES_BUCKET_NAME", None)
            if bucket and not h.startswith(bucket + "."):
                out.add(f"{bucket}.{h}")
    return out


def _ours(url: str) -> bool:
    """A photo on Wano's own storage (its CDN or the storage itself), over https."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    return (urlparse(url).hostname or "") in _hosts()


def problems(images: List[str]) -> dict:
    out = {}
    if len(images) > MAX_PHOTOS:
        out["too_many"] = len(images)
    elsewhere = [u for u in images if not _ours(u)]
    if elsewhere:
        out["elsewhere"] = len(elsewhere)
        out["hosts"] = sorted({(urlparse(u).hostname or "?")[:80] for u in elsewhere if isinstance(u, str)})[:5]
    return out


async def note(db, user_id: str, images: List[str]) -> dict:
    found = problems(list(images or []))
    if found:
        try:
            await db.photo_checks.insert_one({"at": datetime.utcnow(), "user_id": ObjectId(user_id), **found})
        except Exception:
            pass        # counting must never break a post
    return found


async def summary(db, days: int = 7) -> dict:
    since = datetime.utcnow() - timedelta(days=days)
    rows = await db.photo_checks.find({"at": {"$gte": since}}).to_list(2000)
    hosts: dict = {}
    for r in rows:
        for h in r.get("hosts") or []:
            hosts[h] = hosts.get(h, 0) + 1
    posts = await db.videos.count_documents({"media_type": "photo", "created_at": {"$gte": since}})
    return {"days": days, "photo_posts": posts, "would_refuse": len(rows),
            "too_many": sum(1 for r in rows if r.get("too_many")),
            "elsewhere": sum(1 for r in rows if r.get("elsewhere")),
            "hosts": sorted(hosts.items(), key=lambda x: -x[1])[:10]}
