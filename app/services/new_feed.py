"""
The New tab: newest public posts first.

Paging. The old app pages with skip/limit, which shifts whenever someone posts
while the viewer scrolls (page 2 repeats the end of page 1). The app can send
`cursor=start` instead and then pass back the X-Next-Cursor header each time;
the cursor marks the oldest post already scanned, so new uploads never move it.

Flooding. With a cursor, no creator gets more than MAX_PER_CREATOR posts on one
page and never two in a row. Their other posts aren't dropped: up to
MAX_DEFERRED of them ride along in the cursor and come back on later pages.
"""

import base64
import struct
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from bson import ObjectId
from fastapi import HTTPException, status

MAX_PER_CREATOR = 2
MAX_DEFERRED = 20
BATCH_FACTOR = 3          # candidates fetched per batch, as a multiple of limit
MAX_SCANNED_FACTOR = 12   # stop scanning after limit * this many candidates

START = "start"
MAX_EXCLUDED = 5            # shared videos already shown at the top, never repeated below
FUTURE_SLACK = timedelta(minutes=10)


def date_ok() -> dict:
    """A real date, and not in the future: a mis-dated post can't sit on top of New
    forever (or keep the "new videos" bubble lit)."""
    return {"$type": "date", "$lte": datetime.utcnow() + FUTURE_SLACK}


def ready_filter() -> dict:
    """Posts that can actually play. Older posts predate isReadyToStream; they
    count only when they have a playable link or images."""
    playable = {"$type": "string", "$ne": ""}
    return {"$or": [
        {"isReadyToStream": True},
        {"isReadyToStream": {"$exists": False}, "$or": [
            {"remoteUrl_CF": playable},
            {"remoteUrl": playable},
            {"media_type": "photo"},
            {"images.0": {"$exists": True}},
        ]},
    ]}


# ---------------------------------------------------------------- cursor

def _bad_cursor():
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid cursor")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def encode_cursor(at: datetime, oid: ObjectId, deferred: List[ObjectId],
                  excluded: Optional[List[ObjectId]] = None) -> str:
    if at.tzinfo is not None:
        at = at.astimezone(timezone.utc).replace(tzinfo=None)
    micros = int((at - datetime(1970, 1, 1)).total_seconds() * 1_000_000)
    raw = struct.pack(">q", micros) + oid.binary + b"".join(d.binary for d in deferred[:MAX_DEFERRED])
    out = _b64(raw)
    if excluded:
        out += "." + _b64(b"".join(e.binary for e in excluded[:MAX_EXCLUDED]))
    return out


def _unb64(value: str) -> bytes:
    if not value or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for ch in value):
        raise _bad_cursor()
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        raise _bad_cursor()


def decode_cursor(value: str):
    """(at, oid, deferred, excluded); (None, None, [], []) for the first page;
    400 for anything malformed."""
    if value == START:
        return None, None, [], []
    if not value or len(value) > 512 or value.count(".") > 1:
        raise _bad_cursor()
    main, _, ex = value.partition(".")
    raw = _unb64(main)
    excluded = []
    if ex or "." in value:
        exraw = _unb64(ex)
        if not exraw or len(exraw) % 12 or len(exraw) // 12 > MAX_EXCLUDED:
            raise _bad_cursor()
        excluded = [ObjectId(exraw[i:i + 12]) for i in range(0, len(exraw), 12)]
    if len(raw) < 20 or (len(raw) - 20) % 12 or (len(raw) - 20) // 12 > MAX_DEFERRED:
        raise _bad_cursor()
    (micros,) = struct.unpack(">q", raw[:8])
    latest = datetime.utcnow() + timedelta(days=1)
    if micros < 0 or micros > int((latest - datetime(1970, 1, 1)).total_seconds() * 1_000_000):
        raise _bad_cursor()
    at = datetime(1970, 1, 1) + timedelta(microseconds=micros)
    oid = ObjectId(raw[8:20])
    deferred = [ObjectId(raw[i:i + 12]) for i in range(20, len(raw), 12)]
    return at, oid, deferred, excluded


# ---------------------------------------------------------------- page

def _naive(dt):
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _older_than(at: datetime, oid: ObjectId) -> dict:
    return {"$or": [{"created_at": {"$lt": at}}, {"created_at": at, "_id": {"$lt": oid}}]}


async def _hidden_for(db, viewer: Optional[str], ids: List[ObjectId], exclude_watched: bool) -> set:
    """Ids among `ids` this viewer said they're not interested in, plus the ones
    they've watched when exclude_watched is on. Bounded to `ids`."""
    if not viewer or not ids:
        return set()
    q = {"user_id": ObjectId(viewer), "video_id": {"$in": ids}}
    if not exclude_watched:
        q["not_interested"] = True
    return {w["video_id"] async for w in db.watch_history.find(q, {"video_id": 1})}


async def _inactive_creators(db, creator_ids) -> set:
    ids = [c for c in set(creator_ids) if isinstance(c, ObjectId)]
    if not ids:
        return set()
    return {u["_id"] async for u in db.users.find({"_id": {"$in": ids}, "is_active": False}, {"_id": 1})}


# Deactivated accounts, cached briefly: they change rarely and skip paging
# needs them on every request.
_INACTIVE_TTL = timedelta(seconds=60)
_inactive_cache: Tuple[Optional[datetime], List[ObjectId]] = (None, [])


async def all_inactive_creators(db) -> List[ObjectId]:
    global _inactive_cache
    at, ids = _inactive_cache
    now = datetime.now(timezone.utc)
    if at is None or now - at > _INACTIVE_TTL:
        ids = [u["_id"] async for u in db.users.find({"is_active": False}, {"_id": 1})]
        _inactive_cache = (now, ids)
    return ids


async def skip_exclusions(db, match: dict, viewer: Optional[str]) -> dict:
    """match with deactivated creators and the viewer's "not interested" posts
    left out in the query itself. Skip paging can't backfill a page, and the
    store builds take a page shorter than `limit` as the end of the feed, so
    hiding these after $limit cut the New tab short."""
    match = dict(match)
    inactive = await all_inactive_creators(db)
    if inactive:
        current = match.get("creator_id")
        if isinstance(current, dict) and "$nin" in current:
            match["creator_id"] = {"$nin": list(set(current["$nin"]) | set(inactive))}
        elif current is None:
            match["creator_id"] = {"$nin": inactive}
    if viewer and ObjectId.is_valid(viewer):
        not_interested = [
            w["video_id"] async for w in db.watch_history.find(
                {"user_id": ObjectId(viewer), "not_interested": True}, {"video_id": 1}
            ).limit(5000)
        ]
        if not_interested:
            match["_id"] = {"$nin": not_interested}
    return match


async def cursor_page(db, match: dict, *, limit: int, cursor: str, viewer: Optional[str],
                      exclude_watched: bool, cap_creators: bool, project: dict,
                      exclude: Optional[List[ObjectId]] = None):
    """One page of the New tab. Returns (projected docs, next cursor or None).

    Every showable post is always in exactly one place: already shown, carried
    in the cursor (held back by the creator cap), or older than the cursor's
    boundary. The boundary only ever moves past posts that were looked at."""
    at, oid, carried_ids, excluded = decode_cursor(cursor)
    for e in exclude or []:
        if e not in excluded:
            excluded.append(e)
    excluded = excluded[:MAX_EXCLUDED]
    and_list = list(match.get("$and", []))
    base = {k: v for k, v in match.items() if k != "$and"}
    base["created_at"] = date_ok()
    if excluded:
        base["_id"] = {"$nin": excluded}

    chosen, deferred, per_creator = [], [], {}
    state = {"last": None}

    def take(doc) -> bool:
        creator = doc.get("creator_id")
        if cap_creators and (per_creator.get(creator, 0) >= MAX_PER_CREATOR or creator == state["last"]):
            return False
        chosen.append(doc)
        per_creator[creator] = per_creator.get(creator, 0) + 1
        state["last"] = creator
        return True

    def offer(doc):
        if len(chosen) >= limit or not take(doc):
            deferred.append(doc)
        # A post held back for being back-to-back may fit now.
        for d in list(deferred):
            if len(chosen) >= limit:
                break
            if d is not doc and take(d):
                deferred.remove(d)

    # 1. Posts carried over from earlier pages, if they can still be shown.
    if carried_ids:
        q = {**base, "_id": {"$in": [i for i in carried_ids if i not in excluded]}}
        if and_list:
            q["$and"] = and_list
        found = {d["_id"]: d for d in await db.videos.find(q).to_list(length=MAX_DEFERRED)}
        hidden = await _hidden_for(db, viewer, list(found), exclude_watched)
        gone = await _inactive_creators(db, [d.get("creator_id") for d in found.values()])
        for i in carried_ids:
            d = found.get(i)
            if d is not None and i not in hidden and d.get("creator_id") not in gone:
                offer(d)

    # 2. Older posts, in batches, from the boundary down.
    boundary = (at, oid)
    done = False
    scanned, batch = 0, max(limit * BATCH_FACTOR, 20)
    while len(chosen) < limit and not done and scanned < limit * MAX_SCANNED_FACTOR:
        q = dict(base)
        conds = and_list + ([_older_than(*boundary)] if boundary[0] is not None else [])
        if conds:
            q["$and"] = conds
        fresh = await db.videos.find(q).sort([("created_at", -1), ("_id", -1)]).limit(batch).to_list(length=batch)
        scanned += len(fresh)
        hidden = await _hidden_for(db, viewer, [d["_id"] for d in fresh], exclude_watched)
        gone = await _inactive_creators(db, [d.get("creator_id") for d in fresh])
        stopped = False
        for d in fresh:
            if len(chosen) >= limit:
                stopped = True
                break
            boundary = (_naive(d["created_at"]), d["_id"])
            if d["_id"] in hidden or d.get("creator_id") in gone:
                continue
            offer(d)
        if not stopped and len(fresh) < batch:
            done = True

    # Held-back posts ride in the cursor. Past MAX_DEFERRED it's a flood: the
    # oldest extras are left to the creator's profile and For You.
    carry = [d["_id"] for d in deferred][:MAX_DEFERRED]
    next_cursor = None
    if (carry or not done) and boundary[0] is not None:
        next_cursor = encode_cursor(boundary[0], boundary[1], carry, excluded)

    if not chosen:
        return [], next_cursor
    # Same shape as the skip feed: project with the creator joined.
    order = {d["_id"]: i for i, d in enumerate(chosen)}
    pipeline = [
        {"$match": {"_id": {"$in": list(order)}}},
        {"$lookup": {"from": "users", "localField": "creator_id", "foreignField": "_id", "as": "creator"}},
        {"$unwind": {"path": "$creator", "preserveNullAndEmptyArrays": True}},
        {"$project": project},
    ]
    docs = await db.videos.aggregate(pipeline).to_list(length=len(order))
    docs.sort(key=lambda d: order.get(ObjectId(d["_id"]), 0))
    return docs, next_cursor


# ---------------------------------------------------------------- "N new videos" bubble

NEW_COUNT_CAP = 99
NEW_COUNT_SCAN = 300        # newest candidates looked at per check
BUBBLE_CREATORS = 3
POLL_AFTER_SECONDS = 60


def _iso(dt) -> Optional[str]:
    dt = _naive(dt)
    return dt.isoformat() + "Z" if isinstance(dt, datetime) else None


async def _since_point(db, match: dict, since: ObjectId) -> Tuple[datetime, ObjectId]:
    """Where "new" starts: the post's date when the viewer can see that post,
    otherwise the time inside its id. A private or deleted post's own date is
    never used, so the endpoint can't be used to probe it."""
    q = {**match, "_id": since}
    doc = await db.videos.find_one(q, {"created_at": 1})
    at = _naive(doc.get("created_at")) if doc else None
    if not isinstance(at, datetime):
        at = _naive(since.generation_time)
    return at, since


def _newer_than(at: datetime, oid: ObjectId) -> dict:
    return {"$or": [{"created_at": {"$gt": at}}, {"created_at": at, "_id": {"$gt": oid}}]}


async def new_count(db, match: dict, since: ObjectId, viewer: Optional[str]) -> dict:
    """How many posts the New tab would show above `since`, plus up to three of
    their creators, for the "▲ 5 new videos" bubble. Read-only and bounded."""
    base = {k: v for k, v in match.items() if k != "$and"}
    and_list = list(match.get("$and", []))
    at, oid = await _since_point(db, {**base, **({"$and": and_list} if and_list else {})}, since)

    q = dict(base)
    q["created_at"] = date_ok()
    q["$and"] = and_list + [_newer_than(at, oid)]
    fresh = await db.videos.find(q, {"creator_id": 1, "created_at": 1}).sort(
        [("created_at", -1), ("_id", -1)]).limit(NEW_COUNT_SCAN).to_list(length=NEW_COUNT_SCAN)
    hidden = await _hidden_for(db, viewer, [d["_id"] for d in fresh], False)
    gone = await _inactive_creators(db, [d.get("creator_id") for d in fresh])
    visible = [d for d in fresh if d["_id"] not in hidden and d.get("creator_id") not in gone]

    creator_ids = []
    for d in visible:
        c = d.get("creator_id")
        if isinstance(c, ObjectId) and c not in creator_ids:
            creator_ids.append(c)
        if len(creator_ids) == BUBBLE_CREATORS:
            break
    users = {}
    if creator_ids:
        async for u in db.users.find({"_id": {"$in": creator_ids}},
                                     {"username": 1, "display_name": 1, "profile_picture": 1}):
            users[u["_id"]] = u
    creators = [{"id": str(c),
                 "username": users[c].get("username") if isinstance(users[c].get("username"), str) else None,
                 "display_name": users[c].get("display_name") if isinstance(users[c].get("display_name"), str) else None,
                 "profile_picture": users[c].get("profile_picture") if isinstance(users[c].get("profile_picture"), str) else None}
                for c in creator_ids if c in users]

    last = visible[0] if visible else None
    if last is None:
        lq = dict(base)
        lq["created_at"] = date_ok()
        if and_list:
            lq["$and"] = and_list
        for d in await db.videos.find(lq, {"creator_id": 1, "created_at": 1}).sort(
                [("created_at", -1), ("_id", -1)]).limit(20).to_list(length=20):
            if d.get("creator_id") not in await _inactive_creators(db, [d.get("creator_id")]):
                last = d
                break

    return {
        "count": min(len(visible), NEW_COUNT_CAP),
        "more": len(visible) > NEW_COUNT_CAP or len(fresh) == NEW_COUNT_SCAN,
        "creators": creators,
        "newest_at": _iso(visible[0]["created_at"]) if visible else None,
        "last_post_at": _iso(last["created_at"]) if last else None,
        "poll_after_seconds": POLL_AFTER_SECONDS,
    }


# ---------------------------------------------------------------- your own posts, pinned

OWN_PIN_HOURS = 24
OWN_PIN_MAX = 3


async def own_pinned(db, viewer: str, project: dict) -> List[dict]:
    """The viewer's own public posts from the last day, newest first, with a
    live/processing status, so a creator can see their post landed."""
    since = datetime.utcnow() - timedelta(hours=OWN_PIN_HOURS)
    q = {"creator_id": ObjectId(viewer), "is_active": True,
         "$or": [{"privacy": "public"}, {"moderation.state": "held"}],
         "created_at": {"$gte": since}, "status": {"$ne": "scheduled"}}
    raw = await db.videos.find(q).sort([("created_at", -1), ("_id", -1)]).limit(OWN_PIN_MAX).to_list(length=OWN_PIN_MAX)
    if not raw:
        return []
    from app.services.recommendations import is_ready_to_stream
    status_of = {d["_id"]: ("under_review" if (d.get("moderation") or {}).get("state") == "held"
                            else "live" if is_ready_to_stream(d) else "processing") for d in raw}
    order = {d["_id"]: i for i, d in enumerate(raw)}
    pipeline = [
        {"$match": {"_id": {"$in": list(order)}}},
        {"$lookup": {"from": "users", "localField": "creator_id", "foreignField": "_id", "as": "creator"}},
        {"$unwind": {"path": "$creator", "preserveNullAndEmptyArrays": True}},
        {"$project": project},
    ]
    docs = await db.videos.aggregate(pipeline).to_list(length=len(order))
    docs.sort(key=lambda d: order.get(ObjectId(d["_id"]), 0))
    for d in docs:
        d["_pin_status"] = status_of.get(ObjectId(d["_id"]))
    return docs
