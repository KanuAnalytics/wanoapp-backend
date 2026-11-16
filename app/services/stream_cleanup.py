"""Cloudflare Stream videos that no post uses (Admin > Video storage).

Every stored minute is billed every month. The Nov 2026 streaming report found
~490 minutes (6%) in videos no public post plays: uploads that never became a
post, broken uploads, leftovers. This lists them so an admin can look first
and then delete them. Nothing is ever deleted without that.

A video is listed only when ALL of these hold:
  - Cloudflare has had it for at least MIN_AGE_DAYS (an upload being retried
    or still waiting to be posted is never touched);
  - it isn't the shared deleted-video placeholder;
  - no post (public, private, scheduled, trial, inactive), draft or unfinished
    stitch job mentions it (video_deletion.is_stream_uid_referenced, the same
    check deleting a post uses).
Deleting re-checks each video against the posts first, and skips any that a
post started using since the list was made.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)

MIN_AGE_DAYS = 7
PAGE = 1000
MAX_PAGES = 200
STATE_ID = "stream_cleanup"
MAX_DELETE = 2000


def _api() -> str:
    return f"https://api.cloudflare.com/client/v4/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/stream"


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.CLOUDFLARE_STREAM_API_TOKEN}"}


def _list_page(start: Optional[str]) -> List[dict]:
    params = {"asc": "true", "limit": str(PAGE)}
    if start:
        params["start"] = start
    r = requests.get(_api(), headers=_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("result") or []


def list_all() -> List[dict]:
    """Every video in the account, oldest first (synchronous; run in a thread)."""
    out: Dict[str, dict] = {}
    start = None
    for _ in range(MAX_PAGES):
        page = _list_page(start)
        new = [v for v in page if v.get("uid") and v["uid"] not in out]
        for v in new:
            out[v["uid"]] = v
        if len(page) < PAGE or not new:
            break
        start = page[-1].get("created")
    return list(out.values())


def _created(v: dict) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(v.get("created")).replace("Z", "+00:00"))
    except Exception:
        return None


def _row(v: dict) -> dict:
    meta = v.get("meta") if isinstance(v.get("meta"), dict) else {}
    status = v.get("status") if isinstance(v.get("status"), dict) else {}
    dur = v.get("duration")
    return {
        "uid": v["uid"],
        "created": v.get("created"),
        "seconds": round(dur) if isinstance(dur, (int, float)) and dur > 0 else 0,
        "state": status.get("state"),
        "name": str(meta.get("name") or "")[:120],
        "thumbnail": v.get("thumbnail"),
        "size": v.get("size"),
    }


async def scan(db, videos: Optional[Iterable[dict]] = None, now: Optional[datetime] = None) -> dict:
    """Find unused videos and save the list. `videos` (tests) replaces the Cloudflare listing."""
    from app.services.upload_DO import PROTECTED_STREAM_UIDS
    from app.services.video_deletion import is_stream_uid_referenced

    now = now or datetime.now(timezone.utc)
    if videos is None:
        videos = await asyncio.to_thread(list_all)
    videos = list(videos)
    cutoff = now - timedelta(days=MIN_AGE_DAYS)

    # One pass over the posts for the UIDs they store, then the full check
    # (URLs of older posts, drafts, stitch jobs) only for what's left.
    stored = set()
    async for d in db.videos.find({"stream_uid": {"$type": "string"}}, {"stream_uid": 1}):
        stored.add(d["stream_uid"])

    unused, total_seconds = [], 0
    for v in videos:
        uid = v.get("uid")
        dur = v.get("duration") if isinstance(v.get("duration"), (int, float)) else 0
        total_seconds += max(0, dur or 0)
        created = _created(v)
        if not uid or uid in PROTECTED_STREAM_UIDS or uid in stored:
            continue
        if created is None or created > cutoff:
            continue
        if await is_stream_uid_referenced(db, uid):
            continue
        unused.append(_row(v))

    unused.sort(key=lambda r: r["created"] or "")
    result = {
        "scanned_at": now.replace(tzinfo=None),
        "videos": len(videos),
        "minutes": round(total_seconds / 60),
        "unused": len(unused),
        "unused_minutes": round(sum(r["seconds"] for r in unused) / 60),
        "items": unused,
    }
    await db.config.update_one({"_id": STATE_ID}, {"$set": result}, upsert=True)
    return result


async def last_scan(db) -> dict:
    doc = await db.config.find_one({"_id": STATE_ID}) or {}
    doc.pop("_id", None)
    return doc


def _delete(uid: str) -> None:
    from app.services.upload_DO import delete_stream_video
    delete_stream_video(uid)


async def delete(db, uids: Iterable[str], admin: str) -> dict:
    """Delete videos from the last scan. Anything not on that list, or used by
    a post since, is skipped."""
    from app.services import admin_log
    from app.services.upload_DO import PROTECTED_STREAM_UIDS
    from app.services.video_deletion import is_stream_uid_referenced

    scanned = await last_scan(db)
    listed = {r["uid"]: r for r in scanned.get("items") or []}
    deleted, skipped, failed = [], [], []
    for uid in list(dict.fromkeys(u for u in uids if isinstance(u, str)))[:MAX_DELETE]:
        if uid not in listed or uid in PROTECTED_STREAM_UIDS:
            skipped.append(uid)
            continue
        if await is_stream_uid_referenced(db, uid) or await db.videos.find_one({"stream_uid": uid}, {"_id": 1}):
            skipped.append(uid)
            continue
        try:
            await asyncio.to_thread(_delete, uid)
            deleted.append(uid)
        except Exception as e:                       # noqa: BLE001 — one failure never stops the rest
            logger.warning("Could not delete stream %s: %s", uid, e)
            failed.append(uid)
    if deleted:
        left = [r for r in scanned.get("items") or [] if r["uid"] not in set(deleted)]
        await db.config.update_one({"_id": STATE_ID}, {"$set": {
            "items": left, "unused": len(left), "unused_minutes": round(sum(r["seconds"] for r in left) / 60)}})
    seconds = sum(listed[u]["seconds"] for u in deleted)
    await admin_log.record(db, admin, "stream_cleanup", {"deleted": len(deleted), "minutes": round(seconds / 60),
                                                        "skipped": len(skipped), "failed": len(failed)})
    return {"deleted": len(deleted), "minutes_freed": round(seconds / 60), "skipped": skipped, "failed": failed}
