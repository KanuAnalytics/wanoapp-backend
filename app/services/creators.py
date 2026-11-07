"""
Creator growth: every new post gets first viewers, posting streaks earn reach,
and creators can see how their posts are doing.

First viewers. For You's "fresh" slots (see for_you.py) come from here. A new
post (< 48 h) is in TEST until TEST_AUDIENCE For You viewers have scrolled to
it (watched or skipped); the posts with the fewest such viewers, waiting
longest, go first, so none is left at zero. Only viewers For You chose count
(fy_* fields): watching a post by link, or a ring of accounts doing so, can't
pass its test, and bots that load pages without scrolling don't use it up.
After its test a post stays (BOOST) only if those viewers stayed: average
watch >= GOOD_AVG or finish rate >= GOOD_FINISH, and only until BOOST_CAP
For You viewers. Posts
people skip drop out (Recombee can still pick them for the right viewers).
A creator gets at most PER_CREATOR_TESTS posts in the pool per day, so
flooding doesn't buy reach.

Streaks. Consecutive days (UTC) with at least one public, playable post.
Several posts in a day count once. A streak of STREAK_MIN+ days gives a bigger
test audience, a higher cap and a higher rank among boosted posts. It never
saves a post people skip.

Data. video_reach (one row per post: For You impressions, counted views,
watch portion, finishes) and creator_daily (one row per creator per day:
views, finishes, likes, comments, shares). Views use the same once-per-viewer
rule as trending; creators' own views and engagement never count.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from bson import ObjectId

logger = logging.getLogger(__name__)

WINDOW_HOURS = 48
TEST_AUDIENCE = 30
STREAK_TEST_AUDIENCE = 50
BOOST_CAP = 300
STREAK_BOOST_CAP = 600
IMPRESSION_CEILING = 20     # x test audience: served this often and nobody scrolled to it
GOOD_AVG = 0.4
GOOD_FINISH = 0.25
PER_CREATOR_TESTS = 3
STREAK_MIN = 3
STREAK_MAX_DAYS = 120
STREAK_RANK_DAYS = 8        # ranking only needs to know 3+ (bigger test) and up to 7 (boost)
MILESTONES = (3, 7, 14, 30, 60, 100, 365)
CANDIDATE_LIMIT = 3000
STREAK_CACHE_SECONDS = 120
NEW_ACCOUNT_HOURS = 24      # creators younger than this get a smaller test
NEW_ACCOUNT_TEST = 10
NEW_ACCOUNT_BOOST_CAP = 100

_streak_cache: Dict[tuple, tuple] = {}


def clear_cache():
    _streak_cache.clear()


def _now():
    return datetime.now(timezone.utc)


def _naive(dt):
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _day(dt: datetime) -> datetime:
    dt = _naive(dt)
    return datetime(dt.year, dt.month, dt.day)


def _num(value) -> float:
    try:
        n = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return n if n == n and n > 0 else 0.0     # NaN and negatives -> 0


def _showable() -> dict:
    return {"is_active": True, "privacy": "public", "isReadyToStream": {"$ne": False},
            "status": {"$nin": ["scheduled", "publishing", "cancelled"]}}


def test_audience(streak: int, new_account: bool = False) -> int:
    if new_account:
        return NEW_ACCOUNT_TEST
    return STREAK_TEST_AUDIENCE if streak >= STREAK_MIN else TEST_AUDIENCE


def boost_cap(streak: int, new_account: bool = False) -> int:
    if new_account:
        return NEW_ACCOUNT_BOOST_CAP
    return STREAK_BOOST_CAP if streak >= STREAK_MIN else BOOST_CAP


# ---------------------------------------------------------------- recording

async def record_impressions(db, video_ids: Iterable[str]) -> None:
    from pymongo import UpdateOne
    ops = [UpdateOne({"_id": ObjectId(v)}, {"$inc": {"impressions": 1}}, upsert=True)
           for v in video_ids if ObjectId.is_valid(str(v))]
    if not ops:
        return
    try:
        await db.video_reach.bulk_write(ops, ordered=False)
    except Exception:
        logger.warning("Could not record reach for %d impressions", len(ops))


async def record_view(db, video: dict, *, viewer: Optional[str], portion: float) -> None:
    """A counted view (once per viewer per video, see trending.count_view_once)."""
    creator = video.get("creator_id")
    if viewer and creator is not None and str(viewer) == str(creator):
        return
    finished = int(portion >= 0.9)
    try:
        await db.video_reach.update_one(
            {"_id": video["_id"]},
            {"$inc": {"views": 1, "portion_sum": float(portion), "finished": finished}},
            upsert=True,
        )
        if isinstance(creator, ObjectId):
            await _daily(db, creator, views=1, finished=finished, portion_sum=float(portion))
            # Per post per day, for Wano points (points.py).
            day = _day(_now())
            from app.services import points
            earns = await points.view_earns(db, video, viewer, _now())
            incs = {"views": 1, "finished": finished}
            if earns:
                incs.update(pviews=1, pfinished=finished)
            await db.post_daily.update_one(
                {"_id": f"{video['_id']}:{day.date().isoformat()}"},
                {"$inc": incs,
                 "$setOnInsert": {"video_id": video["_id"], "creator_id": creator, "day": day}},
                upsert=True,
            )
    except Exception:
        logger.warning("Could not record reach for view of %s", video.get("_id"))


async def record_fy_watch(db, video_id: str, portion: float) -> None:
    """The first watch report for a For You impression: this viewer was chosen
    by For You and scrolled to the post. Counts toward its first-viewer test."""
    if not ObjectId.is_valid(str(video_id)):
        return
    from pymongo import ReturnDocument
    try:
        after = await db.video_reach.find_one_and_update(
            {"_id": ObjectId(video_id)},
            {"$inc": {"fy_views": 1, "fy_portion_sum": float(portion), "fy_finished": int(portion >= 0.9)}},
            upsert=True, return_document=ReturnDocument.AFTER,
        )
    except Exception:
        logger.warning("Could not record For You watch of %s", video_id)
        return
    try:
        await _maybe_tell_creator(db, ObjectId(video_id), after or {})
    except Exception:
        logger.exception("First-viewer result for %s not sent", video_id)


async def _maybe_tell_creator(db, video_id: ObjectId, reach: dict) -> None:
    """Once, when the post's first-viewer test is complete, tell the creator how it went."""
    if reach.get("tip_sent") or _num(reach.get("fy_views")) < NEW_ACCOUNT_TEST:
        return
    video = await db.videos.find_one({"_id": video_id}, {"creator_id": 1})
    creator = (video or {}).get("creator_id")
    if not isinstance(creator, ObjectId):
        return
    user = await db.users.find_one({"_id": creator}, {"created_at": 1}) or {}
    created = user.get("created_at")
    young = False
    if isinstance(created, datetime):
        created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
        young = created > _now() - timedelta(hours=NEW_ACCOUNT_HOURS)
    s = ((await streaks(db, [creator])).get(creator) or {}).get("current", 0)
    audience = test_audience(s, young)
    if _num(reach.get("fy_views")) < audience:
        return
    claimed = await db.video_reach.update_one({"_id": video_id, "tip_sent": {"$ne": True}},
                                              {"$set": {"tip_sent": True}})
    if not claimed.modified_count:
        return
    stage, _ = _stage(reach, s, young)
    from app.services import growth_notifications
    await growth_notifications.first_viewers_result(db, video_id, reach, stage == "boost", audience)


async def record_engagement(db, creator, kind: str) -> None:
    """A like/comment/share already deduplicated by trending.record_once."""
    field = {"like": "likes", "comment": "comments", "share": "shares"}.get(kind)
    if not field or not isinstance(creator, ObjectId):
        return
    try:
        await _daily(db, creator, **{field: 1})
    except Exception:
        logger.warning("Could not record %s for creator %s", kind, creator)


async def _daily(db, creator: ObjectId, **incs) -> None:
    day = _day(_now())
    await db.creator_daily.update_one(
        {"_id": f"{creator}:{day.date().isoformat()}"},
        {"$inc": incs, "$setOnInsert": {"creator_id": creator, "day": day}},
        upsert=True,
    )


# ---------------------------------------------------------------- streaks

async def streaks(db, creator_ids: Iterable[ObjectId], now: Optional[datetime] = None,
                  max_days: int = STREAK_MAX_DAYS) -> Dict[ObjectId, dict]:
    """{creator: {"current": days, "posted_today": bool}}. A streak stays alive
    until the end of the day after the last post. One grouped query for all the
    creators (days with a post, per creator), cached briefly."""
    out = {}
    today = _day(now or _now())
    now = time.monotonic()
    todo = []
    for c in {c for c in creator_ids if isinstance(c, ObjectId)}:
        hit = _streak_cache.get((c, max_days))
        if hit and hit[0] == today and now - hit[1] < STREAK_CACHE_SECONDS:
            out[c] = hit[2]
        else:
            todo.append(c)
    if not todo:
        return out
    days = {c: set() for c in todo}
    since = today - timedelta(days=max_days)
    try:
        rows = await db.videos.aggregate([
            {"$match": {**_showable(), "creator_id": {"$in": todo},
                        "created_at": {"$gte": since, "$lt": today + timedelta(days=1)}}},
            {"$group": {"_id": {"c": "$creator_id",
                                "d": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}}}}},
        ]).to_list(length=len(todo) * (max_days + 1))
    except Exception:
        logger.warning("Streak lookup failed for %d creators", len(todo))
        rows = []
    for r in rows:
        key = r.get("_id") or {}
        if key.get("c") in days and isinstance(key.get("d"), str):
            days[key["c"]].add(key["d"])
    for c in todo:
        have = days[c]
        posted_today = today.date().isoformat() in have
        day = today if posted_today else today - timedelta(days=1)
        n = 0
        while n < max_days and day.date().isoformat() in have:
            n += 1
            day -= timedelta(days=1)
        result = {"current": n, "posted_today": posted_today}
        _streak_cache[(c, max_days)] = (today, now, result)
        out[c] = result
    return out


def next_milestone(current: int) -> Optional[int]:
    return next((m for m in MILESTONES if m > current), None)


# ---------------------------------------------------------------- first viewers

def _stage(reach: dict, streak: int, new_account: bool = False):
    imp = _num(reach.get("impressions"))
    seen = _num(reach.get("fy_views"))
    psum = _num(reach.get("fy_portion_sum"))
    fin = _num(reach.get("fy_finished"))
    cap = test_audience(streak, new_account)
    if seen < cap:
        # Served over and over with nobody scrolling to it: let it go.
        return ("test", 0.0) if imp < IMPRESSION_CEILING * cap else (None, 0.0)
    good = psum / seen >= GOOD_AVG or fin / seen >= GOOD_FINISH
    if good and seen < boost_cap(streak, new_account):
        quality = (psum / seen) * 0.5 + (fin / seen) * 0.5
        return "boost", quality * (1 + 0.1 * min(streak, 7))
    return None, 0.0


async def first_viewers(db, excluded_oids: List[ObjectId], own: Optional[ObjectId]) -> List[dict]:
    """Candidates for For You's "fresh" slots, best first. Each doc has
    creator_id, created_at, stage ("test" or "boost")."""
    now = _now()
    q = {**_showable(), "created_at": {"$gte": _naive(now - timedelta(hours=WINDOW_HOURS)),
                                       "$lte": _naive(now + timedelta(minutes=10))}}
    if excluded_oids:
        q["_id"] = {"$nin": excluded_oids}
    if own is not None:
        q["creator_id"] = {"$ne": own}
    from app.services.moderation import not_flagged
    q.update(not_flagged())     # one trusted report pauses first viewers
    docs = await db.videos.find(q, {"creator_id": 1, "created_at": 1}).sort(
        [("created_at", -1), ("_id", -1)]).limit(CANDIDATE_LIMIT).to_list(length=CANDIDATE_LIMIT)

    # At most PER_CREATOR_TESTS posts per creator per day (newest first).
    kept, per = [], {}
    for d in docs:
        created = _naive(d.get("created_at"))
        if not isinstance(created, datetime):
            continue
        bucket = int((_naive(now) - created).total_seconds() // 86400)
        key = (d.get("creator_id"), bucket)
        if per.get(key, 0) >= PER_CREATOR_TESTS:
            continue
        per[key] = per.get(key, 0) + 1
        kept.append(d)
    if not kept:
        return await _paid(db, excluded_oids, own, [])

    reach = {r["_id"]: r async for r in db.video_reach.find({"_id": {"$in": [d["_id"] for d in kept]}})}
    # A week of history is all the ranking uses (For You calls this on every page).
    streak = await streaks(db, [d.get("creator_id") for d in kept], max_days=STREAK_RANK_DAYS)
    young = set()
    cutoff = now - timedelta(hours=NEW_ACCOUNT_HOURS)
    async for u in db.users.find({"_id": {"$in": list({d.get("creator_id") for d in kept})}}, {"created_at": 1}):
        created = u.get("created_at")
        if isinstance(created, datetime):
            created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
            if created > cutoff:
                young.add(u["_id"])

    testing, boosted = [], []
    for d in kept:
        s = (streak.get(d.get("creator_id")) or {}).get("current", 0)
        stage, score = _stage(reach.get(d["_id"], {}), s, d.get("creator_id") in young)
        if stage is None:
            continue
        d = {**d, "stage": stage}
        if stage == "test":
            r = reach.get(d["_id"], {})
            testing.append(((_num(r.get("fy_views")), _num(r.get("impressions"))), _naive(d["created_at"]), d))
        else:
            boosted.append((score, d))
    testing.sort(key=lambda t: (t[0], t[1]))
    boosted.sort(key=lambda t: -t[0])
    return [t[2] for t in testing] + await _paid(db, excluded_oids, own, kept) + [b[1] for b in boosted]


async def _paid(db, excluded_oids, own, kept) -> List[dict]:
    """Posts their creators boosted with Wano points (points.py)."""
    from app.services import points
    try:
        seen = {d["_id"] for d in kept}
        return [d for d in await points.boosted_candidates(db, excluded_oids, own) if d["_id"] not in seen][:20]
    except Exception:
        logger.exception("Could not load points boosts")
        return []


def stage_label(reach: dict, streak: int, created_at) -> str:
    """For creator stats: "testing", "boosted" or "done"."""
    created = _naive(created_at)
    if not isinstance(created, datetime) or created < _naive(_now() - timedelta(hours=WINDOW_HOURS)):
        return "done"
    stage, _ = _stage(reach, streak)
    return {"test": "testing", "boost": "boosted"}.get(stage, "done")
