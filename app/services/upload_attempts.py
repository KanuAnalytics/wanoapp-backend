"""Counting posting attempts, to learn why so many never become a post
(Admin > Failed uploads, "Posting attempts"). Nothing here changes what
people see; every write is best-effort and never breaks a request.

The app asks for a Cloudflare upload address before it creates the post. In
November 2026 about 1 address in 7 was never used, but only 5 posts in 60
days actually failed, so most attempts stopped between the address and the
post. Recorded for 30 days (TTL):

- upload_attempts: one per address handed out (Cloudflare uid, who, when,
  file size, phone type from the User-Agent), marked posted_at when a post or
  stitch job names it.
- post_refusals: each time creating a post or stitch job is refused, the
  reason (status and the server's message, or the field that failed
  validation); who, when it's known. No captions or other content are kept.

summary() sorts each unused attempt into: refused (the same person was
refused within 15 minutes), tried again (they posted from a later attempt
within 2 hours) or no post at all.
"""

import logging
from datetime import datetime, timedelta
from typing import Iterable, Optional

from bson import ObjectId

logger = logging.getLogger(__name__)

KEEP_DAYS = 30
REFUSED_WITHIN = timedelta(minutes=15)
RETRIED_WITHIN = timedelta(hours=2)
SKIP_FOLDERS = ("diag", "studio")
_indexed = {"done": False}


def platform(user_agent: Optional[str]) -> str:
    ua = (user_agent or "").lower()
    if "okhttp" in ua or "android" in ua:
        return "android"
    if "cfnetwork" in ua or "darwin" in ua or "iphone" in ua or "ios" in ua:
        return "ios"
    return "other"


async def _ensure_indexes(db) -> None:
    if _indexed["done"]:
        return
    _indexed["done"] = True
    try:
        for coll in (db.upload_attempts, db.post_refusals):
            await coll.create_index("at", expireAfterSeconds=KEEP_DAYS * 86400)
        await db.post_refusals.create_index([("user", 1), ("at", 1)])
        await db.upload_attempts.create_index([("user", 1), ("at", 1)])
    except Exception:
        logger.warning("upload_attempts indexes not created", exc_info=True)


def _oid(user) -> Optional[ObjectId]:
    return ObjectId(user) if isinstance(user, str) and ObjectId.is_valid(user) else None


async def note_address(db, uid: Optional[str], user, size: int, folder: Optional[str], user_agent: Optional[str],
                       anonymous: bool = False, editor: bool = False):
    if not uid:
        return
    try:
        await _ensure_indexes(db)
        await db.upload_attempts.update_one(
            {"_id": uid},
            {"$setOnInsert": {"user": _oid(user), "at": datetime.utcnow(), "size": int(size or 0),
                              "folder": (folder or "videos")[:20], "platform": platform(user_agent),
                              **({"anonymous": True, "editor": editor} if anonymous else {})}},
            upsert=True)
    except Exception:
        logger.warning("upload attempt not noted", exc_info=True)


async def note_posted(db, uids: Iterable[Optional[str]]):
    ids = [u for u in uids if u]
    if not ids:
        return
    try:
        await db.upload_attempts.update_many({"_id": {"$in": ids}, "posted_at": {"$exists": False}},
                                             {"$set": {"posted_at": datetime.utcnow()}})
    except Exception:
        logger.warning("posted attempt not noted", exc_info=True)


async def note_refused(db, user, path: str, status: int, reason: str):
    try:
        await _ensure_indexes(db)
        await db.post_refusals.insert_one({"user": _oid(user), "at": datetime.utcnow(), "path": path[:40],
                                           "status": int(status), "reason": " ".join(str(reason).split())[:120]})
    except Exception:
        logger.warning("post refusal not noted", exc_info=True)


async def summary(db, days: int = 7, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    since = now - timedelta(days=days)
    attempts = await db.upload_attempts.find({"at": {"$gte": since}, "folder": {"$nin": list(SKIP_FOLDERS)}}).to_list(20000)
    refusals = await db.post_refusals.find({"at": {"$gte": since}}).to_list(20000)
    by_user_ref = {}
    for r in refusals:
        by_user_ref.setdefault(r.get("user"), []).append(r["at"])
    by_user_posted = {}
    for a in attempts:
        if a.get("posted_at"):
            by_user_posted.setdefault(a.get("user"), []).append(a["at"])
    out = {"days": days, "attempts": len(attempts), "posted": 0, "refused": 0, "tried_again": 0, "no_post": 0,
           "by_platform": {}, "size_mb": {"posted": None, "not_posted": None}}
    sizes = {"posted": [], "not_posted": []}
    for a in attempts:
        p = out["by_platform"].setdefault(a.get("platform") or "other", {"attempts": 0, "posted": 0})
        p["attempts"] += 1
        if a.get("posted_at"):
            out["posted"] += 1
            p["posted"] += 1
            sizes["posted"].append(a.get("size") or 0)
            continue
        sizes["not_posted"].append(a.get("size") or 0)
        u, t = a.get("user"), a["at"]
        if u is not None and any(t <= x <= t + REFUSED_WITHIN for x in by_user_ref.get(u, [])):
            out["refused"] += 1
        elif u is not None and any(t < x <= t + RETRIED_WITHIN for x in by_user_posted.get(u, [])):
            out["tried_again"] += 1
        else:
            out["no_post"] += 1
    for k, v in sizes.items():
        v.sort()
        out["size_mb"][k] = round(v[len(v) // 2] / 1e6, 1) if v else None
    reasons = {}
    for r in refusals:
        key = f'{r.get("status")}: {r.get("reason")}'
        reasons[key] = reasons.get(key, 0) + 1
    out["refusal_reasons"] = [{"reason": k, "count": n} for k, n in sorted(reasons.items(), key=lambda x: -x[1])[:15]]
    out["refusals"] = len(refusals)
    # Upload addresses handed out without an account: the video editor's
    # (matched to a running stitch job) and anyone else's. "Others" must be 0
    # before the upload_locked switch goes on.
    anon = await db.upload_attempts.find({"at": {"$gte": since}, "anonymous": True},
                                         {"editor": 1}).to_list(20000)
    out["without_account"] = {"video_editor": sum(1 for a in anon if a.get("editor")),
                              "others": sum(1 for a in anon if not a.get("editor"))}
    return out
