"""Fair challenge scoring ("challenge_fair" switch, Nov 2026).

A challenge created while the switch is on gets rules=2 for its whole life;
challenges made before keep the old score, so a live one never reshuffles.

Why: the old score (views + 2 finished + 3 likes + 5 comments + 8 shares on
the post) could be bought in an hour. Signed-out views come from IDs the
phone makes up, a viewer counted again every 6 hours, the creator's own
comments and likes counted, and one post with many hashtags entered every
live challenge.

Rules 2:
  - Entries: a public post made inside the window, stamped with ONE
    challenge when it was posted (the first live challenge hashtag in it, or
    the challenge it joined from the app). Posts with more than MAX_TAGS
    hashtags don't enter. Staff accounts never rank.
  - Points, from "established" accounts only (signed up at least
    ESTABLISHED_DAYS before, never the creator):
      1 per signed-in viewer (once ever), +2 if they watched to the end,
      3 per like, 5 per person who commented (one each, held or hidden
      comments don't count), 8 per person who shared;
      signed-out views count 0.1 each, at most ANON_CAP points per entry.
  - Only what happened before the end counts: the board is frozen and
    stored the first time it's read (or the worker runs) after the end.
  - The live board is cached for CACHE_SECONDS; your own entry is always
    shown to you.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from bson import ObjectId

SWITCH = "challenge_fair"
RULES = 2
MAX_TAGS = 10
ESTABLISHED_DAYS = 3
ANON_WEIGHT = 0.1
ANON_CAP = 30
FINISHED = 0.9
CACHE_SECONDS = 60
SCAN = 2000

_cache: Dict[str, tuple] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(dt):
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


async def on(db) -> bool:
    from app.services import creator_push
    return await creator_push.on(db, SWITCH)


def fair(ch: dict) -> bool:
    return (ch or {}).get("rules") == RULES


# ------------------------------------------------------------------ entering

async def stamp(db, video_id: ObjectId, hashtags: List[str], *, privacy: str,
                joined: Optional[str] = None) -> Optional[ObjectId]:
    """Give a new post its one challenge (rules 2 only). Returns the id or None."""
    if privacy != "public" or not isinstance(hashtags, list) or len(hashtags) > MAX_TAGS:
        return None
    now = _now()
    live = await db.challenges.find({"rules": RULES, "starts_at": {"$lte": now}, "ends_at": {"$gt": now}},
                                    {"hashtag": 1}).to_list(50)
    if not live:
        return None
    chosen = None
    if joined and ObjectId.is_valid(str(joined)):
        chosen = next((c for c in live if str(c["_id"]) == str(joined)), None)
    if chosen is None:
        by_tag = {str(c["hashtag"]).lower(): c for c in live}
        for t in hashtags:
            c = by_tag.get(str(t).lstrip("#").lower())
            if c is not None:
                chosen = c
                break
    if chosen is None:
        return None
    await db.videos.update_one({"_id": video_id}, {"$set": {"challenge_id": chosen["_id"]}})
    return chosen["_id"]


# ------------------------------------------------------------------ signals

async def note_view(db, video: dict, *, viewer: str, signed_in: bool, portion: float) -> None:
    """A counted view of an entry: once per viewer ever, keeping the best portion."""
    cid = (video or {}).get("challenge_id")
    if not isinstance(cid, ObjectId) or not viewer:
        return
    if signed_in and str(viewer) == str(video.get("creator_id")):
        return
    p = float(portion) if isinstance(portion, (int, float)) else 0.0
    await db.challenge_signals.update_one(
        {"_id": f"v:{viewer}:{video['_id']}"},
        {"$setOnInsert": {"kind": "view", "video_id": video["_id"], "challenge_id": cid, "actor": str(viewer),
                          "signed_in": bool(signed_in), "at": _now()},
         "$max": {"portion": max(0.0, min(1.0, p))}},
        upsert=True)


async def note_share(db, video: dict, actor: str) -> None:
    cid = (video or {}).get("challenge_id")
    if not isinstance(cid, ObjectId) or not actor or str(actor) == str(video.get("creator_id")):
        return
    await db.challenge_signals.update_one(
        {"_id": f"s:{actor}:{video['_id']}"},
        {"$setOnInsert": {"kind": "share", "video_id": video["_id"], "challenge_id": cid, "actor": str(actor),
                          "signed_in": True, "at": _now()}},
        upsert=True)


# ------------------------------------------------------------------ ranking

async def _staff(db) -> set:
    cfg = await db.config.find_one({"challenge_staff": {"$exists": True}}, {"challenge_staff": 1}) or {}
    names = [str(n).lower() for n in (cfg.get("challenge_staff") or []) if isinstance(n, str)]
    ids = set()
    async for u in db.users.find({"$or": [{"user_type": "admin"}, {"username_lower": {"$in": names}},
                                          {"username": {"$in": names}}]}, {"_id": 1}):
        ids.add(u["_id"])
    return ids


async def ranking(db, ch: dict) -> List[dict]:
    from app.services.creators import _showable
    from app.services import thumbnails
    end = _naive(ch["ends_at"])
    cutoff = min(_now(), end) - timedelta(days=ESTABLISHED_DAYS)
    q = {**_showable(), "moderation.state": {"$nin": ["held", "removed"]}, "challenge_id": ch["_id"],
         "created_at": {"$gte": ch["starts_at"], "$lt": end}}
    docs = await db.videos.find(q, {"creator_id": 1, "created_at": 1, "urls": 1, "remoteUrl_CF": 1,
                                    "media_type": 1, "images": 1, "hashtags": 1}).limit(SCAN).to_list(SCAN)
    docs = [d for d in docs if len(d.get("hashtags") or []) <= MAX_TAGS]
    if not docs:
        return []
    staff = await _staff(db)
    users = {u["_id"]: u async for u in db.users.find(
        {"_id": {"$in": list({d.get("creator_id") for d in docs})}, "is_active": True},
        {"username": 1, "display_name": 1, "profile_picture": 1})}
    ids = [d["_id"] for d in docs]
    creator_of = {d["_id"]: d.get("creator_id") for d in docs}

    # Who is established (signed up before the cutoff, still active).
    actors = set()
    sigs = await db.challenge_signals.find({"video_id": {"$in": ids}, "at": {"$lt": end}}).to_list(None)
    for s in sigs:
        if s.get("signed_in") and ObjectId.is_valid(s.get("actor", "")):
            actors.add(ObjectId(s["actor"]))
    comments = await db.comments.find({"video_id": {"$in": ids}, "is_active": True, "created_at": {"$lt": end},
                                       "held": {"$exists": False}, "hidden": {"$ne": True}},
                                      {"video_id": 1, "user_id": 1}).to_list(None)
    actors.update(c["user_id"] for c in comments if isinstance(c.get("user_id"), ObjectId))
    likers = await db.users.find({"liked_videos": {"$in": ids}, "is_active": True, "created_at": {"$lte": cutoff}},
                                 {"liked_videos": 1}).to_list(None)
    est = {u["_id"] async for u in db.users.find(
        {"_id": {"$in": list(actors)}, "is_active": True, "created_at": {"$lte": cutoff}}, {"_id": 1})}

    score = {i: 0.0 for i in ids}
    anon = {i: 0.0 for i in ids}
    for s in sigs:
        vid = s["video_id"]
        if vid not in score:
            continue
        if not s.get("signed_in"):
            if s.get("kind") == "view":
                anon[vid] += ANON_WEIGHT
            continue
        a = ObjectId(s["actor"]) if ObjectId.is_valid(s.get("actor", "")) else None
        if a is None or a not in est or a == creator_of.get(vid):
            continue
        if s.get("kind") == "view":
            score[vid] += 1 + (2 if (s.get("portion") or 0) >= FINISHED else 0)
        elif s.get("kind") == "share":
            score[vid] += 8
    for u in likers:
        for vid in set(u.get("liked_videos") or []) & set(ids):
            if u["_id"] != creator_of.get(vid):
                score[vid] += 3
    seen = set()
    for c in comments:
        key = (c["video_id"], c.get("user_id"))
        if key in seen or c.get("user_id") not in est or c.get("user_id") == creator_of.get(c["video_id"]):
            continue
        seen.add(key)
        score[c["video_id"]] += 5
    for vid in ids:
        score[vid] += min(anon[vid], ANON_CAP)

    best = {}
    for d in docs:
        cid = d.get("creator_id")
        if cid not in users or cid in staff:
            continue
        s = score[d["_id"]]
        key = (-s, _naive(d.get("created_at")) or datetime.max, str(d["_id"]))
        if cid not in best or key < best[cid][0]:
            best[cid] = (key, d, s)
    out = []
    for i, (_, d, s) in enumerate(sorted(best.values(), key=lambda t: t[0]), 1):
        u = users[d["creator_id"]]
        out.append({"rank": i, "video_id": str(d["_id"]), "score": int(round(s)),
                    "thumbnail": thumbnails.best_thumbnail(d),
                    "user": {"id": str(u["_id"]), "username": u.get("username"),
                             "display_name": u.get("display_name"), "profile_picture": u.get("profile_picture")}})
    return out


async def board(db, ch: dict) -> List[dict]:
    """Frozen after the end (stored once), cached while live."""
    if _naive(ch["ends_at"]) <= _now():
        if isinstance(ch.get("final"), list):
            return ch["final"]
        rows = await ranking(db, ch)
        await db.challenges.update_one({"_id": ch["_id"], "final": {"$exists": False}},
                                       {"$set": {"final": rows, "frozen_at": _now()}})
        fresh = await db.challenges.find_one({"_id": ch["_id"]}, {"final": 1})
        return (fresh or {}).get("final", rows)
    key = str(ch["_id"])
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    rows = await ranking(db, ch)
    _cache[key] = (time.monotonic(), rows)
    return rows


async def mine(db, ch: dict, rows: List[dict], user_id: Optional[str]) -> Optional[dict]:
    """The viewer's own entry, even when the cached board is older than it."""
    if not user_id or not ObjectId.is_valid(str(user_id)):
        return None
    row = next((r for r in rows if r["user"]["id"] == str(user_id)), None)
    if row:
        return row
    entry = await db.videos.find_one({"challenge_id": ch["_id"], "creator_id": ObjectId(user_id),
                                      "is_active": True}, {"_id": 1}, sort=[("created_at", -1)])
    return {"rank": None, "video_id": str(entry["_id"]), "score": None, "counting": True} if entry else None
