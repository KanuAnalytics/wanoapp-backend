"""Trending: what is taking off right now, not what was big last month.

How a video's trending score is worked out
  Engagement is recorded per video per hour (video_stats_hourly) as it
  happens: views, finished watches, skips, likes, comments, shares. Only the
  last 48 hours count, and each hour counts half as much as the one 12 hours
  newer, so momentum fades when people stop watching.

      positive = sum over hours of decay x ( non-skipped views + 3*finished
                 + 4*likes + 6*comments + 8*shares
                 + 0.1*(views and finished watches by logged-out devices) )
      momentum = positive - 1.5*skips   (skips take off at most 40%)
      quality  = (finished + 2) / (non-skipped views + 6)   smoothed finish rate
      score    = momentum x (0.7 + 0.6*quality)

  To trend, a video needs at least 5 engagements from real accounts in that
  window (likes, comments, shares, finished watches) and positive momentum.
  Logged-out views barely count, so inventing device ids can't buy trending.

Anti-gaming
  Each account's like, comment and share counts once per video, and a
  creator's own engagement with their post never counts.

Anti-gaming on views
  A view is counted once per viewer per video every 6 hours (view_dedupe),
  and logged-out views from one IP address are capped at 1,000 an hour
  (signed-in views are not IP-capped: mobile carriers put many people
  behind one address).

Before there is enough hourly data (right after deploy), videos from the last
7 days with real engagement fill in, ranked by engagement per hour of age,
always after the momentum-ranked ones.

The ranking is cached for 60 s per process. Every request re-checks the
database, so a video deleted or made private drops out immediately, and the
viewer's country comes first, then the rest of Wano.
"""

import asyncio
import logging
import math
import time
from collections import defaultdict, deque

from pymongo.errors import DuplicateKeyError
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from bson import ObjectId
from pymongo import ReturnDocument

logger = logging.getLogger(__name__)

WINDOW_HOURS = 48
HALF_LIFE_HOURS = 12
MIN_ACCOUNT_ENGAGEMENTS = 5
FALLBACK_DAYS = 7
FALLBACK_MIN_ENGAGEMENT = 5
VIEW_DEDUPE_HOURS = 6
IP_VIEWS_PER_HOUR = 1000   # logged-out views only: carriers share one IP across many people
IP_VIDEO_VIEWS_PER_HOUR = 50   # logged-out views one IP can add to one video
MAX_TRACKED_IPS = 100_000      # bounded memory however many addresses show up
CACHE_SECONDS = 60
RANK_SIZE = 300

ANON_WEIGHT = 0.1
MAX_SKIP_PENALTY = 0.4


def _now():
    return datetime.now(timezone.utc)


def _naive_utc(dt):
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _n(value) -> float:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return v if v > 0 and math.isfinite(v) else 0.0


def _hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


# ------------------------------------------------------------------ recording

_ip_hits: Dict[str, deque] = {}


_last_scan = 0.0


def _prune(table: Dict[str, deque], now: float) -> None:
    """Keep tracking tables bounded. A full scan for idle entries runs at most
    once a minute; otherwise the oldest entry is dropped (O(1)), so a flood of
    new addresses can't make every request slow."""
    global _last_scan
    if len(table) < MAX_TRACKED_IPS:
        return
    if now - _last_scan > 60:
        _last_scan = now
        for k in [k for k, q in table.items() if not q or now - q[-1] > 3600]:
            del table[k]
    while len(table) >= MAX_TRACKED_IPS:
        del table[next(iter(table))]


def _allow(table: Dict[str, deque], key: str, per_hour: int) -> bool:
    now = time.monotonic()
    q = table.get(key)
    if q is None:
        _prune(table, now)
        q = table[key] = deque()
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= per_hour:
        return False
    q.append(now)
    return True


def _ip_allows(ip: Optional[str], video_id: Optional[str] = None) -> bool:
    if not ip:
        return True
    if video_id is not None and not _allow(_ip_hits, f"{ip}|{video_id}", IP_VIDEO_VIEWS_PER_HOUR):
        return False
    return _allow(_ip_hits, ip, IP_VIEWS_PER_HOUR)


async def count_view_once(db, *, viewer: str, video_id: str, ip: Optional[str]) -> bool:
    """True if this watch should count as a new view."""
    now = _now()
    anonymous = viewer.startswith("anon-") or viewer == "temp-user"
    if viewer == "temp-user" and ip:
        viewer = f"ip:{ip}"   # old app builds send no device id: one viewer per address
    key = f"{viewer}:{video_id}"
    try:
        prev = await db.view_dedupe.find_one_and_update(
            {"_id": key}, {"$setOnInsert": {"at": now}}, upsert=True,
            return_document=ReturnDocument.BEFORE,
        )
    except DuplicateKeyError:
        return False   # a simultaneous request for the same view already counted it
    if prev is not None:
        last = prev.get("at")
        if last is not None and _naive_utc(last) > _naive_utc(now - timedelta(hours=VIEW_DEDUPE_HOURS)):
            return False
        await db.view_dedupe.update_one({"_id": key}, {"$set": {"at": now}})
    if anonymous:
        return _ip_allows(ip, video_id)
    return True


async def record(db, video_id: str, **counts) -> None:
    """Add engagement to the video's current hour bucket."""
    if not ObjectId.is_valid(video_id):
        return
    incs = {k: v for k, v in counts.items() if v}
    if not incs:
        return
    hour = _hour(_now())
    try:
        await db.video_stats_hourly.update_one(
            {"_id": f"{video_id}:{hour.strftime('%Y%m%d%H')}"},
            {"$inc": incs, "$setOnInsert": {"video_id": ObjectId(video_id), "hour": hour}},
            upsert=True,
        )
    except Exception:
        pass   # trending is best-effort; never break the action that triggered it


async def record_once(db, video_id: str, *, actor: Optional[str], creator, kind: str, **counts) -> None:
    """Record an account's like/comment/share at most once per video (within the
    trending window), and never for the creator's own video."""
    if not actor or (creator is not None and str(creator) == str(actor)):
        return
    try:
        res = await db.engagement_once.update_one(
            {"_id": f"{kind}:{actor}:{video_id}"},
            {"$setOnInsert": {"at": _now()}}, upsert=True,
        )
    except Exception:
        return
    if getattr(res, "upserted_id", None) is None:
        return   # this person already counted for this video
    await record(db, video_id, **counts)
    from app.services import creators
    await creators.record_engagement(db, creator, kind)


async def record_watch(db, video_id: str, *, logged_in: bool, portion: float,
                       viewer: Optional[str] = None, creator=None) -> None:
    finished = portion >= 0.9
    skipped = portion < 0.2
    if logged_in:
        if creator is not None and viewer is not None and str(creator) == str(viewer):
            return   # creators watching their own post don't make it trend
        await record(db, video_id, views=1, finished=int(finished), skips=int(skipped))
    else:
        await record(db, video_id, anon_views=1, anon_finished=int(finished))


# ------------------------------------------------------------------ ranking

def _showable() -> dict:
    return {"is_active": True, "privacy": "public", "isReadyToStream": {"$ne": False},
            "status": {"$nin": ["scheduled", "publishing", "cancelled"]}}


async def _compute(db) -> List[Tuple[str, float, Optional[str], str]]:
    """Global ranking: [(video_id, score, country, basis)] best first."""
    now = _now()
    since = _hour(now - timedelta(hours=WINDOW_HOURS))
    agg: Dict[str, Dict[str, float]] = {}
    async for row in db.video_stats_hourly.find({"hour": {"$gte": _naive_utc(since)}}):
        vid = str(row.get("video_id"))
        if not ObjectId.is_valid(vid) or not isinstance(row.get("hour"), datetime):
            continue
        age_h = max(0.0, (now - row["hour"].replace(tzinfo=timezone.utc)).total_seconds() / 3600)
        w = math.pow(0.5, age_h / HALF_LIFE_HOURS)
        a = agg.setdefault(vid, defaultdict(float))
        g = lambda k: _n(row.get(k))  # noqa: E731
        watched = max(0.0, g("views") - g("skips"))   # people who didn't swipe straight away
        a["pos"] += w * (watched + 3 * g("finished") + 4 * g("likes") + 6 * g("comments")
                         + 8 * g("shares") + ANON_WEIGHT * (g("anon_views") + g("anon_finished")))
        a["neg"] += w * 1.5 * g("skips")
        a["acct"] += g("likes") + g("comments") + g("shares") + g("finished")
        a["views"] += watched + g("anon_views")
        a["finished"] += g("finished") + g("anon_finished")

    momentum_ranked = []
    for vid, a in agg.items():
        # Skips can take off at most 40%: enough to sink a genuinely boring
        # video's rank, not enough for a group of accounts to bury a rival.
        momentum = a["pos"] - min(a["neg"], MAX_SKIP_PENALTY * a["pos"])
        if a["acct"] < MIN_ACCOUNT_ENGAGEMENTS or momentum <= 0:
            continue
        quality = min(1.0, (a["finished"] + 2) / (a["views"] + 6))
        momentum_ranked.append((vid, momentum * (0.7 + 0.6 * quality)))
    momentum_ranked.sort(key=lambda x: x[1], reverse=True)
    momentum_ranked = momentum_ranked[:RANK_SIZE]

    # Bootstrap / thin data: recent posts with real engagement, after the above.
    seen = {v for v, _ in momentum_ranked}
    fallback = []
    if len(momentum_ranked) < RANK_SIZE:
        q = {**_showable(), "created_at": {"$gte": _naive_utc(now - timedelta(days=FALLBACK_DAYS))}}
        cursor = db.videos.find(q, {"created_at": 1, "views_count": 1, "likes_count": 1,
                                    "comments_count": 1, "bookmarks_count": 1, "shares_count": 1}
                                ).sort("likes_count", -1).limit(1000)
        async for d in cursor:
            vid = str(d["_id"])
            if vid in seen:
                continue
            likes, comments, shares = _n(d.get("likes_count")), _n(d.get("comments_count")), _n(d.get("shares_count"))
            if likes + comments + shares < FALLBACK_MIN_ENGAGEMENT:
                continue
            created = d.get("created_at")
            if not isinstance(created, datetime):
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_h = max(0.0, (now - created).total_seconds() / 3600)
            eng = (likes * 4 + comments * 6 + _n(d.get("bookmarks_count")) * 4
                   + shares * 8 + _n(d.get("views_count")) * 0.1)
            fallback.append((vid, eng / math.pow(age_h + 2, 1.5)))
        fallback.sort(key=lambda x: x[1], reverse=True)
        fallback = fallback[: RANK_SIZE - len(momentum_ranked)]

    ranked = [(v, s, "momentum") for v, s in momentum_ranked] + [(v, s, "recent") for v, s in fallback]
    if not ranked:
        return []
    countries = {}
    async for d in db.videos.find({"_id": {"$in": [ObjectId(v) for v, _, _ in ranked]}}, {"country": 1}):
        c = d.get("country")
        countries[str(d["_id"])] = c if isinstance(c, str) else None
    return [(v, s, countries.get(v), basis) for v, s, basis in ranked]


_cache: Dict[str, Tuple[float, list]] = {}
_lock = asyncio.Lock()


def clear_cache() -> None:
    _cache.clear()


def reset_limits() -> None:
    _ip_hits.clear()


async def _ranking(db) -> list:
    hit = _cache.get("global")
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    async with _lock:   # one computation even when many requests arrive at once
        hit = _cache.get("global")
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        try:
            ranked = await _compute(db)
        except Exception:
            logger.exception("Trending computation failed; serving the last ranking")
            # Keep serving the previous ranking (or nothing) and retry soon.
            stale = hit[1] if hit else []
            _cache["global"] = (time.monotonic() - CACHE_SECONDS + 10, stale)
            return stale
        _cache["global"] = (time.monotonic(), ranked)
        return ranked


async def top(db, *, country: Optional[str] = None, limit: int = 40,
              exclude_oids: Optional[List[ObjectId]] = None,
              exclude_creator: Optional[ObjectId] = None) -> List[dict]:
    """Trending videos, the viewer's country first. Each doc has _id,
    creator_id, created_at, country and `trending_score`."""
    ranked = await _ranking(db)
    excluded = {str(x) for x in (exclude_oids or [])}
    if country:
        c = country.upper()
        ranked = [r for r in ranked if (r[2] or "").upper() == c] + [r for r in ranked if (r[2] or "").upper() != c]
    order = [(v, s) for v, s, _, _ in ranked if v not in excluded]
    out: List[dict] = []
    start = 0
    while len(out) < limit and start < len(order):
        chunk = order[start:start + max(limit * 2, 40)]
        start += len(chunk)
        q = {**_showable(), "_id": {"$in": [ObjectId(v) for v, _ in chunk]}}
        if exclude_creator is not None:
            q["creator_id"] = {"$ne": exclude_creator}
        docs = {str(d["_id"]): d async for d in db.videos.find(q, {"creator_id": 1, "created_at": 1, "country": 1})}
        creators = {d.get("creator_id") for d in docs.values() if isinstance(d.get("creator_id"), ObjectId)}
        ok = {u["_id"] async for u in db.users.find({"_id": {"$in": list(creators)}, "is_active": {"$ne": False}},
                                                     {"_id": 1})}
        for v, s in chunk:
            d = docs.get(v)
            if d and d.get("creator_id") in ok:
                d["trending_score"] = s
                out.append(d)
                if len(out) >= limit:
                    break
    return out
