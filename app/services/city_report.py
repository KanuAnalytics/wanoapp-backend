"""Admin > Kampala: are people in one city coming back each week? (Nov 2026)

Growth plan: win one city first. The number that matters is weekly returning
users, not downloads. For a city (Home Ground city, or the city on the
profile), its country, and everyone, week by week (Monday to Sunday, UTC):

  active     signed-in people who watched, liked, posted or sent a message
  returning  active this week AND the week before
  return %   returning / active the week before
  new        accounts created that week
  new back   of last week's new accounts, how many were active this week
  posters    people who posted a video that week

Activity comes from watch history (its latest watch of each video), posts and
messages, so someone who only re-watched old videos can be missed; signed-out
viewers aren't counted. Read-only; cached for a few minutes.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Optional, Set

WEEKS = 8
CACHE_SECONDS = 600
TOP_CREATORS = 25

_cache: Dict[str, tuple] = {}


def _monday(dt: datetime) -> datetime:
    d = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return d - timedelta(days=d.weekday())


def _naive(dt) -> Optional[datetime]:
    if not isinstance(dt, datetime):
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _sid(v) -> Optional[str]:
    return str(v) if v is not None else None


async def _ids(db, query) -> Set[str]:
    return {str(d["_id"]) async for d in db.users.find(query, {"_id": 1})}


def _city(city_id: str) -> Optional[dict]:
    from app.services.home_ground import CITY
    return CITY.get(city_id)


async def report(db, city_id: str = "kampala", now: Optional[datetime] = None, fresh: bool = False) -> dict:
    hit = _cache.get(city_id)
    if hit and not fresh and now is None and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    city = _city(city_id)
    if city is None:
        raise ValueError("unknown city")
    cc = city["country"]
    now = _naive(now) or datetime.utcnow()
    first = _monday(now) - timedelta(weeks=WEEKS - 1)
    # One extra week before the first so the first week has a "week before".
    start = first - timedelta(weeks=1)
    weeks = [first + timedelta(weeks=i) for i in range(WEEKS)]

    def week_of(dt) -> Optional[int]:
        dt = _naive(dt)
        if dt is None or dt < start or dt > now:
            return None
        return (dt - start).days // 7        # 0 = the week before the first

    city_q = {"$or": [{"home_ground.city": city_id}, {"localization.city_key": city["name"].casefold()}]}
    in_city = await _ids(db, city_q)
    in_country = await _ids(db, {"localization.country": {"$in": [cc, cc.upper()]}}) | in_city

    active: Dict[str, Set[int]] = {}
    posted: Dict[str, Set[int]] = {}

    def mark(table, uid, dt):
        w = week_of(dt)
        if uid and w is not None:
            table.setdefault(uid, set()).add(w)

    async for d in db.watch_history.find({"watched_at": {"$gte": start}}, {"user_id": 1, "watched_at": 1}):
        mark(active, _sid(d.get("user_id")), d.get("watched_at"))
    async for d in db.videos.find({"created_at": {"$gte": start}}, {"creator_id": 1, "created_at": 1}):
        uid = _sid(d.get("creator_id"))
        mark(active, uid, d.get("created_at"))
        mark(posted, uid, d.get("created_at"))
    async for d in db.messages.find({"created_at": {"$gte": start}}, {"sender_id": 1, "created_at": 1}):
        mark(active, _sid(d.get("sender_id")), d.get("created_at"))

    joined: Dict[str, int] = {}
    async for d in db.users.find({"created_at": {"$gte": start}}, {"created_at": 1}):
        w = week_of(d.get("created_at"))
        if w is not None:
            joined[str(d["_id"])] = w

    def rows(members: Optional[Set[str]]) -> list:
        def keep(uid):
            return members is None or uid in members
        act = {u: ws for u, ws in active.items() if keep(u)}
        out = []
        for i in range(1, WEEKS + 1):
            a = {u for u, ws in act.items() if i in ws}
            before = {u for u, ws in act.items() if i - 1 in ws}
            back = a & before
            new = {u for u, w in joined.items() if w == i and keep(u)}
            new_last = {u for u, w in joined.items() if w == i - 1 and keep(u)}
            new_back = new_last & a
            out.append({
                "week": weeks[i - 1].strftime("%Y-%m-%d"),
                "partial": i == WEEKS,
                "active": len(a),
                "returning": len(back),
                "return_pct": round(100 * len(back) / len(before)) if before else None,
                "new": len(new),
                "new_back": len(new_back),
                "new_back_pct": round(100 * len(new_back) / len(new_last)) if new_last else None,
                "posters": sum(1 for u, ws in posted.items() if i in ws and keep(u)),
            })
        return out

    creators = await _top_creators(db, in_city, now)
    result = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "city": {"id": city_id, "name": city["name"], "country": cc},
        "members": {"city": len(in_city), "country": len(in_country)},
        "segments": [
            {"key": "city", "name": city["name"], "rows": rows(in_city)},
            {"key": "country", "name": _country_name(cc), "rows": rows(in_country)},
            {"key": "all", "name": "Everyone", "rows": rows(None)},
        ],
        "creators": creators,
    }
    _cache[city_id] = (time.monotonic(), result)
    return result


def _country_name(cc: str) -> str:
    try:
        from app.data.home_ground_cities import COUNTRY_NAMES
        return COUNTRY_NAMES.get(cc, cc.upper())
    except Exception:
        return cc.upper()


async def _top_creators(db, members: Iterable[str], now: datetime) -> list:
    """People in the city who posted in the last 4 weeks, most posts first:
    who to thank, feature and ask to bring friends."""
    from bson import ObjectId
    oids = [ObjectId(u) for u in members if ObjectId.is_valid(u)]
    if not oids:
        return []
    since = now - timedelta(days=28)
    counts = {}
    async for d in db.videos.aggregate([
        {"$match": {"creator_id": {"$in": oids}, "created_at": {"$gte": since}}},
        {"$group": {"_id": "$creator_id", "posts": {"$sum": 1}, "views": {"$sum": {"$ifNull": ["$views_count", 0]}}}},
        {"$sort": {"posts": -1, "views": -1}},
        {"$limit": TOP_CREATORS},
    ]):
        counts[d["_id"]] = d
    if not counts:
        return []
    out = []
    async for u in db.users.find({"_id": {"$in": list(counts)}},
                                 {"username": 1, "display_name": 1, "followers_count": 1}):
        c = counts[u["_id"]]
        out.append({"id": str(u["_id"]), "username": u.get("username"), "name": u.get("display_name"),
                    "followers": u.get("followers_count") or 0, "posts": c["posts"], "views": c["views"]})
    out.sort(key=lambda r: (-r["posts"], -r["views"]))
    return out
