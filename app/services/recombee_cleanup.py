"""One-time clean-up: take staff accounts' past watching out of Recombee.

staff_quiet stops new staff signals, but everything staff watched, liked and
saved before that (often reported posts, while moderating) is still teaching
For You. This deletes it from both Recombee databases (US and EU), keeping the
accounts themselves. Run from Admin > Recombee; safe to run again.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from recombee_api_client.api_requests import (
    Batch, DeleteBookmark, DeleteCartAddition, DeleteDetailView, DeletePurchase, DeleteRating, DeleteViewPortion,
    ListUserBookmarks, ListUserCartAdditions, ListUserDetailViews, ListUserPurchases, ListUserRatings,
    ListUserViewPortions,
)

logger = logging.getLogger(__name__)

STATE_ID = "staff_cleanup"
BATCH = 500
# (list request, delete request, needs the session id)
KINDS = (
    ("detail_views", ListUserDetailViews, DeleteDetailView, False),
    ("view_portions", ListUserViewPortions, DeleteViewPortion, True),
    ("ratings", ListUserRatings, DeleteRating, False),
    ("bookmarks", ListUserBookmarks, DeleteBookmark, False),
    ("purchases", ListUserPurchases, DeletePurchase, False),
    ("cart_additions", ListUserCartAdditions, DeleteCartAddition, False),
)
_task: Optional[asyncio.Task] = None


def staff_filter() -> dict:
    from app.services import staff
    return {"$or": [{"user_type": "admin"}, {"staff_role": {"$in": list(staff.ROLES)}}]}


def _databases():
    from app.services import recombee_service as rs
    out = [("us", rs.recombee_client)]
    if rs.new_client is not None:
        out.append(("eu", rs.new_client))
    return out


def _deletes(uid: str, rows, delete, with_session: bool) -> list:
    reqs, seen = [], set()
    for r in rows or []:
        item = r.get("itemId") if isinstance(r, dict) else None
        if not item:
            continue
        if with_session:
            key = (item, r.get("sessionId"))
            if key in seen:
                continue
            seen.add(key)
            reqs.append(delete(uid, item, session_id=r["sessionId"]) if r.get("sessionId") else delete(uid, item))
        else:
            if item in seen:
                continue
            seen.add(item)
            reqs.append(delete(uid, item))          # no timestamp: every one for this post
    return reqs


async def _send(client, request):
    return await asyncio.to_thread(client.send, request)


async def clear_user(client, uid: str) -> dict:
    """Delete one account's interactions from one database. {kind: deleted}."""
    done = {}
    for name, lister, delete, with_session in KINDS:
        try:
            rows = await _send(client, lister(uid))
        except Exception as e:          # not in this database (404) or can't list: nothing to do
            if "404" not in str(e):
                logger.warning("staff cleanup: listing %s for %s failed: %s", name, uid, e)
            done[name] = 0
            continue
        reqs = _deletes(uid, rows, delete, with_session)
        for i in range(0, len(reqs), BATCH):
            await _send(client, Batch(reqs[i:i + BATCH]))
        done[name] = len(reqs)
    return done


async def run(db) -> dict:
    ids = [str(u["_id"]) async for u in db.users.find(staff_filter(), {"_id": 1})]
    await db.recombee_cleanup.update_one(
        {"_id": STATE_ID}, {"$set": {"started_at": datetime.utcnow(), "accounts": len(ids), "done_accounts": 0,
                                     "deleted": {}, "errors": 0, "finished_at": None}}, upsert=True)
    total: dict = {}
    errors = 0
    for n, uid in enumerate(ids, 1):
        for region, client in _databases():
            try:
                for k, v in (await clear_user(client, uid)).items():
                    total[f"{region}:{k}"] = total.get(f"{region}:{k}", 0) + v
            except Exception:
                errors += 1
                logger.warning("staff cleanup failed for %s in %s", uid, region, exc_info=True)
        await db.recombee_cleanup.update_one({"_id": STATE_ID},
                                             {"$set": {"done_accounts": n, "deleted": total, "errors": errors}})
    await db.recombee_cleanup.update_one({"_id": STATE_ID}, {"$set": {"finished_at": datetime.utcnow()}})
    return await status(db)


def running() -> bool:
    return _task is not None and not _task.done()


async def start(db) -> dict:
    global _task
    if running():
        return {"started": False, "reason": "Already running", **(await status(db))}
    _task = asyncio.create_task(run(db))
    return {"started": True}


async def status(db) -> dict:
    d = await db.recombee_cleanup.find_one({"_id": STATE_ID}) or {}
    d.pop("_id", None)
    return {"running": running(), **d}
