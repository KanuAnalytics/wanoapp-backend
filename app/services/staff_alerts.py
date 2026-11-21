"""Pushes to staff about urgent safety reports ("Involves a child").

The push names nobody and shows nothing: lock screens are seen by others.
Each reported thing alerts staff once; at most ALERTS_PER_HOUR pushes an hour
in total, so a flood of false reports can't flood staff phones (the reports
themselves are all still at the top of Admin)."""

import asyncio
import logging
from datetime import datetime, timedelta

from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

TITLE = "Wano safety"
BODY = "1 urgent safety report. Open Admin to review."
ALERTS_PER_HOUR = 6
KEEP_DAYS = 30


async def staff_tokens(db) -> list:
    out = []
    async for u in db.users.find({"user_type": "admin", "is_active": True}, {"expo_push_tokens": 1}):
        out += [t for t in (u.get("expo_push_tokens") or []) if isinstance(t, str) and t][:5]
    return out


async def urgent(db, key: str) -> bool:
    """Alert staff about one urgent report. True if a push went out. Never raises.
    Only with safe moderation on (safe_ban), like everything new for staff."""
    now = datetime.utcnow()
    try:
        from app.services import safe_mod
        if not await safe_mod.on(db):
            return False
    except Exception:
        return False
    try:
        await db.staff_alerts.insert_one({"_id": key[:120], "at": now})
    except DuplicateKeyError:
        return False
    except Exception:
        logger.warning("staff alert not recorded", exc_info=True)
        return False
    try:
        try:
            await db.staff_alerts.create_index("at", expireAfterSeconds=KEEP_DAYS * 86400)
        except Exception:
            pass
        recent = await db.staff_alerts.count_documents({"at": {"$gte": now - timedelta(hours=1)}, "pushed": True})
        if recent >= ALERTS_PER_HOUR:
            return False
        tokens = await staff_tokens(db)
        if not tokens:
            return False
        from app.services.expo import send_push_batch_data
        msgs = [{"to": t, "title": TITLE, "body": BODY, "sound": "default",
                 "data": {"type": "staff_urgent"}} for t in tokens]
        await asyncio.to_thread(send_push_batch_data, msgs)
        await db.staff_alerts.update_one({"_id": key[:120]}, {"$set": {"pushed": True}})
        return True
    except Exception:
        logger.warning("staff alert push failed", exc_info=True)
        return False
