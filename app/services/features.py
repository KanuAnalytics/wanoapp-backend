"""Turning app features on and off (the switches GET /config/features reads).

The app reads the newest config document that has a `features` object, and the
version check reads the newest config document of all, so a switch is written
into an existing document, never a new one, unless there is no config at all."""

import asyncio
from typing import Dict

from pymongo import ReturnDocument

from app.api.v1.config import APP_FEATURES

# A feature that only works when another one is on.
NEEDS = {"boost": "points", "earnings": "points", "tv_play": "tv_series"}

_lock = asyncio.Lock()


class FeatureError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail


def _clean(raw) -> Dict[str, bool]:
    on = raw if isinstance(raw, dict) else {}
    return {k: on.get(k) is True for k in APP_FEATURES}


async def current(db) -> Dict[str, bool]:
    doc = await db.config.find_one({"features": {"$type": "object"}}, projection={"features": 1},
                                   sort=[("_id", -1)]) or {}
    return _clean(doc.get("features"))


async def _write(db, changes: Dict[str, bool]) -> None:
    sets = {f"features.{k}": v for k, v in changes.items()}
    for _ in range(5):
        doc = await db.config.find_one_and_update({"features": {"$type": "object"}}, {"$set": sets},
                                                  sort=[("_id", -1)], return_document=ReturnDocument.AFTER)
        if doc:
            return
        latest = await db.config.find_one({}, projection={"_id": 1}, sort=[("_id", -1)])
        if latest:
            # No document has a features object (missing or garbage): give the newest one a fresh one.
            r = await db.config.update_one({"_id": latest["_id"], "features": {"$not": {"$type": "object"}}},
                                           {"$set": {"features": dict(changes)}})
            if r.modified_count:
                return
            continue
        await db.config.insert_one({"features": dict(changes)})
        return
    raise FeatureError(503, "Couldn't save the switch, try again")


async def set_feature(db, key: str, on: bool) -> Dict[str, bool]:
    async with _lock:
        now = await current(db)
        changes = {key: on}
        if on and key in NEEDS and not now[NEEDS[key]]:
            raise FeatureError(409, f"Turn on {NEEDS[key]} first")
        if not on:
            for k, needed in NEEDS.items():
                if needed == key and now[k]:
                    changes[k] = False
        await _write(db, changes)
        _forget_cached()
        return await current(db)


def _forget_cached():
    try:
        from app.core import gzip_switch
        gzip_switch.forget()
    except Exception:
        pass


async def all_off(db) -> Dict[str, bool]:
    async with _lock:
        await db.config.update_many({"features": {"$type": "object"}},
                                    {"$set": {"features": {k: False for k in APP_FEATURES}}})
        _forget_cached()
        return await current(db)
