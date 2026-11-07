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

Cash. Each month has a creator pool: max(POINTS_POOL_FLOOR_USD, POINTS_POOL_PERCENT
% of the revenue an admin enters). When the month has closed, the pool is split
between creators in proportion to the points they locked in that month (spending
points on boosts doesn't reduce the share). Automatic on DISTRIBUTE_DAY of the
next month, or earlier by an admin. Shares add to a cash balance (cents) that
keeps building; at POINTS_CASH_MIN_USD a creator can request a payout of all of
it, which an admin approves (after paying outside Wano) or rejects (cash returns).
Wano never pays more than the pool, however many creators there are. Points
themselves have no money value, can't be bought and can't be sent to others.

Collections: points_wallet (spendable balance, the only thing spending
checks), points_ledger (history; earned rows carry their state), points_days
(posting days), points_runs (days/months already settled), points_payouts.
"""

import logging
import math
import re
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

# Who can earn a creator points (anti-farming). A view earns only when the
# viewer is signed in to an account at least VIEWER_MIN_AGE_DAYS old, didn't
# reach the post through a paid boost, and has given this creator fewer than
# VIEWER_DAILY_CAP earning views today.
VIEWER_MIN_AGE_DAYS = 7
VIEWER_DAILY_CAP = 3

# The cash pool: no creator gets more than MAX_SHARE of a month's pool (the
# rest stays with Wano); new cash waits HOLD_DAYS before it can be paid out,
# so a copied post found in that time loses its cash.
MAX_SHARE = 0.25
HOLD_DAYS = 14
PAYOUT_METHODS = ("mobile_money", "pix", "bank")

# Wano Creators: who can share the cash pool (each a setting, see _cfg).
CREATOR_DEFAULTS = {
    "CREATOR_MIN_AGE": 18,
    "CREATOR_ACCOUNT_DAYS": 30,
    "CREATOR_MIN_FOLLOWERS": 100,
    "CREATOR_MIN_VIEWS_28D": 1000,
    "CREATOR_MIN_VIEWERS_28D": 50,
    "CREATOR_STRIKE_DAYS": 90,
    "POINTS_USD_PER_CREATOR": 10,       # top N creators share the pool, N = pool / this
    "POINTS_DEVICE_ACCOUNTS": 3,        # earning accounts per phone per day
    "POINTS_IP_ACCOUNTS": 50,           # per connection (mobile networks share IPs, so looser)
}


def _cfg(name: str):
    v = getattr(settings, name, None)
    return CREATOR_DEFAULTS[name] if v is None else v


def viewer_needs_phone() -> bool:
    v = getattr(settings, "POINTS_VIEWER_NEEDS_PHONE", None)
    return True if v is None else bool(v)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _num(v) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return 0
    return max(0, int(v))


def pool_defaults() -> dict:
    return {"floor_usd": float(getattr(settings, "POINTS_POOL_FLOOR_USD", 200)),
            "percent": float(getattr(settings, "POINTS_POOL_PERCENT", 20)),
            "revenue_usd": 0.0}


def pool_max_usd() -> float:
    return float(getattr(settings, "POINTS_POOL_MAX_USD", 5000) or 5000)


def max_share() -> float:
    return float(getattr(settings, "POINTS_MAX_SHARE", MAX_SHARE) or MAX_SHARE)


def cash_minimum_cents() -> int:
    return int(round(float(getattr(settings, "POINTS_CASH_MIN_USD", 5)) * 100))


DISTRIBUTE_DAY = 5


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


# ---------------------------------------------------------------- who can earn

async def _slot(db, kind: str, key: str, viewer: str, day: str, limit: int, now: datetime) -> bool:
    """One of the first `limit` accounts seen on this device/connection today."""
    row = await db.points_device_daily.find_one_and_update(
        {"_id": f"{kind}:{key}:{day}"},
        {"$addToSet": {"accounts": viewer}, "$setOnInsert": {"at": _naive(now)}},
        upsert=True, return_document=ReturnDocument.AFTER)
    accounts = (row or {}).get("accounts") or []
    return viewer in accounts[:limit]


async def view_earns(db, video: dict, viewer, now: datetime, device: Optional[str] = None,
                     ip: Optional[str] = None) -> bool:
    """Whether a counted view (already once per viewer per video, never the
    creator's own) also earns the creator points. Never raises."""
    try:
        if not viewer or not ObjectId.is_valid(str(viewer)):
            return False
        creator = video.get("creator_id")
        u = await db.users.find_one({"_id": ObjectId(str(viewer)), "is_active": True},
                                    {"created_at": 1, "phone_number": 1})
        created = (u or {}).get("created_at")
        if not isinstance(created, datetime) or _naive(created) > _naive(now) - timedelta(days=VIEWER_MIN_AGE_DAYS):
            return False
        # A real phone number per account makes fake viewers expensive.
        if viewer_needs_phone() and not (isinstance(u.get("phone_number"), str) and u["phone_number"].strip()):
            return False
        if await db.feed_impressions.find_one({"viewer": str(viewer), "video_id": video.get("_id"), "source": "boost"},
                                              {"_id": 1}):
            return False
        day = _naive(now).strftime("%Y%m%d")
        dev = re.sub(r"[^A-Za-z0-9_\-]", "", device or "")[:64]
        if dev and not await _slot(db, "d", dev, str(viewer), day, int(_cfg("POINTS_DEVICE_ACCOUNTS")), now):
            return False
        if ip and not await _slot(db, "ip", str(ip)[:64], str(viewer), day, int(_cfg("POINTS_IP_ACCOUNTS")), now):
            return False
        row = await db.points_viewer_daily.find_one_and_update(
            {"_id": f"{viewer}:{creator}:{day}"},
            {"$inc": {"n": 1}, "$setOnInsert": {"at": _naive(now), "viewer": ObjectId(str(viewer)), "creator": creator}},
            upsert=True, return_document=ReturnDocument.AFTER)
        return (row or {}).get("n", 1) <= VIEWER_DAILY_CAP
    except Exception:
        logger.exception("Could not check whether a view earns points")
        return False


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
            # Earning views only (see view_earns); plain views stay for stats.
            amount = _num(r.get("pviews")) * PER_VIEW + _num(r.get("pfinished")) * PER_FINISH
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
                                           {"amount": 1, "video_id": 1}).to_list(None)
        # Posts deleted, hidden, held or reported as copies since don't lock in.
        vids = list({r.get("video_id") for r in rows if isinstance(r.get("video_id"), ObjectId)})
        still = {v["_id"] for v in await db.videos.find({**qualifying(), "_id": {"$in": vids}}, {"_id": 1}).to_list(None)}
        still -= await _copy_reported(db, list(still))
        dropped = [r["_id"] for r in rows if r.get("video_id") not in still]
        if dropped:
            await db.points_ledger.update_many({"_id": {"$in": dropped}, "state": "pending"},
                                               {"$set": {"state": "expired", "reason": "post no longer qualifies"}})
        rows = [r for r in rows if r.get("video_id") in still]
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
    out["pools"] = await auto_distribute(db)
    out["released"] = await release_held(db)
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


# ---------------------------------------------------------------- the cash pool

def _pool_cents(cfg: dict) -> int:
    return int(round(max(cfg["floor_usd"], cfg["revenue_usd"] * cfg["percent"] / 100.0) * 100))


async def _pool_cfg(db, month: str) -> dict:
    doc = await db.points_pool.find_one({"_id": month}) or {}
    cfg = pool_defaults()
    for k in cfg:
        if isinstance(doc.get(k), (int, float)) and not isinstance(doc.get(k), bool):
            cfg[k] = float(doc[k])
    return {**cfg, "state": doc.get("state", "open")}


async def set_pool(db, month: str, confirm: bool = False, **changes) -> dict:
    cur = await db.points_pool.find_one({"_id": month})
    if cur and cur.get("state") != "open":
        raise PointsError(409, "This month's pool was already shared out")
    sets = {k: float(v) for k, v in changes.items() if v is not None}
    after = {**(await _pool_cfg(db, month)), **sets}
    if _pool_cents(after) > pool_max_usd() * 100 and not confirm:
        raise PointsError(400, f"That makes a pool of ${_pool_cents(after) / 100:,.2f}, over ${pool_max_usd():,.0f}. "
                               "Check the numbers and send confirm: true if it's right.")
    try:
        await db.points_pool.update_one({"_id": month, "state": {"$in": ["open", None]}},
                                        {"$set": sets, "$setOnInsert": {"state": "open"}}, upsert=True)
    except DuplicateKeyError:
        raise PointsError(409, "This month's pool was already shared out")
    return await pool_preview(db, month)


async def _month_shares(db, month: str) -> Dict[ObjectId, int]:
    """Points each active creator locked in that month, minus any since marked
    not original (revoked)."""
    out = {}
    async for r in db.points_ledger.aggregate([
        {"$match": {"kind": "earned", "state": "locked", "month": month}},
        {"$group": {"_id": "$user_id", "points": {"$sum": "$amount"}}},
    ]):
        if _num(r.get("points")) > 0:
            out[r["_id"]] = _num(r["points"])
    if out:
        active = {u["_id"] async for u in db.users.find({"_id": {"$in": list(out)}, "is_active": True}, {"_id": 1})}
        out = {u: p for u, p in out.items() if u in active}
    return out


async def creator_status(db, user: ObjectId, end: datetime) -> dict:
    """Whether a creator qualifies as a Wano Creator (can share the cash pool),
    judged over the 28 days before `end`. Returns {eligible, checks}."""
    end = _naive(end)
    start = end - timedelta(days=28)
    u = await db.users.find_one({"_id": user}, {"date_of_birth": 1, "phone_number": 1, "created_at": 1,
                                                "followers_count": 1, "is_active": 1}) or {}
    dob = u.get("date_of_birth")
    age = None
    if isinstance(dob, datetime):
        dob = _naive(dob)
        age = end.year - dob.year - ((end.month, end.day) < (dob.month, dob.day))
    created = u.get("created_at")
    account_days = (end - _naive(created)).days if isinstance(created, datetime) else 0
    followers = _num(u.get("followers_count"))
    views = 0
    async for r in db.post_daily.aggregate([
        {"$match": {"creator_id": user, "day": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": None, "v": {"$sum": {"$ifNull": ["$pviews", 0]}}}},
    ]):
        views = _num(r.get("v"))
    viewers = len(await db.points_viewer_daily.distinct(
        "viewer", {"creator": user, "at": {"$gte": start, "$lt": end}}))
    strikes = await db.videos.count_documents({
        "creator_id": user, "originality.state": "rejected",
        "originality.at": {"$gte": end - timedelta(days=int(_cfg("CREATOR_STRIKE_DAYS")))}})
    checks = [
        {"key": "age", "ok": age is not None and age >= int(_cfg("CREATOR_MIN_AGE")),
         "have": None, "need": int(_cfg("CREATOR_MIN_AGE"))},
        {"key": "phone", "ok": isinstance(u.get("phone_number"), str) and bool(u["phone_number"].strip()),
         "have": None, "need": None},
        {"key": "account_days", "ok": account_days >= int(_cfg("CREATOR_ACCOUNT_DAYS")),
         "have": max(0, account_days), "need": int(_cfg("CREATOR_ACCOUNT_DAYS"))},
        {"key": "followers", "ok": followers >= int(_cfg("CREATOR_MIN_FOLLOWERS")),
         "have": followers, "need": int(_cfg("CREATOR_MIN_FOLLOWERS"))},
        {"key": "views_28d", "ok": views >= int(_cfg("CREATOR_MIN_VIEWS_28D")),
         "have": views, "need": int(_cfg("CREATOR_MIN_VIEWS_28D"))},
        {"key": "viewers_28d", "ok": viewers >= int(_cfg("CREATOR_MIN_VIEWERS_28D")),
         "have": viewers, "need": int(_cfg("CREATOR_MIN_VIEWERS_28D"))},
        {"key": "no_copied_posts", "ok": strikes == 0, "have": strikes, "need": 0},
    ]
    return {"eligible": u.get("is_active") is True and all(c["ok"] for c in checks), "checks": checks}


async def _eligible_top(db, shares: Dict[ObjectId, int], month: str, n: int) -> Dict[ObjectId, int]:
    """The top `n` creators by points who qualify as Wano Creators for `month`."""
    y, m = (int(x) for x in month.split("-"))
    end = datetime(y + (m == 12), m % 12 + 1, 1)
    ranked = sorted(shares.items(), key=lambda t: (-t[1], str(t[0])))
    out: Dict[ObjectId, int] = {}
    # Cheap checks in bulk first (account fields), the full status only for those left.
    cutoff_created = end - timedelta(days=int(_cfg("CREATOR_ACCOUNT_DAYS")))
    for i in range(0, len(ranked), 500):
        chunk = ranked[i:i + 500]
        maybe = {u["_id"] async for u in db.users.find({
            "_id": {"$in": [u for u, _ in chunk]}, "is_active": True,
            "followers_count": {"$gte": int(_cfg("CREATOR_MIN_FOLLOWERS"))},
            "phone_number": {"$type": "string", "$ne": ""},
            "created_at": {"$lte": cutoff_created}}, {"_id": 1})}
        for u, p in chunk:
            if len(out) >= n:
                return out
            if u in maybe and (await creator_status(db, u, end))["eligible"]:
                out[u] = p
    return out


def top_n(pool_cents: int) -> int:
    return max(1, int(pool_cents // (float(_cfg("POINTS_USD_PER_CREATOR")) * 100)))


def _split(cents: int, shares: Dict[ObjectId, int], share_cap: Optional[float] = None) -> Dict[ObjectId, int]:
    """Cents per creator, proportional to points, nobody above share_cap of the
    pool (what the cap frees goes to the others, up to their own cap; anything
    left stays unpaid). Always whole cents, never more than the pool."""
    shares = {u: p for u, p in shares.items() if p > 0}
    if not shares or cents <= 0:
        return {}
    cap = int(cents * share_cap) if share_cap and share_cap < 1 else cents
    out: Dict[ObjectId, int] = {}
    left, active = cents, dict(shares)
    while active:
        total = sum(active.values())
        over = [u for u, p in active.items() if left * p / total > cap]
        if not over:
            break
        for u in over:
            out[u] = cap
            left -= cap
            del active[u]
        if left <= 0:
            return out
    if active:
        total = sum(active.values())
        base = {u: left * p // total for u, p in active.items()}
        rem = left - sum(base.values())
        for u in sorted(active, key=lambda u: (-((left * active[u]) % total), str(u)))[:rem]:
            base[u] += 1
        out.update(base)
    return out


async def pool_preview(db, month: str) -> dict:
    cfg = await _pool_cfg(db, month)
    shares = await _month_shares(db, month)
    total = sum(shares.values())
    cents = _pool_cents(cfg)
    split = _split(cents, shares, max_share())
    top = sorted(split.items(), key=lambda t: -t[1])[:20]
    return {"month": month, "state": cfg["state"], "floor_usd": cfg["floor_usd"], "percent": cfg["percent"],
            "revenue_usd": cfg["revenue_usd"], "pool_usd": cents / 100, "total_points": total,
            "usd_per_1000_points": round(cents / total * 10, 4) if total else None, "creators": len(shares),
            "top": [{"user_id": str(u), "points": shares[u], "usd": c / 100} for u, c in top]}


def _month_closed_tag(month: str) -> str:
    return "month:" + month


async def distribute(db, month: str) -> dict:
    if not await db.points_runs.find_one({"_id": _month_closed_tag(month)}):
        raise PointsError(400, "That month isn't over yet")
    cfg = await _pool_cfg(db, month)
    cents = _pool_cents(cfg)
    try:
        claimed = await db.points_pool.find_one_and_update(
            {"_id": month, "state": {"$ne": "distributed"}},
            {"$set": {"state": "distributed", "pool_cents": cents, **{k: cfg[k] for k in pool_defaults()},
                      "distributed_at": _naive(_utcnow())}}, upsert=True)
    except DuplicateKeyError:          # already distributed (the upsert found no open doc)
        raise PointsError(409, "Already shared out")
    if claimed is not None and claimed.get("state") == "distributed":
        raise PointsError(409, "Already shared out")
    shares = await _eligible_top(db, await _month_shares(db, month), month, top_n(cents))
    split = _split(cents, shares, max_share())
    now = _utcnow()
    held = HOLD_DAYS > 0
    paid = 0
    for u, c in split.items():
        if c <= 0:
            continue
        try:
            await db.creator_earnings.insert_one({
                "_id": f"share:{u}:{month}", "user_id": u, "month": month, "points": shares[u], "cents": c,
                "orig_points": shares[u], "orig_cents": c,
                "state": "held" if held else "available", "at": _naive(now),
                "available_at": _naive(now) + timedelta(days=HOLD_DAYS)})
        except DuplicateKeyError:
            continue
        await db.earnings_wallet.update_one({"_id": u}, {"$inc": {"held_cents" if held else "cents": c}}, upsert=True)
        paid += c
    total = sum(shares.values())
    await db.points_pool.update_one({"_id": month}, {"$set": {"total_points": total, "paid_cents": paid,
                                                              "kept_cents": cents - paid, "creators": len(split)}})
    return {"month": month, "pool_usd": cents / 100, "total_points": total, "creators": len(split),
            "paid_usd": paid / 100, "kept_usd": (cents - paid) / 100}


async def release_held(db) -> int:
    """Move cash whose hold is over to the spendable balance (once each)."""
    now = _naive(_utcnow())
    n = 0
    async for row in db.creator_earnings.find({"state": "held", "available_at": {"$lte": now}}, {"_id": 1}):
        r = await db.creator_earnings.find_one_and_update({"_id": row["_id"], "state": "held"},
                                                          {"$set": {"state": "available", "released_at": now}})
        if r and _num(r.get("cents")) > 0:
            await db.earnings_wallet.update_one({"_id": r["user_id"]},
                                                {"$inc": {"held_cents": -_num(r["cents"]), "cents": _num(r["cents"])}},
                                                upsert=True)
            n += 1
    return n


async def _credit_cash(db, user: ObjectId, cents: int) -> None:
    """Admin / tests: add cash that can be paid out now."""
    await db.earnings_wallet.update_one({"_id": user}, {"$inc": {"cents": int(cents)}}, upsert=True)


async def auto_distribute(db) -> int:
    now = _naive(_utcnow())
    if now.day < DISTRIBUTE_DAY:
        return 0
    prev = (datetime(now.year, now.month, 1) - timedelta(days=1)).strftime("%Y-%m")
    doc = await db.points_pool.find_one({"_id": prev}, {"state": 1})
    if (doc or {}).get("state") == "distributed" or not await db.points_runs.find_one({"_id": _month_closed_tag(prev)}):
        return 0
    try:
        await distribute(db, prev)
        return 1
    except PointsError:
        return 0


_ACCOUNT_RULES = {
    "mobile_money": re.compile(r"^\d{8,15}$"),
    "pix": re.compile(r"^[A-Za-z0-9@._+\-]{3,80}$"),
    "bank": re.compile(r"^[A-Za-z0-9\-]{6,40}$"),
}


def normalize_account(method: str, account: str) -> Optional[str]:
    if method not in _ACCOUNT_RULES or not isinstance(account, str):
        return None
    a = account.strip()
    if method == "mobile_money":
        # Digits only, so "+256 700 000 001" and "256-700-000-001" are the same number.
        a = re.sub(r"\D", "", a) if re.fullmatch(r"[\d\s\-+().]+", a) else a
    elif method == "bank":
        a = re.sub(r"[\s\-]", "", a).upper()
    if method == "pix":
        a = a.lower() if "@" in a else a
    return a if _ACCOUNT_RULES[method].match(a) else None


def _hint(account: str) -> str:
    return "•••• " + account[-4:] if len(account) > 4 else "••••"


async def set_payout_account(db, user: ObjectId, method: str, account: str, country: str) -> dict:
    a = normalize_account(method, account)
    if not a or not isinstance(country, str) or not re.fullmatch(r"[A-Za-z]{2}", country):
        raise PointsError(422, "Check the payout method, number and country")
    await db.payout_accounts.update_one({"_id": user}, {"$set": {
        "method": method, "account": a, "country": country.lower(), "updated_at": _naive(_utcnow())}}, upsert=True)
    return {"method": method, "country": country.lower(), "account_hint": _hint(a)}


async def request_payout(db, user: ObjectId) -> dict:
    if not await db.users.find_one({"_id": user, "is_active": True}, {"_id": 1}):
        raise PointsError(403, "This account can't request payouts")
    acct = await db.payout_accounts.find_one({"_id": user})
    if not acct:
        raise PointsError(400, "Add where we should send your money first")
    w = await db.earnings_wallet.find_one({"_id": user}) or {}
    cents = w.get("cents") if isinstance(w.get("cents"), int) else 0
    if cents < cash_minimum_cents():
        raise PointsError(400, f"You need ${cash_minimum_cents() / 100:g} to request a payout")
    if not await db.earnings_wallet.find_one_and_update({"_id": user, "cents": cents}, {"$inc": {"cents": -cents}}):
        raise PointsError(409, "Your balance changed, try again")
    pid = ObjectId()
    await db.points_payouts.insert_one({"_id": pid, "user_id": user, "cents": cents, "state": "requested",
                                        "at": _naive(_utcnow()),
                                        "payout_account": {k: acct.get(k) for k in ("method", "account", "country")}})
    return {"id": str(pid), "usd": cents / 100, "state": "requested"}


async def decide_payout(db, pid: ObjectId, approve: bool, admin: str) -> dict:
    p = await db.points_payouts.find_one_and_update(
        {"_id": pid, "state": "requested"},
        {"$set": {"state": "approved" if approve else "rejected", "decided_by": admin,
                  "decided_at": _naive(_utcnow())}}, return_document=ReturnDocument.AFTER)
    if not p:
        raise PointsError(409, "This payout was already decided")
    if not approve:
        await db.earnings_wallet.update_one({"_id": p["user_id"]}, {"$inc": {"cents": p["cents"]}}, upsert=True)
    return {"id": str(pid), "state": p["state"], "usd": p["cents"] / 100}


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
            if before["state"] == "locked" and before.get("month"):
                await _claw_back(db, before["user_id"], before["month"], _num(before.get("amount")))
    return {"video_id": str(video_id), "points_removed": removed}


async def _claw_back(db, user: ObjectId, month: str, points_removed: int) -> None:
    """A post of that month lost its points while the month's cash is on hold:
    take its part of the cash back (it stays with Wano). Each ruling's part is
    worked out from the original share, so rulings landing at the same time
    add up the same in any order; the write retries until it applies."""
    if points_removed <= 0:
        return
    for _ in range(25):
        row = await db.creator_earnings.find_one({"_id": f"share:{user}:{month}", "state": "held"})
        if not row:
            return
        orig_c = _num(row.get("orig_cents", row.get("cents")))
        orig_p = _num(row.get("orig_points", row.get("points")))
        if orig_p <= 0 or _num(row.get("cents")) <= 0:
            return
        take = min(_num(row["cents"]), -(-orig_c * points_removed // orig_p))
        done = await db.creator_earnings.find_one_and_update(
            {"_id": row["_id"], "state": "held", "cents": row["cents"]},
            {"$inc": {"cents": -take, "points": -min(points_removed, _num(row.get("points"))),
                      "clawed_back_cents": take},
             "$set": {"orig_cents": orig_c, "orig_points": orig_p}})
        if done:
            await db.earnings_wallet.update_one({"_id": user}, {"$inc": {"held_cents": -take}})
            return
    logger.error("Could not claw back cash for %s %s after retries", user, month)


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
        "earnings": await _earnings(db, user),
        "creator_status": await creator_status(db, user, _utcnow()),
        "history": [{"kind": h.get("kind"), "state": h.get("state"), "amount": h.get("amount"),
                     "month": h.get("month"), "video_id": str(h["video_id"]) if h.get("video_id") else None,
                     "at": h.get("at")} for h in hist],
    }


async def _earnings(db, user: ObjectId) -> dict:
    w = await db.earnings_wallet.find_one({"_id": user}) or {}
    cents = w.get("cents") if isinstance(w.get("cents"), int) else 0
    held = w.get("held_cents") if isinstance(w.get("held_cents"), int) else 0
    last = await db.creator_earnings.find_one({"user_id": user}, sort=[("month", -1)])
    acct = await db.payout_accounts.find_one({"_id": user})
    nxt = await db.creator_earnings.find_one({"user_id": user, "state": "held"}, sort=[("available_at", 1)])
    return {"cash_usd": cents / 100, "held_usd": max(0, held) / 100,
            "held_until": nxt.get("available_at") if nxt else None,
            "payout_minimum_usd": cash_minimum_cents() / 100,
            "can_request_payout": cents >= cash_minimum_cents() and bool(acct),
            "payout_account": {"method": acct["method"], "country": acct.get("country"),
                               "account_hint": _hint(acct["account"])} if acct else None,
            "last_share": {"month": last["month"], "points": last["points"], "usd": last["cents"] / 100} if last else None}


async def public_pool(db) -> dict:
    now = _naive(_utcnow())
    month = month_of(now)
    cfg = await _pool_cfg(db, month)
    last = await db.points_pool.find_one({"state": "distributed"}, sort=[("_id", -1)])
    return {"this_month": {"month": month, "pool_usd": _pool_cents(cfg) / 100},
            "last_month": {"month": last["_id"], "pool_usd": last.get("pool_cents", 0) / 100,
                           "usd_per_1000_points": round(last.get("pool_cents", 0) / last["total_points"] * 10, 4)
                           if last.get("total_points") else None} if last else None}


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
