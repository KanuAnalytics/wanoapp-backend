"""Filling a new Recombee database (in a region closer to Africa) from Wano's
own records, so recommendations don't start from zero after the move.

How a move goes:
  1. A new database is made in Recombee's admin in EU West, and its id and
     token go into RECOMBEE_NEW_DB_ID / RECOMBEE_NEW_PRIVATE_TOKEN (with
     RECOMBEE_NEW_REGION=eu-west). From then on every write the server sends
     to the current database is copied to the new one (recombee_service).
  2. POST /api/v1/recombee/move starts this job: properties, every video,
     users' interest tags and country, then everything people did that Wano
     keeps (watches, likes, bookmarks, comments, shares, "not interested",
     and logged-out devices' recent watches), each with its original time.
     GET /api/v1/recombee/move shows how far it has got. Safe to run again:
     Recombee refuses an interaction it already has (same user, post and
     time), which is counted as "already there", not as a failure.
  3. When it's done, the server is pointed at the new database
     (RECOMBEE_DB_ID / RECOMBEE_PRIVATE_TOKEN / RECOMBEE_REGION=eu-west) and the
     RECOMBEE_NEW_* settings are emptied. The old database is left alone.

What Recombee knew that Wano never stored (views by logged-out devices
older than the impressions kept, and partial watches under the "watched"
threshold of logged-in users) can't be copied.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

from bson import ObjectId
from recombee_api_client.api_requests import (
    AddBookmark, AddCartAddition, AddPurchase, AddRating, Batch, SetItemValues,
    SetUserValues, SetViewPortion,
)

from app.services import recombee_service
from app.services.recommendations import (
    ITEM_PROPERTIES, USER_PROPERTIES, ANON_PREFIX, item_values,
)

logger = logging.getLogger(__name__)

BATCH = 1000
STATE_ID = "recombee_move"
STEPS = ("properties", "videos", "users", "watches", "likes", "bookmarks", "comments", "shares", "devices")

_task: Optional[asyncio.Task] = None


def _ts(dt) -> Optional[str]:
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _id(x) -> Optional[str]:
    if isinstance(x, ObjectId):
        return str(x)
    if isinstance(x, str) and ObjectId.is_valid(x):
        return x
    return None


def _stable(item: str) -> str:
    """Wano doesn't keep when a like or bookmark happened: use the post's own
    time, the same on every run, so running the move again doesn't add it twice."""
    return ObjectId(item).generation_time.isoformat()


class _Step:
    def __init__(self, db, name: str):
        self.db, self.name = db, name
        self.batch: List = []
        self.sent = self.already = self.failed = 0

    async def add(self, req) -> None:
        self.batch.append(req)
        if len(self.batch) >= BATCH:
            await self.flush()

    async def flush(self) -> None:
        if not self.batch:
            return
        reqs, self.batch = self.batch, []
        try:
            req = Batch(reqs)
            req.timeout = 120000
            res = await recombee_service.send_to_new(req) or []
            for r in res:
                code = (r or {}).get("code") if isinstance(r, dict) else None
                if code in (200, 201):
                    self.sent += 1
                elif code == 409:          # already there (a run before this one)
                    self.already += 1
                else:
                    self.failed += 1
            missing = len(reqs) - len(res)
            self.failed += max(0, missing)
        except Exception as e:
            logger.warning("Recombee move: batch of %d failed in %s: %s", len(reqs), self.name, e)
            self.failed += len(reqs)
        await self.save()

    async def save(self, done: bool = False) -> None:
        upd = {f"steps.{self.name}": {"sent": self.sent, "already": self.already, "failed": self.failed,
                                       "done": done}, "at": datetime.utcnow()}
        await self.db.config.update_one({"_id": STATE_ID}, {"$set": upd}, upsert=True)

    async def finish(self) -> None:
        await self.flush()
        await self.save(done=True)


async def _properties(db) -> None:
    from recombee_api_client.api_requests import AddItemProperty, AddUserProperty
    made = failed = 0
    for name, kind in ITEM_PROPERTIES.items():
        try:
            await recombee_service.send_to_new(AddItemProperty(name, kind)); made += 1
        except Exception as e:
            if getattr(e, "status_code", None) != 409:
                failed += 1
    for name, kind in USER_PROPERTIES.items():
        try:
            await recombee_service.send_to_new(AddUserProperty(name, kind)); made += 1
        except Exception as e:
            if getattr(e, "status_code", None) != 409:
                failed += 1
    await db.config.update_one({"_id": STATE_ID}, {"$set": {
        "steps.properties": {"sent": made, "already": 0, "failed": failed, "done": True}}}, upsert=True)


async def _videos(db) -> None:
    s = _Step(db, "videos")
    async for v in db.videos.find({}):
        await s.add(SetItemValues(str(v["_id"]), item_values(v), cascade_create=True))
    await s.finish()


async def _users(db) -> None:
    s = _Step(db, "users")
    async for u in db.users.find({}, {"tags": 1, "localization": 1}):
        tags = [t for t in (u.get("tags") or []) if isinstance(t, str) and t][:50]
        country = str(((u.get("localization") or {}).get("country") or ""))[:8]
        await s.add(SetUserValues(str(u["_id"]), {"tags": tags, "country": country}, cascade_create=True))
    await s.finish()


async def _watches(db) -> None:
    """Watches, likes kept on the watch row, and "not interested"."""
    s = _Step(db, "watches")
    async for w in db.watch_history.find({}, {"user_id": 1, "video_id": 1, "portion": 1, "watched_at": 1,
                                             "first_watched_at": 1, "not_interested": 1}):
        user, item = _id(w.get("user_id")), _id(w.get("video_id"))
        if not user or not item:
            continue
        when = _ts(w.get("watched_at") or w.get("first_watched_at"))
        kw = {"cascade_create": True}
        if when:
            kw["timestamp"] = when
        if w.get("not_interested"):
            await s.add(AddRating(user, item, -1.0, **kw))
        portion = w.get("portion")
        if isinstance(portion, (int, float)) and not isinstance(portion, bool) and 0 <= portion <= 1:
            await s.add(SetViewPortion(user, item, float(portion), **kw))
    await s.finish()


async def _likes(db) -> None:
    s = _Step(db, "likes")
    async for u in db.users.find({"liked_videos.0": {"$exists": True}}, {"liked_videos": 1}):
        user = str(u["_id"])
        for v in (u.get("liked_videos") or [])[:5000]:
            item = _id(v)
            if item:
                await s.add(AddRating(user, item, 1.0, cascade_create=True, timestamp=_stable(item)))
    await s.finish()


async def _bookmarks(db) -> None:
    s = _Step(db, "bookmarks")
    async for u in db.users.find({"bookmarked_videos.0": {"$exists": True}}, {"bookmarked_videos": 1}):
        user = str(u["_id"])
        for v in (u.get("bookmarked_videos") or [])[:5000]:
            item = _id(v)
            if item:
                await s.add(AddBookmark(user, item, cascade_create=True, timestamp=_stable(item)))
    await s.finish()


async def _comments(db) -> None:
    s = _Step(db, "comments")
    async for c in db.comments.find({"is_active": {"$ne": False}}, {"user_id": 1, "video_id": 1, "created_at": 1}):
        user, item = _id(c.get("user_id")), _id(c.get("video_id"))
        if not user or not item:
            continue
        kw = {"cascade_create": True}
        when = _ts(c.get("created_at"))
        if when:
            kw["timestamp"] = when
        await s.add(AddCartAddition(user, item, **kw))
    await s.finish()


async def _shares(db) -> None:
    s = _Step(db, "shares")
    async for e in db.engagement_once.find({"_id": {"$regex": "^share:"}}):
        parts = str(e["_id"]).split(":")
        if len(parts) != 3:
            continue
        user, item = _id(parts[1]), _id(parts[2])
        if not user or not item:
            continue
        kw = {"cascade_create": True}
        when = _ts(e.get("at") or e.get("created_at"))
        if when:
            kw["timestamp"] = when
        await s.add(AddPurchase(user, item, **kw))
    await s.finish()


async def _devices(db) -> None:
    """Logged-out devices: the watches Wano still has on its For You impressions."""
    s = _Step(db, "devices")
    async for imp in db.feed_impressions.find({"viewer": {"$regex": f"^{ANON_PREFIX}"},
                                               "portion": {"$exists": True}},
                                              {"viewer": 1, "video_id": 1, "portion": 1, "watched_at": 1, "at": 1}):
        item, portion = _id(imp.get("video_id")), imp.get("portion")
        if not item or not isinstance(portion, (int, float)) or isinstance(portion, bool) or not 0 <= portion <= 1:
            continue
        kw = {"cascade_create": True}
        when = _ts(imp.get("watched_at") or imp.get("at"))
        if when:
            kw["timestamp"] = when
        await s.add(SetViewPortion(str(imp["viewer"]), item, float(portion), **kw))
    await s.finish()


RUNNERS = {"properties": _properties, "videos": _videos, "users": _users, "watches": _watches,
           "likes": _likes, "bookmarks": _bookmarks, "comments": _comments, "shares": _shares,
           "devices": _devices}


async def run(db) -> dict:
    await db.config.update_one({"_id": STATE_ID}, {"$set": {
        "state": "running", "started_at": datetime.utcnow(), "steps": {}, "error": None,
        "to_region": str(getattr(recombee_service.settings, "RECOMBEE_NEW_REGION", ""))}}, upsert=True)
    try:
        for name in STEPS:
            await db.config.update_one({"_id": STATE_ID}, {"$set": {"step": name}})
            await RUNNERS[name](db)
        await db.config.update_one({"_id": STATE_ID}, {"$set": {"state": "done", "step": None,
                                                               "finished_at": datetime.utcnow()}})
    except asyncio.CancelledError:
        await db.config.update_one({"_id": STATE_ID}, {"$set": {"state": "stopped"}})
        raise
    except Exception as e:
        logger.exception("Recombee move failed")
        await db.config.update_one({"_id": STATE_ID}, {"$set": {"state": "failed", "error": str(e)[:300]}})
    return await status(db)


def running() -> bool:
    return _task is not None and not _task.done()


async def start(db) -> dict:
    """Start the job in the background (one at a time per server process)."""
    global _task
    if recombee_service.new_client is None:
        return {"started": False, "reason": "No new database set (RECOMBEE_NEW_DB_ID / RECOMBEE_NEW_PRIVATE_TOKEN)"}
    if running():
        return {"started": False, "reason": "Already running", **(await status(db))}
    _task = asyncio.create_task(run(db))
    return {"started": True}


async def status(db) -> dict:
    doc = await db.config.find_one({"_id": STATE_ID}) or {}
    doc.pop("_id", None)
    return {"configured": recombee_service.new_client is not None, "running_here": running(), **doc}
