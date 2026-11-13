"""The For You feed: Recombee's personal picks blended with Wano's own sources,
then re-ranked with feed rules.

Sources (share of a page, for a viewer in the "mix" variant):
  personal   ~70%  Recombee RecommendItemsToUser / RecommendNextItems
  fresh      ~15%  posts under 72 h old with few views, so new work gets tested
  trending   ~10%  last 7 days, ranked by engagement velocity
  following   ~5%  recent posts from creators the viewer follows
  local      ~10%  popular recent posts from creators in the viewer's city (when
                   they set one: PUT /users/me/city), taken from personal
The "control" variant is Recombee only, with the same feed rules, so the two
can be compared (FOR_YOU_MIX_ROLLOUT = percent of viewers in "mix").

Feed rules for every page:
  - nothing the viewer already watched (watch_history, 60 days)
  - nothing they were shown in the last 24 h (feed_impressions)
  - the same creator never twice in a row, and at most 2 times per page
  - the database decides what is showable (Recombee's copy can lag)
If Recombee is down, its share is filled from popular recent posts, so the
feed never goes empty.
"""

import asyncio
import hashlib
import logging
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from bson import ObjectId
from recombee_api_client.api_requests import RecommendItemsToUser, RecommendNextItems

from app.core.config import settings
from app.services.recombee_service import recombee_send
from app.services.recommendations import LEGACY_ANON_USER

logger = logging.getLogger(__name__)

WATCHED_LOOKBACK_DAYS = 60
WATCHED_MAX = 3000
IMPRESSION_LOOKBACK_HOURS = 24
# Newest first, so a very heavy day can't grow the exclusion list without end.
IMPRESSION_MAX = 1000
FRESH_MAX_AGE_HOURS = 72
FRESH_MAX_VIEWS = 500
TRENDING_DAYS = 7
FOLLOWING_DAYS = 14
MAX_PER_CREATOR = 2
DIVERSITY = 0.3
ROTATION_RATE = 0.5

SHARES = {"fresh": 0.15, "trending": 0.10, "following": 0.05, "local": 0.10}
LOCAL_DAYS = 7
LOCAL_CREATORS = 3000


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def showable_filter() -> dict:
    return {
        "is_active": True,
        "privacy": "public",
        "isReadyToStream": {"$ne": False},
        "status": {"$nin": ["scheduled", "publishing", "cancelled"]},
    }


def variant_for(viewer: str) -> str:
    rollout = max(0, min(100, int(getattr(settings, "FOR_YOU_MIX_ROLLOUT", 50))))
    bucket = int(hashlib.sha256(viewer.encode()).hexdigest()[:8], 16) % 100
    return "mix" if bucket < rollout else "control"


def _quotas(limit: int, variant: str, follows_anyone: bool, has_city: bool = False) -> Dict[str, int]:
    if variant != "mix":
        return {"personal": limit}
    q = {}
    for src, share in SHARES.items():
        if src == "following" and not follows_anyone:
            continue
        if src == "local" and not has_city:
            continue
        q[src] = max(1, round(share * limit)) if limit >= 6 else 0
    q["personal"] = max(0, limit - sum(q.values()))
    return q


def _slot_plan(limit: int, quotas: Dict[str, int]) -> List[str]:
    """Spread the non-personal slots evenly through the page."""
    plan = ["personal"] * limit
    taken = set()
    for src in ("fresh", "trending", "following", "local"):
        k = quotas.get(src, 0)
        for i in range(k):
            pos = min(limit - 1, max(1, round((i + 1) * limit / (k + 1))))
            while pos in taken and pos < limit - 1:
                pos += 1
            while pos in taken and pos > 0:
                pos -= 1
            if pos not in taken:
                taken.add(pos)
                plan[pos] = src
    return plan


# ------------------------------------------------------------------ candidates

async def _personal(viewer: str, scenario: str, count: int, next_recomm_id: Optional[str],
                    user_tags: List[str]):
    """(ids, recomm_id) from Recombee; ([], None) if it fails."""
    try:
        if next_recomm_id:
            req = RecommendNextItems(next_recomm_id, count)
        else:
            kw = dict(
                scenario=scenario, cascade_create=True, rotation_rate=ROTATION_RATE,
                diversity=DIVERSITY,
                filter="'is_active' == true AND 'privacy' == \"public\" AND 'is_ready_to_stream' == true",
            )
            if user_tags:
                kw["booster"] = "if size(intersection(context_user[\"tags\"], 'hashtags')) > 0 then 1.5 else 1"
            req = RecommendItemsToUser(viewer, count, **kw)
        req.timeout = 5000
        result = await recombee_send(req) or {}
        ids = [r.get("id") for r in result.get("recomms", []) if ObjectId.is_valid(r.get("id", ""))]
        return ids, result.get("recommId")
    except Exception:
        logger.warning("Recombee recommendation failed; filling from Wano's own sources")
        return [], None


async def _docs_for(db, ids: List[str], excluded: set) -> List[dict]:
    oids = [ObjectId(i) for i in ids if i not in excluded]
    if not oids:
        return []
    docs = await db.videos.find({"_id": {"$in": oids}, **showable_filter()},
                                {"creator_id": 1, "created_at": 1}).to_list(length=len(oids))
    by_id = {str(d["_id"]): d for d in docs}
    return [by_id[i] for i in ids if i in by_id]


async def _fresh(db, excluded_oids, own) -> List[dict]:
    """New posts getting their first viewers (app/services/creators.py): the
    least-seen first, then the ones people watched, streak creators higher."""
    from app.services import creators
    return (await creators.first_viewers(db, excluded_oids, own))[:60]


async def _trending(db, excluded_oids, own, country=None) -> List[dict]:
    """Momentum-ranked trending (app/services/trending.py), viewer's country first."""
    from app.services import trending
    return await trending.top(db, country=country, limit=40, exclude_oids=excluded_oids, exclude_creator=own)


async def _local(db, city_key: str, excluded_oids, own) -> List[dict]:
    """Popular recent posts by creators in the viewer's city."""
    ids = [u["_id"] async for u in db.users.find(
        {"localization.city_key": city_key, "is_active": {"$ne": False}}, {"_id": 1}).limit(LOCAL_CREATORS)]
    ids = [i for i in ids if i != own]
    if not ids:
        return []
    q = {**showable_filter(), "moderation.state": {"$nin": ["held", "removed"]}, "creator_id": {"$in": ids},
         "created_at": {"$gte": _now() - timedelta(days=LOCAL_DAYS)}}
    if excluded_oids:
        q["_id"] = {"$nin": excluded_oids}
    return await db.videos.find(q, {"creator_id": 1, "created_at": 1}).sort(
        [("likes_count", -1), ("created_at", -1)]).limit(40).to_list(40)


async def _following(db, following: List[ObjectId], excluded_oids) -> List[dict]:
    if not following:
        return []
    q = {**showable_filter(), "creator_id": {"$in": following[:500]},
         "created_at": {"$gte": _now() - timedelta(days=FOLLOWING_DAYS)}, "_id": {"$nin": excluded_oids}}
    return await db.videos.find(q, {"creator_id": 1, "created_at": 1}).sort("created_at", -1).limit(40).to_list(40)


POPULAR_POOL_SECONDS = 300
POPULAR_POOL_SIZE = 400
_popular_cache: dict = {}


def clear_cache():
    _popular_cache.clear()


async def _popular_pool(db) -> List[dict]:
    """Most-liked posts of the last 60 days plus the newest, shared by every
    request for POPULAR_POOL_SECONDS (sorting by likes has no index, so doing it
    per request read two months of posts each time Recombee came up short)."""
    q = {**showable_filter(), "created_at": {"$gte": _now() - timedelta(days=60)}}
    docs = await db.videos.find(q, {"creator_id": 1, "created_at": 1}
                                ).sort("likes_count", -1).limit(POPULAR_POOL_SIZE).to_list(POPULAR_POOL_SIZE)
    seen = {d["_id"] for d in docs}
    newest = await db.videos.find(showable_filter(), {"creator_id": 1, "created_at": 1}
                                  ).sort("created_at", -1).limit(150).to_list(150)
    return docs + [d for d in newest if d["_id"] not in seen]


async def _popular_backup(db, excluded_oids, own) -> List[dict]:
    """Stands in for Recombee when it is unreachable. Never the viewer's own posts."""
    import time as _time
    hit = _popular_cache.get("pool")
    if not hit or _time.monotonic() - hit[0] > POPULAR_POOL_SECONDS:
        hit = (_time.monotonic(), await _popular_pool(db))
        _popular_cache["pool"] = hit
    excluded = set(excluded_oids)
    docs = [d for d in hit[1] if d["_id"] not in excluded and (own is None or d.get("creator_id") != own)]
    random.shuffle(docs)
    docs = docs[:200]
    # The pool can be a few minutes old: re-check these few by id (cheap) so a post
    # made private or deleted since never slips through.
    if docs:
        ok = {d["_id"] async for d in db.videos.find({"_id": {"$in": [d["_id"] for d in docs]}, **showable_filter()},
                                                     {"_id": 1})}
        docs = [d for d in docs if d["_id"] in ok]
    return docs


# ------------------------------------------------------------------ assembly

class Page:
    def __init__(self):
        self.items: List[dict] = []      # {"id", "creator_id", "source"}
        self.recomm_id: Optional[str] = None
        self.variant: str = "control"
        self.timing: Dict[str, float] = {}   # ms per step, for the Server-Timing header


def _pick(queue: List[dict], used: set, counts: Dict[str, int], prev: Optional[str], strict: bool):
    for i, d in enumerate(queue):
        vid, creator = str(d["_id"]), str(d.get("creator_id"))
        if vid in used:
            continue
        if strict and (creator == prev or counts.get(creator, 0) >= MAX_PER_CREATOR):
            continue
        return queue.pop(i)
    return None


async def build_page(db, *, viewer: str, current_user: Optional[str], scenario: str, limit: int,
                     next_recomm_id: Optional[str], featured: Optional[dict]) -> Page:
    page = Page()
    page.variant = variant_for(viewer)

    user = {}
    if current_user and ObjectId.is_valid(current_user):
        user = await db.users.find_one({"_id": ObjectId(current_user)},
                                       {"following": 1, "tags": 1, "localization": 1}) or {}
    following = [f for f in (user.get("following") or []) if isinstance(f, ObjectId)]
    own = ObjectId(current_user) if current_user and ObjectId.is_valid(current_user) else None

    slots = limit - (1 if featured else 0)
    # Over-fetch from Recombee: some picks get dropped by the rules below.
    want = min(100, max(slots * 5, 20))

    # What this viewer has already seen. These lookups and Recombee don't
    # depend on each other, so they run at the same time (one after another
    # they were most of the feed's ~1 s on the server).
    async def _watched() -> set:
        if not own:
            return set()
        # The most recent watches only: a heavy viewer can have tens of thousands.
        since = _now() - timedelta(days=WATCHED_LOOKBACK_DAYS)
        cursor = (db.watch_history.find({"user_id": own, "watched_at": {"$gte": since}}, {"video_id": 1})
                  .sort("watched_at", -1).limit(WATCHED_MAX))
        return {str(w["video_id"]) async for w in cursor}

    async def _never() -> set:
        # "Not interested": never shown again, however old, however empty the feed gets.
        if not own:
            return set()
        return {str(w["video_id"]) async for w in
                db.watch_history.find({"user_id": own, "not_interested": True}, {"video_id": 1}).limit(5000)}

    async def _shown() -> set:
        # Everyone on a build that sends no device id shares one viewer id, so
        # those impressions say nothing about the person asking: excluding them
        # would hide more and more of the catalogue from every logged-out
        # viewer as the day's traffic piles up, until the feed came back empty.
        if viewer == LEGACY_ANON_USER:
            return set()
        since = _now() - timedelta(hours=IMPRESSION_LOOKBACK_HOURS)
        cursor = (db.feed_impressions.find({"viewer": viewer, "at": {"$gte": since}}, {"video_id": 1})
                  .sort("at", -1).limit(IMPRESSION_MAX))
        return {str(imp["video_id"]) async for imp in cursor}

    import time as _time

    async def _timed(name, coro):
        t = _time.perf_counter()
        try:
            return await coro
        finally:
            page.timing[name] = (_time.perf_counter() - t) * 1000

    watched, never, shown, (ids, page.recomm_id) = await asyncio.gather(
        _timed("watched", _watched()), _never(), _timed("shown", _shown()),
        _timed("recombee", _personal(viewer, scenario, want, next_recomm_id, user.get("tags") or [])))
    t_assemble = _time.perf_counter()
    featured_id = str(featured["_id"]) if featured else None
    if featured_id:
        never.add(featured_id)

    # A heavy viewer can have seen nearly everything. Rather than an empty
    # feed, loosen step by step: first allow posts they watched (but weren't
    # shown today), then anything but "Not interested". Unseen posts always
    # come first; the app also drops repeats within one session.
    tiers = [watched | shown | never, shown | never, never]
    picked: list = []
    for excluded in tiers:
        got = await _assemble(db, page, ids, excluded | {it["id"] for it in picked}, own, user, following,
                              slots - len(picked), featured, picked)
        picked += got
        if len(picked) >= max(1, (slots + 1) // 2):
            break
    page.items = picked
    page.timing["assemble"] = (_time.perf_counter() - t_assemble) * 1000
    return page


async def _assemble(db, page, ids, excluded: set, own, user: dict, following: list, slots: int,
                    featured: Optional[dict], already: list) -> list:
    """One pass of the page rules with this exclusion set. Returns new items."""
    if slots <= 0:
        return []
    items: list = []
    excluded_oids = [ObjectId(x) for x in excluded if ObjectId.is_valid(x)]
    city_key = (user.get("localization") or {}).get("city_key")
    city_key = city_key if isinstance(city_key, str) and city_key else None
    quotas = _quotas(slots, page.variant, bool(following), bool(city_key))
    # Every source at the same time: they don't depend on each other.
    jobs = {"personal": _docs_for(db, ids, excluded) if ids else _popular_backup(db, excluded_oids, own)}
    if page.variant == "mix":
        jobs["fresh"] = _fresh(db, excluded_oids, own)
        jobs["trending"] = _trending(db, excluded_oids, own, ((user.get("localization") or {}).get("country")))
        jobs["following"] = _following(db, following, excluded_oids)
        if city_key:
            jobs["local"] = _local(db, city_key, excluded_oids, own)
    queues = dict(zip(jobs.keys(), await asyncio.gather(*jobs.values())))

    # Each post counts for its most specific source only.
    claimed = set()
    for k in ("following", "local", "trending", "fresh", "personal"):   # a rising post is "trending", not "fresh"
        if k in queues:
            queues[k] = [d for d in queues[k] if str(d["_id"]) not in claimed]
            claimed.update(str(d["_id"]) for d in queues[k])

    # Drop posts whose creator is banned or deleted.
    creator_ids = {d.get("creator_id") for q in queues.values() for d in q if d.get("creator_id")}
    if creator_ids:
        # One read for both: who exists, and who of them is switched off.
        found = [u async for u in db.users.find({"_id": {"$in": list(creator_ids)}}, {"_id": 1, "is_active": 1})]
        known = {u["_id"] for u in found}
        inactive = {u["_id"] for u in found if u.get("is_active") is False}
        for k in queues:
            queues[k] = [d for d in queues[k] if d.get("creator_id") in known and d.get("creator_id") not in inactive]

    used = set(excluded)
    counts: Dict[str, int] = {}
    prev = None
    if featured:
        prev = str(featured.get("creator_id"))
        counts[prev] = 1
    for it in already:
        c = it.get("creator_id")
        counts[c] = counts.get(c, 0) + 1
        prev = c

    backup_order = ["personal", "trending", "fresh", "following", "local", "popular"]
    backup_loaded = False

    async def load_backup():
        # Popular recent posts, only when the other sources can't fill the page
        # within the feed rules (e.g. Recombee keeps suggesting one creator).
        nonlocal backup_loaded
        backup_loaded = True
        docs = await _popular_backup(db, [ObjectId(x) for x in used if ObjectId.is_valid(x)], own)
        known_ok = {u["_id"] async for u in db.users.find(
            {"_id": {"$in": list({d.get("creator_id") for d in docs})}, "is_active": {"$ne": False}}, {"_id": 1})}
        queues["popular"] = [d for d in docs if d.get("creator_id") in known_ok]

    for src in _slot_plan(slots, quotas):
        choice = None
        for strict in (True, False):
            for attempt in range(2):
                for s in [src] + [b for b in backup_order if b != src]:
                    if s not in queues:
                        continue
                    d = _pick(queues[s], used, counts, prev, strict)
                    if d is not None:
                        choice = (d, s)
                        break
                if choice or backup_loaded:
                    break
                await load_backup()
            if choice:
                break
        if not choice:
            break
        d, s = choice
        vid, creator = str(d["_id"]), str(d.get("creator_id"))
        used.add(vid)
        counts[creator] = counts.get(creator, 0) + 1
        prev = creator
        # Paid points boosts are labelled, so those viewers don't earn the creator points back.
        items.append({"id": vid, "creator_id": creator, "source": "boost" if d.get("stage") == "paid" else s})

    return items


async def record_impressions(db, *, viewer: str, current_user: Optional[str], variant: str,
                             recomm_id: Optional[str], items: List[dict]) -> None:
    if not items or viewer == LEGACY_ANON_USER:
        # Nothing is excluded for the shared legacy viewer, and a watch
        # recorded under it would attach to a stranger's impression and skew
        # the A/B figures, so don't store these at all.
        return
    now = _now()
    docs = [{
        "viewer": viewer, "user_id": ObjectId(current_user) if current_user and ObjectId.is_valid(current_user) else None,
        "video_id": ObjectId(it["id"]), "source": it.get("source"), "variant": variant,
        "recomm_id": recomm_id, "position": i, "at": now,
    } for i, it in enumerate(items)]
    try:
        await db.feed_impressions.insert_many(docs, ordered=False)
    except Exception:
        logger.warning("Could not record %d feed impressions", len(docs))
    from app.services import creators
    await creators.record_impressions(db, [it["id"] for it in items])


async def record_watch(db, *, viewer: str, video_id: str, portion: float) -> None:
    """Attach the watch result to the most recent impression of that video."""
    if viewer == LEGACY_ANON_USER:
        return   # no impressions are stored for the shared legacy viewer
    since = _now() - timedelta(days=2)
    imp = await db.feed_impressions.find_one(
        {"viewer": viewer, "video_id": ObjectId(video_id), "at": {"$gte": since}},
        sort=[("at", -1)],
    )
    if imp:
        res = await db.feed_impressions.update_one(
            {"_id": imp["_id"], "portion": {"$exists": False}},
            {"$set": {"portion": float(portion), "watched_at": _now()}},
        )
        if getattr(res, "modified_count", 0):
            # First report for this impression: a For You viewer reached the post.
            from app.services import creators
            await creators.record_fy_watch(db, video_id, portion)
        else:
            await db.feed_impressions.update_one(
                {"_id": imp["_id"]},
                {"$max": {"portion": float(portion)}, "$set": {"watched_at": _now()}},
            )


async def report(db, days: int) -> dict:
    """Per variant and per source, computed inside MongoDB (one aggregate)."""
    since = _now() - timedelta(days=days)
    p = {"$ifNull": ["$portion", -1]}          # -1 = shown but never reported watched
    pipeline = [
        {"$match": {"at": {"$gte": since}}},
        {"$group": {
            "_id": {"variant": {"$ifNull": ["$variant", "unknown"]}, "source": {"$ifNull": ["$source", "unknown"]}},
            "impressions": {"$sum": 1},
            "watched": {"$sum": {"$cond": [{"$gte": [p, 0]}, 1, 0]}},
            "portion_sum": {"$sum": {"$cond": [{"$gte": [p, 0]}, p, 0]}},
            "completed": {"$sum": {"$cond": [{"$gte": [p, 0.9]}, 1, 0]}},
            "skipped": {"$sum": {"$cond": [{"$and": [{"$gte": [p, 0]}, {"$lt": [p, 0.2]}]}, 1, 0]}},
        }},
    ]
    keys = ("impressions", "watched", "portion_sum", "completed", "skipped")
    out: Dict[str, dict] = {}
    async for row in db.feed_impressions.aggregate(pipeline):
        v = out.setdefault(row["_id"]["variant"], {**{k: 0 for k in keys}, "by_source": {}})
        v["by_source"][row["_id"]["source"]] = {k: row[k] for k in keys}
        for k in keys:
            v[k] += row[k]

    def finish(row):
        w = row["watched"] or 1
        row["avg_portion"] = round(row.pop("portion_sum") / w, 4)
        row["completion_rate"] = round(row.pop("completed") / w, 4)
        row["skip_rate"] = round(row.pop("skipped") / w, 4)
        row["watch_rate"] = round(row["watched"] / (row["impressions"] or 1), 4)
        return row

    for v in out.values():
        for src in v["by_source"].values():
            finish(src)
        finish(v)
    return {"days": days, "since": since.isoformat(), "variants": out}
