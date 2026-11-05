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


def encode_cursor(at: datetime, oid: ObjectId, deferred: List[ObjectId]) -> str:
    if at.tzinfo is not None:
        at = at.astimezone(timezone.utc).replace(tzinfo=None)
    micros = int((at - datetime(1970, 1, 1)).total_seconds() * 1_000_000)
    raw = struct.pack(">q", micros) + oid.binary + b"".join(d.binary for d in deferred[:MAX_DEFERRED])
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> Tuple[Optional[datetime], Optional[ObjectId], List[ObjectId]]:
    """(None, None, []) for the first page; 400 for anything malformed."""
    if value == START:
        return None, None, []
    if not value or len(value) > 512:
        raise _bad_cursor()
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        raise _bad_cursor()
    if len(raw) < 20 or (len(raw) - 20) % 12 or (len(raw) - 20) // 12 > MAX_DEFERRED:
        raise _bad_cursor()
    (micros,) = struct.unpack(">q", raw[:8])
    latest = datetime.utcnow() + timedelta(days=1)
    if micros < 0 or micros > int((latest - datetime(1970, 1, 1)).total_seconds() * 1_000_000):
        raise _bad_cursor()
    at = datetime(1970, 1, 1) + timedelta(microseconds=micros)
    oid = ObjectId(raw[8:20])
    deferred = [ObjectId(raw[i:i + 12]) for i in range(20, len(raw), 12)]
    return at, oid, deferred


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


async def cursor_page(db, match: dict, *, limit: int, cursor: str, viewer: Optional[str],
                      exclude_watched: bool, cap_creators: bool, project: dict):
    """One page of the New tab. Returns (projected docs, next cursor or None).

    Every showable post is always in exactly one place: already shown, carried
    in the cursor (held back by the creator cap), or older than the cursor's
    boundary. The boundary only ever moves past posts that were looked at."""
    at, oid, carried_ids = decode_cursor(cursor)
    and_list = list(match.get("$and", []))
    base = {k: v for k, v in match.items() if k != "$and"}
    base["created_at"] = {"$type": "date"}

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
        q = {**base, "_id": {"$in": carried_ids}}
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
        next_cursor = encode_cursor(boundary[0], boundary[1], carry)

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
