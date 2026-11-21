"""One attention limit for everything that pops up ("attention_cap" switch).

What's new, the Wano TV strip, local flyers, quick-post rings and the review
prompt all want the same moment. With the switch on, the app asks for a slot
first (POST /attention/take) and the server gives at most CAP_DAY a day per
person, counted by the person's local day (the app sends its UTC offset, so
the day doesn't turn over in the middle of an African afternoon). The last
slot of the day is kept for the kind an admin put first in the order.
Safety and account notices never ask: they always show. Staff test accounts
are never limited, so they can check new items.
"""

from datetime import datetime, timedelta
from typing import List

from bson import ObjectId

SWITCH = "attention_cap"
KINDS = ("whats_new", "tv_strip", "flyer", "quick_ring", "review_prompt")
CAP_DAY = 3
KEEP_DAYS = 3


async def _on(db) -> bool:
    from app.services import creator_push
    return await creator_push.on(db, SWITCH)


async def settings(db) -> dict:
    cfg = await db.config.find_one({"attention": {"$type": "object"}}, {"attention": 1}) or {}
    a = cfg.get("attention") or {}
    order: List[str] = [k for k in (a.get("order") or []) if k in KINDS]
    order += [k for k in KINDS if k not in order]
    cap = a.get("cap_day") if isinstance(a.get("cap_day"), int) and 1 <= a.get("cap_day") <= 10 else CAP_DAY
    return {"order": order, "cap_day": cap}


def _day(tz_minutes: int) -> str:
    tz = max(-720, min(840, int(tz_minutes or 0)))
    return (datetime.utcnow() + timedelta(minutes=tz)).strftime("%Y%m%d")


async def _exempt(db, me) -> bool:
    from app.services import staff
    u = await db.users.find_one({"_id": ObjectId(str(me))}, {"user_type": 1, "staff_role": 1, "is_active": 1})
    return staff.is_staff(u)


async def available(db, me: str, kind: str, tz: int = 0) -> bool:
    if not await _on(db) or await _exempt(db, me):
        return True
    s = await settings(db)
    doc = await db.attention_used.find_one({"_id": f"{me}:{_day(tz)}"}) or {}
    n = int(doc.get("n") or 0)
    if n >= s["cap_day"]:
        return False
    if n == s["cap_day"] - 1 and kind != s["order"][0]:
        return False
    return True


async def take(db, me: str, kind: str, tz: int = 0) -> bool:
    """Use one slot. True if the thing may show now."""
    if kind not in KINDS:
        return False
    if not await _on(db) or await _exempt(db, me):
        return True
    if not await available(db, me, kind, tz):
        return False
    s = await settings(db)
    key = f"{me}:{_day(tz)}"
    from pymongo.errors import DuplicateKeyError
    try:
        res = await db.attention_used.update_one(
            {"_id": key, "$or": [{"n": {"$lt": s["cap_day"]}}, {"n": {"$exists": False}}]},
            {"$inc": {"n": 1}, "$push": {"kinds": kind}, "$setOnInsert": {"at": datetime.utcnow()}}, upsert=True)
    except DuplicateKeyError:
        return False                      # full: the filter missed and the insert hit the day's record
    try:
        await db.attention_used.create_index("at", expireAfterSeconds=KEEP_DAYS * 86400)
    except Exception:
        pass
    return bool(res.modified_count or res.upserted_id)
