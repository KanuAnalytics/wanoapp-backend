"""Small copies of pictures, so phones on slow or costly data don't download
a 2 MB photo to draw a 40-point circle.

For every picture on our Spaces CDN (profile pictures, post images and
thumbnails, Wano TV posters) this writes WebP copies at a few widths next to
it, at a path the app can work out from the original URL by itself:

    <cdn>/<bucket>/<key>                    the original, untouched
    <cdn>/<bucket>/small/w160/<key>.webp    tiny faces
    <cdn>/<bucket>/small/w320/<key>.webp    avatars
    <cdn>/<bucket>/small/w480/<key>.webp    grids, previews, feed posters
    <cdn>/<bucket>/small/w1080/<key>.webp   full-screen photos

(<key> without its extension.) A picture narrower than a width is copied at
its own size, so every width always exists once a picture is done. The app
falls back to the original if a copy isn't there yet.

A background loop finds pictures to do: what changed recently first (new
posts, profiles edited), then everything older, oldest first, a batch at a
time. Each picture is claimed in `image_variants` before it is worked on, so
several server processes never do the same one twice.
"""

import asyncio
import io
import logging
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Tuple

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.core.config import settings

logger = logging.getLogger(__name__)

WIDTHS = (160, 320, 480, 1080)
QUALITY = 72
MAX_BYTES = 30 * 1024 * 1024          # bigger than this isn't a picture we draw
MAX_PIXELS = 60_000_000               # decompression-bomb guard
BATCH = 25
RECENT = timedelta(hours=6)
RETRY_FAILED_AFTER = timedelta(days=3)
WORKING_STALE = timedelta(minutes=10)
PREFIX = "small"
# Bumped when the server learns to read more pictures; anything that failed
# under an older one is tried again straight away (2: iPhone HEIC photos,
# which failed before pillow-heif was installed, so posts showed black).
DECODER = 2
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp")


# ------------------------------------------------------------------ paths

def source_key(url: Optional[str]) -> Optional[str]:
    """The Spaces key of a picture we can shrink, else None."""
    from app.services.upload_DO import spaces_object_key
    if not isinstance(url, str) or len(url) > 2048:
        return None
    key = spaces_object_key(url.split("?", 1)[0].split("#", 1)[0])
    if not key or ".." in key.split("/") or key.startswith(PREFIX + "/"):
        return None
    if not key.lower().endswith(IMAGE_EXT):
        return None
    return key


def variant_key(key: str, width: int) -> str:
    stem = key.rsplit(".", 1)[0] if "." in key.rsplit("/", 1)[-1] else key
    return f"{PREFIX}/w{width}/{stem}.webp"


def variant_url(url: str, width: int) -> Optional[str]:
    key = source_key(url)
    if not key:
        return None
    return f"{settings.DO_SPACES_CDN_URL}/{settings.DO_SPACES_BUCKET_NAME}/{variant_key(key, width)}"


# ------------------------------------------------------------------ work

def _client():
    from app.services.upload_DO import _spaces_client
    return _spaces_client()


def _fetch(key: str) -> bytes:
    obj = _client().get_object(Bucket=settings.DO_SPACES_BUCKET_NAME, Key=key)
    size = obj.get("ContentLength") or 0
    if size > MAX_BYTES:
        raise ValueError(f"too big ({size} bytes)")
    data = obj["Body"].read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("too big")
    return data


def _put(key: str, data: bytes) -> None:
    _client().put_object(Bucket=settings.DO_SPACES_BUCKET_NAME, Key=key, Body=data, ACL="public-read",
                         ContentType="image/webp", CacheControl="public, max-age=31536000, immutable")


def make_variants(data: bytes, widths: Iterable[int] = WIDTHS) -> List[Tuple[int, bytes]]:
    """[(width, webp bytes)] for each width; pure, for tests."""
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        import pillow_heif  # noqa: F401  (HEIC photos from iPhones, often saved with a .jpg name)
        pillow_heif.register_heif_opener()
    except Exception:
        logger.warning("pillow-heif missing: iPhone HEIC photos can't be shrunk")
    with Image.open(io.BytesIO(data)) as im:
        if im.format == "JPEG":
            im.draft("RGB", (2 * max(widths), 2 * max(widths)))   # decode big JPEGs at reduced size: far less memory
        im.seek(0)                                   # first frame of a GIF
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() or im.mode == "P" else "RGB")
        out = []
        for w in widths:
            img = im
            if im.width > w:
                h = max(1, round(im.height * w / im.width))
                img = im.resize((w, h), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "WEBP", quality=QUALITY, method=4)
            out.append((w, buf.getvalue()))
        return out


async def process(db, key: str) -> bool:
    """Shrink one picture (already claimed). True when its copies exist."""
    now = datetime.utcnow()
    try:
        data = await asyncio.to_thread(_fetch, key)
        variants = await asyncio.to_thread(make_variants, data)
        for w, blob in variants:
            await asyncio.to_thread(_put, variant_key(key, w), blob)
        await db.image_variants.update_one({"_id": key}, {"$set": {
            "state": "done", "at": now, "bytes": len(data),
            "sizes": {f"w{w}": len(b) for w, b in variants}}, "$unset": {"error": ""}})
        return True
    except Exception as e:                            # noqa: BLE001 — one bad file never stops the loop
        await db.image_variants.update_one({"_id": key}, {"$set": {
            "state": "failed", "at": now, "error": str(e)[:300], "decoder": DECODER}})
        return False


async def claim(db, key: str) -> bool:
    now = datetime.utcnow()
    try:
        await db.image_variants.insert_one({"_id": key, "state": "working", "at": now})
        return True
    except DuplicateKeyError:
        pass
    # Take back a failed one after a while, or one a crashed process left half done.
    r = await db.image_variants.update_one(
        {"_id": key, "$or": [{"state": "failed", "at": {"$lt": now - RETRY_FAILED_AFTER}},
                             {"state": "failed", "decoder": {"$not": {"$gte": DECODER}}},
                             {"state": "working", "at": {"$lt": now - WORKING_STALE}}]},
        {"$set": {"state": "working", "at": now}})
    return r.modified_count == 1


# ------------------------------------------------------------------ finding pictures

def _urls_of_video(v: dict) -> List[str]:
    out = []
    urls = v.get("urls") if isinstance(v.get("urls"), dict) else {}
    for u in (urls.get("thumbnail"), v.get("thumbnail")):
        if isinstance(u, str):
            out.append(u)
    imgs = v.get("images")
    if isinstance(imgs, list):
        out += [u for u in imgs[:20] if isinstance(u, str)]
    return out


def _urls_of_tv(t: dict) -> List[str]:
    """Every picture of a Wano TV title the app shows: poster, backdrop, the
    trailer's thumbnail (the lists use it) and episode thumbnails."""
    out = [t.get("poster"), t.get("backdrop")]
    trailer = t.get("trailer") if isinstance(t.get("trailer"), dict) else {}
    out.append(trailer.get("thumbnail"))
    for season in t.get("seasons") or []:
        for ep in (season.get("episodes") or []) if isinstance(season, dict) else []:
            if isinstance(ep, dict):
                out.append(ep.get("thumbnail"))
    return [u for u in out if isinstance(u, str)][:60]


async def _todo(db, urls: Iterable[str]) -> List[str]:
    keys = list(dict.fromkeys(k for k in (source_key(u) for u in urls) if k))
    if not keys:
        return []
    done = {d["_id"] async for d in db.image_variants.find(
        {"_id": {"$in": keys}, "$or": [{"state": "done"}, {"state": "working"},
                                       {"state": "failed", "decoder": {"$gte": DECODER},
                                        "at": {"$gte": datetime.utcnow() - RETRY_FAILED_AFTER}}]},
        {"_id": 1})}
    return [k for k in keys if k not in done]


async def _recent_urls(db, since: datetime) -> List[str]:
    urls: List[str] = []
    async for v in db.videos.find({"created_at": {"$gte": since}},
                                  {"urls.thumbnail": 1, "thumbnail": 1, "images": 1}).limit(500):
        urls += _urls_of_video(v)
    async for u in db.users.find({"updated_at": {"$gte": since}, "profile_picture": {"$type": "string"}},
                                 {"profile_picture": 1}).limit(500):
        urls.append(u["profile_picture"])
    return urls


async def _backfill_urls(db, coll: str, want: int) -> List[str]:
    """Walk a collection oldest first from a saved place; wraps when done."""
    state = await db.config.find_one({"_id": f"small_images:{coll}"}) or {}
    after = state.get("after") if isinstance(state.get("after"), ObjectId) else None
    q = {"_id": {"$gt": after}} if after else {}
    proj = {"profile_picture": 1} if coll == "users" else \
        {"poster": 1, "backdrop": 1, "trailer.thumbnail": 1, "seasons.episodes.thumbnail": 1} if coll == "tv_titles" else \
        {"image": 1} if coll == "tv_genres" else {"urls.thumbnail": 1, "thumbnail": 1, "images": 1}
    docs = await db[coll].find(q, proj).sort("_id", 1).limit(want).to_list(want)
    urls: List[str] = []
    for d in docs:
        if coll == "users":
            urls.append(d.get("profile_picture"))
        elif coll == "tv_titles":
            urls += _urls_of_tv(d)
        elif coll == "tv_genres":
            urls.append(d.get("image"))
        else:
            urls += _urls_of_video(d)
    last = docs[-1]["_id"] if docs else None
    upd = {"after": last, "at": datetime.utcnow()}
    if last is None and after is not None:
        upd["finished"] = True                         # walked everything once; stays true
    await db.config.update_one({"_id": f"small_images:{coll}"}, {"$set": upd}, upsert=True)
    return [u for u in urls if isinstance(u, str)]


async def sweep(db, batch: int = BATCH) -> dict:
    """One round: recent pictures first, then a slice of the backlog."""
    keys = await _todo(db, await _recent_urls(db, datetime.utcnow() - RECENT))
    # Pictures that failed before the server could read them (e.g. HEIC) come next.
    if len(keys) < batch:
        keys += [d["_id"] async for d in db.image_variants.find(
            {"state": "failed", "decoder": {"$not": {"$gte": DECODER}}}, {"_id": 1}).limit(batch)
            if d["_id"] not in keys]
    for coll in ("users", "videos", "tv_titles", "tv_genres"):
        if len(keys) >= batch:
            break
        keys += [k for k in await _todo(db, await _backfill_urls(db, coll, 60)) if k not in keys]
    done = failed = 0
    for key in keys[:batch]:
        if not await claim(db, key):
            continue
        if await process(db, key):
            done += 1
        else:
            failed += 1
    return {"done": done, "failed": failed, "queued": max(0, len(keys) - batch)}


async def status(db) -> dict:
    counts = {s: await db.image_variants.count_documents({"state": s}) for s in ("done", "failed", "working")}
    agg = await db.image_variants.aggregate([
        {"$match": {"state": "done", "bytes": {"$gt": 0}}},
        {"$group": {"_id": None, "orig": {"$sum": "$bytes"}, "w160": {"$sum": "$sizes.w160"},
                    "w320": {"$sum": "$sizes.w320"}, "w480": {"$sum": "$sizes.w480"}, "n": {"$sum": 1}}}]).to_list(1)
    walks = {d["_id"].split(":", 1)[1]: bool(d.get("finished")) async for d in
             db.config.find({"_id": {"$regex": "^small_images:"}}, {"finished": 1})}
    return {**counts, "backlog_walked": walks, "totals": (agg[0] if agg else None)}


class SmallImagesLoop:
    def __init__(self):
        self._task: Optional[asyncio.Task] = None

    async def _run(self):
        from app.core.database import get_database
        while True:
            pause = 20
            try:
                r = await sweep(get_database())
                if not (r["done"] or r["failed"]):
                    pause = 120                        # nothing to do: look again in a while
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("small_images sweep failed")
                pause = 120
            await asyncio.sleep(pause)

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None


small_images_loop = SmallImagesLoop()
