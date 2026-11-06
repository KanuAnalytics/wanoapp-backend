"""
Safety check for new posts.

Reports. A report counts ("flag") only when it comes from a trusted account:
at least TRUSTED_ACCOUNT_DAYS old, active, not the post's creator, and under
REPORTS_PER_DAY counted reports in the last 24 h. Brand-new or mass-reporting
accounts can still report (it is recorded and emailed) but can't move a post.

- 1 flag: the post leaves the first-viewer pool (creators.first_viewers) at
  once. It stays in New, on the profile and in Recombee: one report isn't a verdict.
- HOLD_FLAGS flags: the post is held for review. Its privacy becomes "private"
  (so every public surface drops it: feeds, trending, share pages, sitemap,
  Recombee) and the old privacy is kept in moderation.prev_privacy. The creator
  still sees it, marked under review, and can't change its privacy while held.
  Withdrawn reports never release a hold; only a review does.

Review (admin): restore puts the old privacy back and resets the flags; remove
deletes the post. Pending reports become "dismissed" or "resolved".
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId
from pymongo import ReturnDocument
from recombee_api_client.api_requests import SetItemValues

from app.services.recombee_service import recombee_send

logger = logging.getLogger(__name__)

TRUSTED_ACCOUNT_DAYS = 3
REPORTS_PER_DAY = 20
HOLD_FLAGS = 3

HELD = "held"
FLAGGED = "flagged"
CLEARED = "cleared"
REMOVED = "removed"


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def not_flagged() -> dict:
    """Mongo filter: posts no trusted account has flagged (for the first-viewer pool)."""
    return {"$or": [{"moderation.flags": {"$exists": False}}, {"moderation.flags": {"$lte": 0}}]}


def is_held(video: dict) -> bool:
    return ((video or {}).get("moderation") or {}).get("state") == HELD


async def _sync_privacy(video_id, privacy: str) -> None:
    try:
        req = SetItemValues(str(video_id), {"privacy": privacy}, cascade_create=False)
        req.timeout = 5000
        await recombee_send(req)
    except Exception:
        logger.exception("Could not sync privacy of video %s to Recombee", video_id)


async def _trusted(db, reporter_id: str, video: dict) -> bool:
    if str(video.get("creator_id")) == str(reporter_id):
        return False
    user = await db.users.find_one({"_id": ObjectId(reporter_id)}, {"created_at": 1, "is_active": 1})
    created = _aware((user or {}).get("created_at"))
    if not user or user.get("is_active") is False or not isinstance(created, datetime):
        return False
    if created > _now() - timedelta(days=TRUSTED_ACCOUNT_DAYS):
        return False
    since = (_now() - timedelta(hours=24)).replace(tzinfo=None)
    counted_today = await db.reports.count_documents(
        {"reporter_id": ObjectId(reporter_id), "counts": True, "created_at": {"$gte": since}})
    return counted_today < REPORTS_PER_DAY


async def on_report(db, video: dict, reporter_id: str, report_id: str) -> bool:
    """Called after a report is saved. Returns True if it counted."""
    if not await _trusted(db, reporter_id, video):
        return False
    await db.reports.update_one({"_id": ObjectId(report_id)}, {"$set": {"counts": True}})
    after = await db.videos.find_one_and_update(
        {"_id": video["_id"]},
        {"$inc": {"moderation.flags": 1}, "$set": {"moderation.last_flag_at": _now()}},
        return_document=ReturnDocument.AFTER,
    )
    if not after:
        return True
    state = (after.get("moderation") or {}).get("state")
    if state not in (HELD, REMOVED) and (after["moderation"].get("flags") or 0) < HOLD_FLAGS:
        await db.videos.update_one({"_id": video["_id"], "moderation.state": {"$nin": [HELD, REMOVED]}},
                                   {"$set": {"moderation.state": FLAGGED}})
    if (after["moderation"].get("flags") or 0) >= HOLD_FLAGS and state not in (HELD, REMOVED):
        await hold(db, video["_id"])
    return True


async def hold(db, video_id) -> bool:
    """Hold for review exactly once, even with simultaneous reports."""
    before = await db.videos.find_one_and_update(
        {"_id": video_id, "moderation.state": {"$nin": [HELD, REMOVED]}},
        {"$set": {"moderation.state": HELD, "moderation.held_at": _now(), "privacy": "private"}},
        return_document=ReturnDocument.BEFORE,
    )
    if not before:
        return False
    await db.videos.update_one({"_id": video_id},
                               {"$set": {"moderation.prev_privacy": before.get("privacy") or "public"}})
    await _sync_privacy(video_id, "private")
    logger.warning("Video %s held for review after %s trusted reports", video_id, HOLD_FLAGS)
    return True


async def on_withdraw(db, report: dict) -> None:
    """A counted report was withdrawn: lower the flags. Never releases a hold."""
    if not report.get("counts"):
        return
    await db.reports.update_one({"_id": report["_id"]}, {"$set": {"counts": False}})
    after = await db.videos.find_one_and_update(
        {"_id": report["video_id"], "moderation.flags": {"$gt": 0}},
        {"$inc": {"moderation.flags": -1}},
        return_document=ReturnDocument.AFTER,
    )
    if after and (after.get("moderation") or {}).get("flags", 0) <= 0 \
            and (after.get("moderation") or {}).get("state") == FLAGGED:
        await db.videos.update_one({"_id": after["_id"], "moderation.state": FLAGGED},
                                   {"$unset": {"moderation.state": ""}})


async def decide(db, video: dict, action: str, admin_id: str, note: Optional[str] = None) -> dict:
    now = _now()
    mod = video.get("moderation") or {}
    review = {"moderation.reviewed_by": ObjectId(admin_id), "moderation.reviewed_at": now,
              "moderation.note": note}
    if action == "restore":
        update = {"$set": {**review, "moderation.state": CLEARED, "moderation.flags": 0}}
        if mod.get("state") == HELD:
            update["$set"]["privacy"] = mod.get("prev_privacy") or "public"
        await db.videos.update_one({"_id": video["_id"]}, update)
        await db.reports.update_many({"video_id": video["_id"], "status": "pending", "is_active": True},
                                     {"$set": {"status": "dismissed", "counts": False, "reviewed_by": ObjectId(admin_id),
                                               "reviewed_at": now.replace(tzinfo=None)}})
        if mod.get("state") == HELD:
            await _sync_privacy(video["_id"], update["$set"]["privacy"])
    else:
        from app.services.video_deletion import soft_delete_video
        await soft_delete_video(db, video)
        await db.videos.update_one({"_id": video["_id"]}, {"$set": {**review, "moderation.state": REMOVED}})
        await db.reports.update_many({"video_id": video["_id"], "status": "pending", "is_active": True},
                                     {"$set": {"status": "resolved", "reviewed_by": ObjectId(admin_id),
                                               "reviewed_at": now.replace(tzinfo=None)}})
    return await db.videos.find_one({"_id": video["_id"]}) or {}
