"""Creator milestones (behind the "milestones" switch).

Ladders, each step awarded once as a badge in user_badges (so the existing
badge list, push and "notified" retry machinery carry them):
- followers: real followers only -- active accounts at least FOLLOWER_MIN_AGE
  old (followers_count only picks who to check; it can be inflated)
- views: total views on the creator's live public posts
- post_views: a single live public post reaching the number

The follower ladder also gives a public tier shown next to the name (feed,
profile). A tier is an achievement: it is kept if followers later dip.

A creator seen for the first time (no `milestones` state yet: everyone at
launch) gets every step they already passed, but only the highest of each
ladder is celebrated and pushed; the rest are filed quietly.

Milestone badges carry `seen: False` until the app shows the celebration
(POST /milestones/seen)."""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from bson import ObjectId

logger = logging.getLogger(__name__)

LADDERS: Dict[str, List[int]] = {
    "followers": [100, 1_000, 10_000, 50_000, 100_000, 1_000_000],
    "views": [10_000, 100_000, 1_000_000, 10_000_000],
    "post_views": [1_000, 10_000, 100_000],
}
LADDER_LABELS = {"followers": "Followers", "views": "Total views", "post_views": "Best post"}
TIERS = [(1_000, "rising", "Rising"), (10_000, "star", "Star"), (100_000, "gold", "Gold"),
         (1_000_000, "legend", "Legend")]
TIER_LABELS = {k: l for _, k, l in TIERS}
FOLLOWER_MIN_AGE = timedelta(days=3)
CHUNK = 50_000


def _int(v) -> int:
    """A safe non-negative int from a stored count (junk, inf, NaN -> 0)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    try:
        return max(0, int(v))
    except (OverflowError, ValueError):
        return 0


def _fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n // 1_000_000}M"
    if n >= 1_000:
        return f"{n // 1_000}K"
    return str(n)


def is_milestone(kind) -> bool:
    return isinstance(kind, str) and kind.rsplit("_", 1)[0] in LADDERS and kind.rsplit("_", 1)[-1].isdigit()


def label(kind: str) -> str:
    ladder, n = kind.rsplit("_", 1)
    n = _fmt(int(n))
    if ladder == "followers":
        return f"{n} followers"
    if ladder == "views":
        return f"{n} views"
    return f"A post hit {n} views"


def push_text(kind: str) -> tuple:
    ladder, n = kind.rsplit("_", 1)
    v = _fmt(int(n))
    if ladder == "followers":
        return f"You reached {v} followers!", f"{v} followers on Wano. Open the app to celebrate."
    if ladder == "views":
        return f"{v} views!", f"Your posts have been watched {v} times. Open the app to celebrate."
    return f"A post hit {v} views!", f"One of your posts reached {v} views. Open the app to see it."


def tier_for(followers_step: int) -> Optional[str]:
    key = None
    for at, k, _ in TIERS:
        if followers_step >= at:
            key = k
    return key


def _tier_rank(key) -> int:
    return next((i for i, (_, k, _l) in enumerate(TIERS) if k == key), -1)


def _live_post() -> dict:
    return {"is_active": True, "privacy": "public", "moderation.state": {"$nin": ["held", "removed"]},
            "$or": [{"status": {"$exists": False}}, {"status": {"$ne": "scheduled"}}]}


async def _real_followers(db, followers, now: datetime) -> int:
    ids = []
    for f in followers if isinstance(followers, list) else []:
        if isinstance(f, ObjectId):
            ids.append(f)
        elif isinstance(f, str) and ObjectId.is_valid(f):
            ids.append(ObjectId(f))
    ids = list(dict.fromkeys(ids))
    total, cutoff = 0, now - FOLLOWER_MIN_AGE
    for i in range(0, len(ids), CHUNK):
        total += await db.users.count_documents({"_id": {"$in": ids[i:i + CHUNK]}, "is_active": True,
                                                 "created_at": {"$lte": cutoff}})
    return total


async def _view_stats(db, creator_ids: Optional[List[ObjectId]] = None) -> Dict[ObjectId, dict]:
    match = {**_live_post(), "views_count": {"$gt": 0}}
    if creator_ids is not None:
        match["creator_id"] = {"$in": creator_ids}
    out = {}
    async for r in db.videos.aggregate([
        {"$match": match},
        {"$group": {"_id": "$creator_id", "views": {"$sum": "$views_count"}, "best": {"$max": "$views_count"}}},
    ]):
        if isinstance(r["_id"], ObjectId):
            out[r["_id"]] = {"views": _int(r.get("views")), "best": _int(r.get("best"))}
    return out


def _reached(ladder: str, value: int) -> List[int]:
    return [n for n in LADDERS[ladder] if value >= n]


async def _enabled(db) -> bool:
    from app.services import features
    return (await features.current(db)).get("milestones") is True


async def award(db, now: Optional[datetime] = None) -> dict:
    """Award every step creators have newly passed. Run by the growth loop."""
    out = {"awarded": 0, "creators": 0}
    if not await _enabled(db):
        return out
    now = now or datetime.utcnow()
    stats = await _view_stats(db)
    candidates = {c for c, s in stats.items()
                  if s["views"] >= LADDERS["views"][0] or s["best"] >= LADDERS["post_views"][0]}
    candidates |= set(await db.users.distinct("_id", {"is_active": True,
                                                       "followers_count": {"$gte": LADDERS["followers"][0]}}))
    for cid in candidates:
        try:
            out["awarded"] += await _award_one(db, cid, stats.get(cid) or {"views": 0, "best": 0}, now)
            out["creators"] += 1
        except Exception:
            logger.exception("milestones for %s failed", cid)
    return out


async def _award_one(db, cid: ObjectId, s: dict, now: datetime) -> int:
    user = await db.users.find_one({"_id": cid, "is_active": True},
                                   {"followers_count": 1, "milestones": 1, "tier": 1})
    if not user:
        return 0
    state = user.get("milestones") if isinstance(user.get("milestones"), dict) else None
    first = state is None
    state = dict(state or {})
    values = {"views": s["views"], "post_views": s["best"]}
    count = user.get("followers_count") if isinstance(user.get("followers_count"), int) else 0
    next_f = next((n for n in LADDERS["followers"] if n > int(state.get("followers") or 0)), None)
    values["followers"] = 0
    if next_f is not None and count >= next_f:
        full = await db.users.find_one({"_id": cid}, {"followers": 1})
        values["followers"] = await _real_followers(db, (full or {}).get("followers"), now)
    awarded, sets = 0, {}
    for ladder, value in values.items():
        have = int(state.get(ladder) or 0)
        steps = [n for n in _reached(ladder, value) if n > have]
        if not steps:
            continue
        top = steps[-1]
        for n in steps:
            loud = n == top  # only the newest step is celebrated; skipped ones are filed quietly
            doc = {"_id": f"{cid}:{ladder}_{n}", "user_id": cid, "kind": f"{ladder}_{n}", "at": now,
                   "notified": not loud, "seen": not loud, "value": n}
            try:
                await db.user_badges.insert_one(doc)
                awarded += 1
            except Exception:
                pass  # already have it
        sets[f"milestones.{ladder}"] = top
        if ladder == "followers":
            t = tier_for(top)
            if t and _tier_rank(t) > _tier_rank(user.get("tier")):
                sets["tier"] = t
    if first and not sets:
        sets["milestones"] = {}
    if sets:
        await db.users.update_one({"_id": cid}, {"$set": sets})
    return awarded


async def summary(db, me: ObjectId) -> dict:
    user = await db.users.find_one({"_id": me}, {"followers_count": 1, "milestones": 1, "tier": 1}) or {}
    s = (await _view_stats(db, [me])).get(me) or {"views": 0, "best": 0}
    current = {"followers": _int(user.get("followers_count")), "views": s["views"],
               "post_views": s["best"]}
    rows = await db.user_badges.find({"user_id": me}).sort([("at", -1), ("_id", 1)]).to_list(200)
    rows = [r for r in rows if is_milestone(r.get("kind"))]
    reached = {k: sorted(int(r["kind"].rsplit("_", 1)[1]) for r in rows if r["kind"].startswith(k + "_")
                         and r["kind"].rsplit("_", 1)[0] == k) for k in LADDERS}
    ladders = []
    for k, steps in LADDERS.items():
        nxt = next((n for n in steps if n not in reached[k]), None)
        ladders.append({"key": k, "label": LADDER_LABELS[k], "current": current[k], "next": nxt,
                        "reached": reached[k]})
    tier = user.get("tier") if user.get("tier") in TIER_LABELS else None
    nt = next(((at, k, l) for at, k, l in TIERS if _tier_rank(k) > _tier_rank(tier)), None)
    celebrate = [{"id": r["_id"], "kind": r["kind"], "label": label(r["kind"]), "value": r.get("value"),
                  "at": r.get("at")} for r in rows if r.get("seen") is False]
    return {
        "tier": {"key": tier, "label": TIER_LABELS[tier]} if tier else None,
        "next_tier": {"key": nt[1], "label": nt[2], "at": nt[0]} if nt else None,
        "ladders": ladders,
        "celebrate": celebrate[:3],
    }


async def mark_seen(db, me: ObjectId, ids: List[str]) -> int:
    ids = [i for i in ids if isinstance(i, str) and i.startswith(f"{me}:")]
    if not ids:
        return 0
    r = await db.user_badges.update_many({"_id": {"$in": ids}, "user_id": me}, {"$set": {"seen": True}})
    return r.modified_count
