"""Livelier feeds (behind the "feed_fx" switch).

Local reactions
  One-tap reactions on a post: "+1 <your city>", "Eish", "Ayaa", "Fire". Each
  account adds each kind once per post (tapping again takes it back). A post
  keeps a count per kind and per Home Ground city, so the app can say
  "38 from Kampala reacted". The city comes from the reacting account's Home
  Ground pick; under-18s count towards the post but never towards a city.
  Reactions don't feed trending, so they can't be used to buy a rank.

Trending board
  The trending ranking (app/services/trending.py) as a chart: strict per
  country ("#3 in Uganda") or all of Wano, with how many places each post
  moved since about a day ago and roughly how many views it had in the last
  hour. Ranks are saved once an hour per country so movement can be worked
  out; a post with no rank a day ago is "new".
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.services import home_ground as hg, trending

KINDS = ("plus_one", "eish", "ayaa", "fire")
REACTIONS_PER_DAY = 300
BOARD_MAX = 100
SNAPSHOT_TOP = 200


def _now() -> datetime:
    return datetime.utcnow()


def _oid(video_id: str) -> ObjectId:
    if not isinstance(video_id, str) or not ObjectId.is_valid(video_id):
        raise HTTPException(status_code=404, detail="Post not found")
    return ObjectId(video_id)


async def _visible_post(db, vid: ObjectId, me: Optional[ObjectId]) -> dict:
    v = await db.videos.find_one({"_id": vid, "is_active": True},
                                 {"creator_id": 1, "privacy": 1, "reactions": 1, "city_reactions": 1, "comments_count": 1})
    if not v:
        raise HTTPException(status_code=404, detail="Post not found")
    own = me is not None and v.get("creator_id") == me
    if v.get("privacy") not in ("public", "unlisted") and not own:
        raise HTTPException(status_code=404, detail="Post not found")
    return v


def _blocked(user: dict, other) -> bool:
    for k in ("blocked_users", "blocked_by"):
        lst = user.get(k)
        if isinstance(lst, list) and other in lst:
            return True
    return False


def _counts(v: dict) -> Dict[str, int]:
    raw = v.get("reactions") if isinstance(v.get("reactions"), dict) else {}
    return {k: max(0, raw.get(k)) if isinstance(raw.get(k), int) else 0 for k in KINDS}


def _city_count(v: dict, city: Optional[str]) -> int:
    raw = v.get("city_reactions") if isinstance(v.get("city_reactions"), dict) else {}
    n = raw.get(city) if city else None
    return max(0, n) if isinstance(n, int) else 0


async def summary(db, video_id: str, me: Optional[str]) -> dict:
    vid = _oid(video_id)
    my_oid = ObjectId(me) if me and ObjectId.is_valid(me) else None
    v = await _visible_post(db, vid, my_oid)
    out = {"counts": _counts(v), "mine": [], "city": None}
    if my_oid:
        user = await db.users.find_one({"_id": my_oid}, {"home_ground": 1, "date_of_birth": 1, "localization": 1}) or {}
        out["mine"] = [d["kind"] async for d in db.video_reactions.find({"video_id": vid, "user_id": my_oid},
                                                                         {"kind": 1})
                       if d.get("kind") in KINDS]
        city = hg.city_of(user)
        if city and not hg.is_minor(user):
            out["city"] = {"id": city, "name": hg.CITY[city]["name"], "count": _city_count(v, city)}
    return out


async def react(db, video_id: str, me: str, kind: str, on: bool) -> dict:
    if kind not in KINDS:
        raise HTTPException(status_code=422, detail="Unknown reaction")
    vid = _oid(video_id)
    my_oid = ObjectId(me)
    v = await _visible_post(db, vid, my_oid)
    user = await db.users.find_one({"_id": my_oid}, {"home_ground": 1, "date_of_birth": 1, "localization": 1,
                                                     "blocked_users": 1, "blocked_by": 1}) or {}
    if _blocked(user, v.get("creator_id")):
        raise HTTPException(status_code=404, detail="Post not found")
    city = hg.city_of(user)
    city = city if city and not hg.is_minor(user) else None
    if kind == "plus_one" and not city:
        raise HTTPException(status_code=409, detail="Pick your Home Ground city first")
    key = f"{vid}:{my_oid}:{kind}"
    if on:
        day = _now().strftime("%Y%m%d")
        cap = await db.reaction_daily.find_one_and_update(
            {"_id": f"{my_oid}:{day}"}, {"$inc": {"n": 1}, "$setOnInsert": {"at": _now()}},
            upsert=True, return_document=ReturnDocument.AFTER)
        if (cap or {}).get("n", 0) > REACTIONS_PER_DAY:
            await db.reaction_daily.update_one({"_id": f"{my_oid}:{day}"}, {"$inc": {"n": -1}})
            raise HTTPException(status_code=429, detail="That's a lot of reactions today. Try again tomorrow.")
        try:
            await db.video_reactions.insert_one({"_id": key, "video_id": vid, "user_id": my_oid, "kind": kind,
                                                 "city": city, "at": _now()})
        except DuplicateKeyError:
            await db.reaction_daily.update_one({"_id": f"{my_oid}:{day}"}, {"$inc": {"n": -1}})
            return await summary(db, video_id, me)       # already there: nothing changes
        inc = {f"reactions.{kind}": 1}
        if city:
            inc[f"city_reactions.{city}"] = 1
        await db.videos.update_one({"_id": vid}, {"$inc": inc})
    else:
        doc = await db.video_reactions.find_one_and_delete({"_id": key})
        if doc:
            # Only take back what is there, so counts never go negative.
            await db.videos.update_one({"_id": vid, f"reactions.{kind}": {"$gt": 0}},
                                       {"$inc": {f"reactions.{kind}": -1}})
            c = doc.get("city")
            if isinstance(c, str) and c:
                await db.videos.update_one({"_id": vid, f"city_reactions.{c}": {"$gt": 0}},
                                           {"$inc": {f"city_reactions.{c}": -1}})
    return await summary(db, video_id, me)


# ------------------------------------------------------------------ top comment

async def top_comment(db, video_id: str, me: Optional[str]) -> Optional[dict]:
    """The comment the feed shows over a post for a moment: the pinned one, or
    the most liked of the recent ones. None when there is nothing worth showing
    or comments are off. Never from an account that is banned or blocked."""
    vid = _oid(video_id)
    my_oid = ObjectId(me) if me and ObjectId.is_valid(me) else None
    v = await _visible_post(db, vid, my_oid)
    full = await db.videos.find_one({"_id": vid}, {"comments_enabled": 1})
    if (full or {}).get("comments_enabled") is False:
        return None
    blocked = set()
    if my_oid:
        me_doc = await db.users.find_one({"_id": my_oid}, {"blocked_users": 1, "blocked_by": 1}) or {}
        for k in ("blocked_users", "blocked_by"):
            if isinstance(me_doc.get(k), list):
                blocked.update(me_doc[k])
    docs = await db.comments.find({"video_id": vid, "parent_id": None, "is_active": True},
                                  {"content": 1, "user_id": 1, "likes_count": 1, "is_pinned": 1}
                                  ).sort("created_at", -1).limit(50).to_list(50)
    users = {u["_id"]: u async for u in db.users.find(
        {"_id": {"$in": list({d.get("user_id") for d in docs})}, "is_active": {"$ne": False}},
        {"display_name": 1, "username": 1, "profile_picture": 1})}
    ok = [d for d in docs if d.get("user_id") in users and d.get("user_id") not in blocked
          and isinstance(d.get("content"), str) and d["content"].strip()]
    if not ok:
        return None
    likes = lambda d: d.get("likes_count") if isinstance(d.get("likes_count"), int) else 0  # noqa: E731
    best = sorted(ok, key=lambda d: (d.get("is_pinned") is True, likes(d)), reverse=True)[0]
    u = users[best["user_id"]]
    name = u.get("display_name") if isinstance(u.get("display_name"), str) else None
    return {"id": str(best["_id"]), "text": best["content"].strip()[:140],
            "likes": likes(best), "pinned": best.get("is_pinned") is True,
            "user": {"name": name or (u.get("username") if isinstance(u.get("username"), str) else "Someone"),
                     "picture": u.get("profile_picture") if isinstance(u.get("profile_picture"), str) else None},
            "total": v.get("comments_count") if isinstance(v.get("comments_count"), int) else len(ok)}


# ------------------------------------------------------------------ trending board

def _scope(country: Optional[str]) -> str:
    return (country or "all").lower()


async def _ranked_ids(db, country: Optional[str]) -> List[str]:
    ranked = await trending._ranking(db)
    if country:
        c = country.upper()
        ranked = [r for r in ranked if (r[2] or "").upper() == c]
    return [r[0] for r in ranked]


async def _snapshot(db, scope: str, ids: List[str]) -> None:
    hour = _now().strftime("%Y%m%d%H")
    try:
        await db.trending_snapshots.update_one(
            {"_id": f"{scope}:{hour}"},
            {"$setOnInsert": {"scope": scope, "at": _now(),
                              "ranks": {v: i + 1 for i, v in enumerate(ids[:SNAPSHOT_TOP])}}},
            upsert=True)
    except Exception:
        pass   # movement is a nice-to-have; never fail the board over it


async def _day_ago(db, scope: str) -> Optional[Dict[str, int]]:
    now = _now()
    snap = await db.trending_snapshots.find_one(
        {"scope": scope, "at": {"$gte": now - timedelta(hours=30), "$lte": now - timedelta(hours=20)}},
        sort=[("at", 1)])
    ranks = (snap or {}).get("ranks")
    return ranks if isinstance(ranks, dict) else None


async def _views_last_hour(db, ids: List[str]) -> Dict[str, int]:
    now = datetime.now(timezone.utc)
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    prev_hour = this_hour - timedelta(hours=1)
    frac_left = 1 - (now - this_hour).total_seconds() / 3600    # share of the last hour that fell in prev_hour
    out: Dict[str, float] = {}
    oids = [ObjectId(v) for v in ids if ObjectId.is_valid(v)]
    async for row in db.video_stats_hourly.find(
            {"video_id": {"$in": oids}, "hour": {"$gte": prev_hour.replace(tzinfo=None)}},
            {"video_id": 1, "hour": 1, "views": 1, "anon_views": 1}):
        h = row.get("hour")
        if not isinstance(h, datetime):
            continue
        n = trending._n(row.get("views")) + trending._n(row.get("anon_views"))
        w = 1.0 if h.replace(tzinfo=None) >= this_hour.replace(tzinfo=None) else frac_left
        k = str(row["video_id"])
        out[k] = out.get(k, 0.0) + n * w
    return {k: int(round(v)) for k, v in out.items()}


async def board(db, *, country: Optional[str], skip: int, limit: int) -> List[dict]:
    """Ranked items: {id, rank, change ('new' or places moved, + is up), views_last_hour}."""
    if country is not None and not (len(country) == 2 and country.isalpha()):
        raise HTTPException(status_code=422, detail="Country is a 2-letter code")
    scope = _scope(country)
    ids = await _ranked_ids(db, country)
    await _snapshot(db, scope, ids)
    prev = await _day_ago(db, scope)
    # Only posts that can be shown right now, ranked among themselves.
    window = ids[: min(len(ids), BOARD_MAX)]
    shown = {str(d["_id"]): d async for d in db.videos.find(
        {**trending._showable(), "_id": {"$in": [ObjectId(v) for v in window]}}, {"creator_id": 1})}
    creators = {d.get("creator_id") for d in shown.values()}
    ok = {u["_id"] async for u in db.users.find({"_id": {"$in": list(creators)}, "is_active": {"$ne": False}},
                                                 {"_id": 1})}
    visible = [v for v in window if v in shown and shown[v].get("creator_id") in ok]
    page = visible[skip: skip + limit]
    views = await _views_last_hour(db, page)
    out = []
    for v in page:
        rank = visible.index(v) + 1
        before = prev.get(v) if prev else None
        if prev is None:
            change = None
        elif not isinstance(before, int):
            change = "new"
        else:
            change = before - rank
        out.append({"id": v, "rank": rank, "change": change, "views_last_hour": views.get(v, 0)})
    return out


async def countries(db, limit: int = 12) -> List[dict]:
    """Countries with posts trending right now, most first, with display names."""
    ranked = await trending._ranking(db)
    counts: Dict[str, int] = {}
    for r in ranked:
        c = (r[2] or "").lower()
        if len(c) == 2 and c.isalpha():
            counts[c] = counts.get(c, 0) + 1
    top = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:limit]
    return [{"code": c, "name": hg.COUNTRIES.get(c, c.upper()), "count": n} for c, n in top]
