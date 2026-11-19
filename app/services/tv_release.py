"""UTC episode embargoes and the weekly-release worker. Older episodes stay live."""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

log = logging.getLogger(__name__)


def utc(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("Use a date and time with a time zone")
    if value.tzinfo is None:
        raise ValueError("A time zone is required")
    return value.astimezone(timezone.utc)


def stored_time(value):
    # Motor uses naive UTC datetimes by default; API input still requires a zone.
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return utc(value)


def released(ep, now=None):
    if ep.get("is_published") is False:
        return False
    try:
        at = stored_time(ep.get("publish_at"))
    except (ValueError, TypeError):
        return False  # malformed embargo must fail closed
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return at is None or at <= now


def episodes(doc):
    return [e for s in doc.get("seasons") or [] if isinstance(s, dict)
            for e in s.get("episodes") or [] if isinstance(e, dict)]


def preview(ep):
    out = {k: ep.get(k) for k in ("id", "season", "number", "title", "synopsis", "duration_minutes", "thumbnail")}
    at = stored_time(ep.get("publish_at"))
    out["publish_at"] = at.isoformat() if at else None
    return out


async def _notify(db, doc, ep, now):
    from app.services import tv_more, home_ground, growth_notifications
    sent = 0
    async for r in db.tv_reminders.find({"title_id": doc["_id"]}):
        # People subscribing after release should not receive old episode alerts.
        at = stored_time(ep.get("publish_at"))
        subscribed = r.get("at")
        if at and now - at > timedelta(days=7):
            continue
        if subscribed and at and stored_time(subscribed) > at:
            continue
        key = f"{doc['_id']}:{ep['id']}:{r['user_id']}"
        try:
            await db.tv_release_notices.insert_one({"_id": key, "state": "pending", "at": now})
        except DuplicateKeyError:
            pass
        job = await db.tv_release_notices.find_one_and_update(
            {"_id": key, "$or": [{"state": "pending"}, {"state": "sending", "claimed_at": {"$lt": now - timedelta(minutes=5)}}]},
            {"$set": {"state": "sending", "claimed_at": now}}, return_document=ReturnDocument.AFTER)
        if not job:
            continue
        u = await db.users.find_one({"_id": r["user_id"], "is_active": {"$ne": False}})
        rating = tv_more.min_age(doc.get("age_rating"))
        if not u or (rating and home_ground.is_minor(u) and (home_ground.age_of(u.get("date_of_birth")) or 0) < rating):
            await db.tv_release_notices.update_one({"_id": key}, {"$set": {"state": "skipped"}})
            continue
        if growth_notifications._quiet(u, now):
            await db.tv_release_notices.update_one({"_id": key}, {"$set": {"state": "pending"}})
            continue
        toks = tv_more._tokens(u)
        # Retain for retry when the device registers its push token.
        if not toks:
            await db.tv_release_notices.update_one({"_id": key}, {"$set": {"state": "pending"}})
            continue
        msgs = [tv_more._message(t, doc, f"{doc.get('title')} · Episode {ep['number']}",
                    "Your next episode is available. Watch now on Wano TV.") for t in toks]
        try:
            ok = await tv_more._send(msgs)
        except Exception:
            ok = 0
            log.exception("Episode reminder delivery failed")
        await db.tv_release_notices.update_one({"_id": key}, {"$set": {"state": "sent" if ok == len(msgs) else "pending", "sent_at": now if ok == len(msgs) else None}})
        sent += int(ok == len(msgs))
    return sent


async def publish_due(db, now: Optional[datetime] = None):
    from app.services import tv_admin, tv_more
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    count = 0
    async for doc in db.tv_titles.find({"type": "series", "$or": [{"status": "coming_soon"}, {"is_active": True}]}):
        try:
            premiere = stored_time(doc.get("premiere_at"))
        except (ValueError, TypeError):
            log.warning("Invalid premiere time on %s", doc["_id"])
            continue
        if not doc.get("is_active"):
            if not premiere or premiere > now or tv_admin.problems(doc):
                continue
            result = await db.tv_titles.update_one({"_id": doc["_id"], "status": "coming_soon", "premiere_at": doc["premiere_at"]},
                {"$set": {"status": "live", "is_active": True, "status_at": now}})
            if not result.modified_count:
                continue
            doc = {**doc, "is_active": True, "status": "live"}
            tv_more.clear_cache()
            count += 1
        for ep in episodes(doc):
            if ep.get("publish_at") and released(ep, now) and ep.get("video_url") and (not ep.get("cf_uid") or ep.get("cf_ready") is True):
                await _notify(db, doc, ep, now)
    return count


class ReleaseWorker:
    def __init__(self):
        self.task = None
    def start(self):
        self.task = asyncio.create_task(self.run())
    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
    async def run(self):
        from app.core.database import get_database
        while True:
            try:
                await publish_due(get_database())
            except Exception:
                log.exception("Wano TV release check failed")
            await asyncio.sleep(30)

release_worker = ReleaseWorker()
