"""
Wano TV popularity from real viewing.

A play counts once per viewer per title per day (logged in: the account;
logged out: the device id). Logged-out plays from one network are capped per
title per day, so a script cycling device ids can't push a title up.

Daily totals live in tv_title_daily. A title's score is its plays over the
last WINDOW_DAYS, each day weighted by half every HALF_LIFE_DAYS, plus the
editorial `popularity` boost set in the catalogue. Cached CACHE_SECONDS.
"""

import time
from datetime import datetime, timedelta
from typing import Dict, Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

WINDOW_DAYS = 14
HALF_LIFE_DAYS = 7
ANON_PER_IP_PER_DAY = 30
CACHE_SECONDS = 300

_cache: dict = {}


def clear_cache():
    _cache.clear()


def _today(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.utcnow()).replace(hour=0, minute=0, second=0, microsecond=0)


async def record_play(db, title_id: ObjectId, viewer: str, *, logged_in: bool,
                      ip: Optional[str] = None, now: Optional[datetime] = None) -> bool:
    """True if this play was counted."""
    if not viewer:
        return False
    now = now or datetime.utcnow()
    day = _today(now)
    stamp = day.strftime("%Y%m%d")
    try:
        await db.tv_play_once.insert_one({"_id": f"{title_id}:{viewer}:{stamp}", "at": now})
    except DuplicateKeyError:
        return False
    if not logged_in:
        net = (ip or "unknown")[:64]
        row = await db.tv_play_ip.find_one_and_update(
            {"_id": f"{title_id}:{net}:{stamp}"},
            {"$inc": {"n": 1}, "$setOnInsert": {"at": now}},
            upsert=True, return_document=True)
        if (row or {}).get("n", 1) > ANON_PER_IP_PER_DAY:
            return False
    await db.tv_title_daily.update_one(
        {"_id": f"{title_id}:{stamp}"},
        {"$inc": {"plays": 1}, "$setOnInsert": {"title_id": title_id, "day": day}},
        upsert=True)
    return True


async def plays(db, title_id: ObjectId, days: int = 1) -> int:
    since = _today() - timedelta(days=days - 1)
    rows = await db.tv_title_daily.find({"title_id": title_id, "day": {"$gte": since}},
                                        {"plays": 1}).to_list(length=None)
    return sum(int(r.get("plays") or 0) for r in rows)


async def scores(db) -> Dict[ObjectId, float]:
    """Decayed recent plays per title (titles with no plays are absent)."""
    hit = _cache.get("scores")
    if hit and hit[0] > time.monotonic():
        return hit[1]
    today = _today()
    since = today - timedelta(days=WINDOW_DAYS - 1)
    out: Dict[ObjectId, float] = {}
    async for r in db.tv_title_daily.find({"day": {"$gte": since}}, {"title_id": 1, "day": 1, "plays": 1}):
        try:
            age = max(0, (today - r["day"]).days)
            out[r["title_id"]] = out.get(r["title_id"], 0.0) + float(r.get("plays") or 0) * 0.5 ** (age / HALF_LIFE_DAYS)
        except (KeyError, TypeError):
            continue
    _cache["scores"] = (time.monotonic() + CACHE_SECONDS, out)
    return out


def score(doc: dict, played: Dict[ObjectId, float]) -> float:
    boost = doc.get("popularity")
    boost = boost if isinstance(boost, (int, float)) and not isinstance(boost, bool) else 0
    return played.get(doc.get("_id"), 0.0) + boost
