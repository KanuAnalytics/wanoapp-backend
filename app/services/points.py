"""
Wano points.

Earning. Each UTC day, once it is over (SETTLE_DELAY), a creator who posted at
least one qualifying post that day earns, from every qualifying post of theirs,
PER_VIEW per counted view and PER_FINISH per finished view that day (views are
once per viewer and never the creator's own; see creators.record_view), up to
DAILY_CAP a day. Days without a qualifying post earn nothing.

A qualifying post is public, live, ready, not held or removed, in one of
CATEGORIES (the creator picks it), not marked not-original by an admin, and
not reported as a copy (intellectual_property / spam) by COPY_REPORTS people.

The month. Points earned in a month stay pending. When the month ends, they lock
in (become spendable) if the creator had QUALIFY_DAYS or more posting days;
otherwise they expire.

Spending. Locked points buy extra For You viewers for the creator's own post
(VIEWERS_PER_POINT each, over BOOST_DAYS); what isn't used comes back.

Payouts. Off (POINTS_PAYOUT_ENABLED). When on, a creator with at least
POINTS_PAYOUT_MIN points can request a payout of their whole balance; an admin
approves (pays outside Wano) or rejects (points return). Points have no fixed
money value, can't be bought and can't be sent to other users.

Collections: points_wallet (spendable balance, the only thing spending
checks), points_ledger (history; earned rows carry their state), points_days
(posting days), points_runs (days/months already settled), points_payouts.
"""

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import BulkWriteError, DuplicateKeyError

from app.core.config import settings

logger = logging.getLogger(__name__)

CATEGORIES = {
    "comedy": "Comedy & skits",
    "music": "Music & dance",
    "food_culture": "Food, culture & travel",
    "learn_news": "Learn & local news",
    "film": "Film & documentary",
    "sports": "Sports",
}
PER_VIEW = 1
PER_FINISH = 1
DAILY_CAP = 2000
QUALIFY_DAYS = 20
COPY_REPORTS = 2
COPY_CATEGORIES = ("intellectual_property", "spam")
SETTLE_DELAY = timedelta(hours=1)
MONTH_DELAY = timedelta(hours=3)
CATCH_UP_DAYS = 40
MIN_BOOST = 100
MAX_BOOST = 100_000
VIEWERS_PER_POINT = 1
BOOST_DAYS = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _num(v) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return 0
    return max(0, int(v))


def payouts_enabled() -> bool:
    return bool(getattr(settings, "POINTS_PAYOUT_ENABLED", False))


def payout_minimum() -> int:
    return int(getattr(settings, "POINTS_PAYOUT_MIN", 50_000) or 50_000)


def month_of(day: datetime) -> str:
    return day.strftime("%Y-%m")


def qualifying() -> dict:
    return {"is_active": True, "privacy": "public", "isReadyToStream": {"$ne": False},
            "status": {"$nin": ["scheduled", "publishing", "cancelled"]},
            "moderation.state": {"$nin": ["held", "removed"]},
            "category": {"$in": list(CATEGORIES)},
            "originality.state": {"$ne": "rejected"}}


async def _copy_reported(db, ids: List[ObjectId]) -> set:
    if not ids:
        return set()
    out = set()
    async for r in db.reports.aggregate([
        {"$match": {"video_id": {"$in": ids}, "is_active": True, "category": {"$in": list(COPY_CATEGORIES)}}},
        {"$group": {"_id": "$video_id", "people": {"$addToSet": "$reporter_id"}}},
    ]):
        if len(r["people"]) >= COPY_REPORTS:
            out.add(r["_id"])
    return out


async def _ledger(db, _id: str, user: ObjectId, kind: str, amount: int, now: datetime, **extra) -> bool:
    try:
        await db.points_ledger.insert_one({"_id": _id, "user_id": user, "kind": kind, "amount": int(amount),
                                           "at": _naive(now), **extra})
        return True
    except DuplicateKeyError:
        return False


async def _wallet(db, user: ObjectId, delta: int) -> None:
    await db.points_wallet.update_one({"_id": user}, {"$inc": {"balance": int(delta)}}, upsert=True)


async def grant(db, user: ObjectId, amount: int, reason: str) -> None:
    """Admin / tests: add spendable points."""
    now = _utcnow()
    await _ledger(db, f"grant:{user}:{ObjectId()}", user, "grant", amount, now, note=reason)
    await _wallet(db, user, amount)


# ---------------------------------------------------------------- settling

async def settle_day(db, day: datetime) -> int:
    """Pending points for one UTC day. Returns rows written."""
    tag = "day:" + day.strftime("%Y-%m-%d")
    if await db.points_runs.find_one({"_id": tag}):
        return 0
    end = day + timedelta(days=1)
    posted = await db.videos.find({**qualifying(), "created_at": {"$gte": day, "$lt": end}},
                                  {"creator_id": 1}).to_list(None)
    bad = await _copy_reported(db, [p["_id"] for p in posted])
    creators_today = {p["creator_id"] for p in posted if p["_id"] not in bad and isinstance(p.get("creator_id"), ObjectId)}
    written = 0
    if creators_today:
        for c in creators_today:
            try:
                await db.points_days.insert_one({"_id": f"{c}:{day.date().isoformat()}", "user_id": c,
                                                 "month": month_of(day), "day": day})
            except DuplicateKeyError:
                pass
        rows = await db.post_daily.find({"day": day, "creator_id": {"$in": list(creators_today)}}).to_list(None)
        vids = list({r.get("video_id") for r in rows if isinstance(r.get("video_id"), ObjectId)})
        ok = {v["_id"]: v for v in await db.videos.find(
            {**qualifying(), "_id": {"$in": vids}, "created_at": {"$lt": end}}, {"creator_id": 1}).to_list(None)}
        bad = await _copy_reported(db, list(ok))
        per: Dict[ObjectId, list] = {}
        for r in rows:
            v = ok.get(r.get("video_id"))
            if not v or v["_id"] in bad or v.get("creator_id") != r.get("creator_id"):
                continue
            amount = _num(r.get("views")) * PER_VIEW + _num(r.get("finished")) * PER_FINISH
            if amount > 0:
                per.setdefault(v["creator_id"], []).append((amount, v["_id"]))
        now = _utcnow()
        docs = []
        for c, items in per.items():
            left = DAILY_CAP
            for amount, vid in sorted(items, key=lambda t: (-t[0], str(t[1]))):
                take = min(amount, left)
                if take <= 0:
                    break
                left -= take
                docs.append({"_id": f"earn:{vid}:{day.date().isoformat()}", "user_id": c, "kind": "earned",
                             "state": "pending", "amount": take, "video_id": vid, "day": day,
                             "month": month_of(day), "at": _naive(now)})
        if docs:
            try:
                res = await db.points_ledger.insert_many(docs, ordered=False)
                written = len(res.inserted_ids)
            except BulkWriteError as e:
                written = e.details.get("nInserted", 0)
    try:
        await db.points_runs.insert_one({"_id": tag, "at": _naive(_utcnow())})
    except DuplicateKeyError:
        pass
    return written


async def close_month(db, month: str) -> int:
    """Lock in or expire a finished month's pending points. Returns users done."""
    tag = "month:" + month
    if await db.points_runs.find_one({"_id": tag}):
        return 0
    users = await db.points_ledger.distinct("user_id", {"kind": "earned", "state": "pending", "month": month})
    now = _utcnow()
    done = 0
    for u in users:
        days = await db.points_days.count_documents({"user_id": u, "month": month})
        rows = await db.points_ledger.find({"user_id": u, "kind": "earned", "state": "pending", "month": month},
                                           {"amount": 1}).to_list(None)
        total = sum(_num(r.get("amount")) for r in rows)
        keep = days >= QUALIFY_DAYS
        # The summary row is the once-only guard for this user and month.
        if not await _ledger(db, f"month:{u}:{month}", u, "locked_month" if keep else "expired",
                             total if keep else 0, now, month=month, posting_days=days, points=total):
            continue
        await db.points_ledger.update_many({"user_id": u, "kind": "earned", "state": "pending", "month": month},
                                           {"$set": {"state": "locked" if keep else "expired"}})
        if keep and total:
            await _wallet(db, u, total)
        done += 1
    try:
        await db.points_runs.insert_one({"_id": tag, "at": _naive(now)})
    except DuplicateKeyError:
        pass
    return done


async def end_boosts(db) -> int:
    """Refund the unused part of boosts that have ended."""
    now = _naive(_utcnow())
    n = 0
    async for v in db.videos.find({"points_boost.until": {"$lte": now}, "points_boost.refunded": {"$ne": True}},
                                  {"points_boost": 1, "creator_id": 1}):
        claimed = await db.videos.find_one_and_update(
            {"_id": v["_id"], "points_boost.refunded": {"$ne": True}, "points_boost.until": {"$lte": now}},
            {"$set": {"points_boost.refunded": True}})
        if not claimed:
            continue
        b = claimed["points_boost"]
        reach = await db.video_reach.find_one({"_id": v["_id"]}, {"impressions": 1}) or {}
        used = max(0, _num(reach.get("impressions")) - _num(b.get("start")))
        unused = max(0, _num(b.get("target")) - used)
        refund = min(_num(b.get("points")), unused // VIEWERS_PER_POINT)
        if refund:
            await _ledger(db, f"refund:{v['_id']}:{b.get('started_at')}", claimed["creator_id"], "refund", refund,
                          _utcnow(), video_id=v["_id"])
            await _wallet(db, claimed["creator_id"], refund)
        n += 1
    return n


async def settle(db) -> dict:
    now = _naive(_utcnow())
    today = datetime(now.year, now.month, now.day)
    out = {"days": 0, "months": 0, "boosts": 0}
    for back in range(CATCH_UP_DAYS, 0, -1):
        day = today - timedelta(days=back)
        if now >= day + timedelta(days=1) + SETTLE_DELAY:
            out["days"] += await settle_day(db, day)
    first = datetime(now.year, now.month, 1)
    prev = (first - timedelta(days=1)).replace(day=1)
    prev2 = (prev - timedelta(days=1)).replace(day=1)
    out["months"] += await close_month(db, month_of(prev2))          # in case a run was missed
    if now >= first + MONTH_DELAY:
        out["months"] += await close_month(db, month_of(prev))
    out["boosts"] = await end_boosts(db)
    return out


# ---------------------------------------------------------------- spending

class PointsError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


async def boost(db, user: ObjectId, video_id: ObjectId, amount: int) -> dict:
    v = await db.videos.find_one({"_id": video_id})
    if not v:
        raise PointsError(404, "Post not found")
    if v.get("creator_id") != user:
        raise PointsError(403, "You can only boost your own posts")
    from app.services.creators import _showable
    if not await db.videos.find_one({"_id": video_id, **_showable(), "moderation.state": {"$nin": ["held", "removed"]}},
                                    {"_id": 1}):
        raise PointsError(400, "Only public, live posts can be boosted")
    paid = await db.points_wallet.find_one_and_update({"_id": user, "balance": {"$gte": amount}},
                                                      {"$inc": {"balance": -amount}})
    if not paid:
        raise PointsError(400, "Not enough points")
    now = _naive(_utcnow())
    reach = await db.video_reach.find_one({"_id": video_id}, {"impressions": 1}) or {}
    b = v.get("points_boost") or {}
    active = isinstance(b.get("until"), datetime) and b["until"] > now and not b.get("refunded")
    viewers = amount * VIEWERS_PER_POINT
    if active:
        await db.videos.update_one({"_id": video_id}, {"$inc": {"points_boost.target": viewers,
                                                                 "points_boost.points": amount}})
        started = b.get("started_at")
    else:
        started = now.isoformat()
        await db.videos.update_one({"_id": video_id}, {"$set": {"points_boost": {
            "start": _num(reach.get("impressions")), "target": viewers, "points": amount,
            "until": now + timedelta(days=BOOST_DAYS), "started_at": started, "refunded": False}}})
    await _ledger(db, f"spend:{video_id}:{ObjectId()}", user, "spent", -amount, _utcnow(), video_id=video_id)
    return {"video_id": str(video_id), "points": amount, "extra_viewers": viewers}


async def request_payout(db, user: ObjectId) -> dict:
    if not payouts_enabled():
        raise PointsError(403, "Payouts aren't available yet")
    w = await db.points_wallet.find_one({"_id": user}) or {}
    bal = _num(w.get("balance"))
    if bal < payout_minimum():
        raise PointsError(400, f"You need {payout_minimum()} points to request a payout")
    if not await db.points_wallet.find_one_and_update({"_id": user, "balance": bal}, {"$inc": {"balance": -bal}}):
        raise PointsError(409, "Your balance changed, try again")
    pid = ObjectId()
    now = _utcnow()
    await db.points_payouts.insert_one({"_id": pid, "user_id": user, "points": bal, "state": "requested",
                                        "at": _naive(now)})
    await _ledger(db, f"payout:{pid}", user, "payout", -bal, now, payout_id=pid)
    return {"id": str(pid), "points": bal, "state": "requested"}


async def decide_payout(db, pid: ObjectId, approve: bool, admin: str) -> dict:
    p = await db.points_payouts.find_one_and_update(
        {"_id": pid, "state": "requested"},
        {"$set": {"state": "approved" if approve else "rejected", "decided_by": admin,
                  "decided_at": _naive(_utcnow())}}, return_document=ReturnDocument.AFTER)
    if not p:
        raise PointsError(409, "This payout was already decided")
    if not approve:
        await _ledger(db, f"payout-returned:{pid}", p["user_id"], "payout_returned", p["points"], _utcnow(),
                      payout_id=pid)
        await _wallet(db, p["user_id"], p["points"])
    return {"id": str(pid), "state": p["state"], "points": p["points"]}


async def not_original(db, video_id: ObjectId, admin: str) -> dict:
    """An admin decides a post isn't original: it stops earning and its points go."""
    v = await db.videos.find_one_and_update({"_id": video_id}, {"$set": {"originality": {
        "state": "rejected", "by": admin, "at": _naive(_utcnow())}}})
    if not v:
        raise PointsError(404, "Post not found")
    removed = 0
    async for row in db.points_ledger.find({"video_id": video_id, "kind": "earned",
                                            "state": {"$in": ["pending", "locked"]}}, {"_id": 1}):
        before = await db.points_ledger.find_one_and_update(
            {"_id": row["_id"], "state": {"$in": ["pending", "locked"]}}, {"$set": {"state": "revoked"}})
        if before and before["state"] == "locked":
            await _wallet(db, before["user_id"], -_num(before.get("amount")))
        if before:
            removed += _num(before.get("amount"))
    return {"video_id": str(video_id), "points_removed": removed}


# ---------------------------------------------------------------- reading

async def summary(db, user: ObjectId) -> dict:
    now = _naive(_utcnow())
    month = month_of(now)
    w = await db.points_wallet.find_one({"_id": user}) or {}
    balance = w.get("balance") if isinstance(w.get("balance"), int) else 0
    pending = 0
    async for r in db.points_ledger.find({"user_id": user, "kind": "earned", "state": "pending"}, {"amount": 1}):
        pending += _num(r.get("amount"))
    days = await db.points_days.count_documents({"user_id": user, "month": month})
    hist = await db.points_ledger.find({"user_id": user}).sort([("at", -1), ("_id", -1)]).limit(30).to_list(30)
    return {
        "balance": balance, "pending": pending, "month": month, "posting_days": days,
        "days_needed": QUALIFY_DAYS,
        "boost": {"min_points": MIN_BOOST, "viewers_per_point": VIEWERS_PER_POINT, "days": BOOST_DAYS},
        "payout": {"enabled": payouts_enabled(), "minimum": payout_minimum(),
                   "can_request": payouts_enabled() and balance >= payout_minimum()},
        "history": [{"kind": h.get("kind"), "state": h.get("state"), "amount": h.get("amount"),
                     "month": h.get("month"), "video_id": str(h["video_id"]) if h.get("video_id") else None,
                     "at": h.get("at")} for h in hist],
    }


async def boosted_candidates(db, excluded: List[ObjectId], own: Optional[ObjectId]) -> List[dict]:
    """Posts with points boost viewers left, for For You's fresh slots."""
    from app.services.creators import _showable
    from app.services.moderation import not_flagged
    now = _naive(_utcnow())
    q = {**_showable(), **not_flagged(), "moderation.state": {"$nin": ["held", "removed"]},
         "points_boost.until": {"$gt": now}, "points_boost.refunded": {"$ne": True}}
    if excluded:
        q["_id"] = {"$nin": excluded}
    if own is not None:
        q["creator_id"] = {"$ne": own}
    docs = await db.videos.find(q, {"creator_id": 1, "created_at": 1, "points_boost": 1}).limit(100).to_list(100)
    if not docs:
        return []
    reach = {r["_id"]: r async for r in db.video_reach.find({"_id": {"$in": [d["_id"] for d in docs]}},
                                                            {"impressions": 1})}
    out = []
    for d in docs:
        b = d.get("points_boost") or {}
        left = _num(b.get("start")) + _num(b.get("target")) - _num((reach.get(d["_id"]) or {}).get("impressions"))
        if left > 0:
            out.append((left, {"_id": d["_id"], "creator_id": d.get("creator_id"), "created_at": d.get("created_at"),
                               "stage": "paid"}))
    out.sort(key=lambda t: -t[0])
    return [t[1] for t in out]
