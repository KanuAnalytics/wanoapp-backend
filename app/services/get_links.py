"""Creator invite links: wanoafrica.com/get?by=<name> (Nov 2026).

The website sends each phone to the right store and tells us about the tap,
so Admin > Kampala growth can show which creators bring people in. A tap is
counted once per phone per link per day. Installs themselves show in Play
Console / App Store Connect under the same campaign name (utm_campaign / ct).
"""

import hashlib
import re
from datetime import datetime, timedelta
from typing import Optional

PLATFORMS = ("android", "ios", "other")
SEEN_DAYS = 2
_BY = re.compile(r"[^a-z0-9._-]")


def clean_by(value) -> str:
    """A link name: lowercase letters, digits, dot, dash, underscore; at most 40."""
    if not isinstance(value, str):
        return ""
    return _BY.sub("", value.strip().lower().lstrip("@"))[:40]


async def record(db, by, platform, who: str = "", now: Optional[datetime] = None) -> bool:
    by = clean_by(by) or "none"
    platform = platform if platform in PLATFORMS else "other"
    now = now or datetime.utcnow()
    day = now.strftime("%Y-%m-%d")
    key = hashlib.sha256(f"{who}|{by}|{day}".encode()).hexdigest()[:32]
    try:
        await db.get_link_seen.insert_one({"_id": key, "at": now})
    except Exception:          # already counted today (duplicate key)
        return False
    await db.get_link_daily.update_one(
        {"_id": f"{day}:{by}"},
        {"$inc": {platform: 1, "total": 1}, "$setOnInsert": {"by": by, "day": day}},
        upsert=True)
    return True


async def summary(db, days: int = 30, now: Optional[datetime] = None) -> list:
    now = now or datetime.utcnow()
    since = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = []
    async for d in db.get_link_daily.aggregate([
        {"$match": {"day": {"$gte": since}}},
        {"$group": {"_id": "$by", **{p: {"$sum": {"$ifNull": [f"${p}", 0]}} for p in PLATFORMS},
                    "total": {"$sum": "$total"}, "last": {"$max": "$day"}}},
        {"$sort": {"total": -1}},
        {"$limit": 200},
    ]):
        rows.append({"by": d["_id"], **{p: d[p] for p in PLATFORMS}, "total": d["total"], "last": d["last"]})
    return rows
