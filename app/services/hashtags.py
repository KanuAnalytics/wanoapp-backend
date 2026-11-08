"""Hashtag suggestions for captions: live challenges first, then what's trending
(public posts from the last TRENDING_DAYS), then the ready-made library
(app/data/hashtag_library.py). Matching ignores case and accents."""

import time
import unicodedata
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from app.data.hashtag_library import IDEAS, LIBRARY

TRENDING_DAYS = 7
POPULAR_DAYS = 30
CACHE_SECONDS = 600
MAX_RESULTS = 20

_cache: Dict[str, tuple] = {}


def fold(s: str) -> str:
    """Lower case without accents: 'École' -> 'ecole'."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def clean_query(q: Optional[str]) -> str:
    q = (q or "").strip().lstrip("#")
    return "".join(c for c in q if c.isalnum() or c == "_")[:40]


def _library_index() -> List[dict]:
    hit = _cache.get("library")
    if hit:
        return hit[1]
    seen, out = set(), []
    for cat in LIBRARY:
        for t in cat["tags"]:
            key = fold(t["tag"])
            if key in seen:
                continue
            seen.add(key)
            out.append({"tag": t["tag"], "category": cat["category"], "key": key})
    _cache["library"] = (float("inf"), out)
    return out


def _visible_posts(since: datetime) -> dict:
    return {"is_active": True, "privacy": "public", "created_at": {"$gte": since},
            "moderation.state": {"$nin": ["held", "removed"]}}


async def _popular(db, days: int) -> List[dict]:
    """[{tag, posts}] most used by active creators' public posts, cached."""
    key = f"popular:{days}"
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    since = datetime.utcnow() - timedelta(days=days)
    rows = []
    banned = await db.users.distinct("_id", {"is_active": False})
    async for r in db.videos.aggregate([
        {"$match": {**_visible_posts(since), "hashtags": {"$exists": True, "$ne": []},
                    "creator_id": {"$nin": banned}}},
        {"$project": {"hashtags": 1}},
        {"$unwind": "$hashtags"},
        {"$match": {"hashtags": {"$type": "string"}}},
        {"$group": {"_id": "$hashtags", "posts": {"$sum": 1}}},
        {"$sort": {"posts": -1, "_id": 1}},
        {"$limit": 2000},
    ]):
        rows.append({"tag": r["_id"], "posts": int(r["posts"])})
    # Same tag in different cases counts once, under its most used spelling.
    merged: Dict[str, dict] = {}
    for r in rows:
        k = fold(r["tag"])
        if k in merged:
            merged[k]["posts"] += r["posts"]
        else:
            merged[k] = dict(r)
    out = sorted(merged.values(), key=lambda r: (-r["posts"], fold(r["tag"])))
    _cache[key] = (time.monotonic() + CACHE_SECONDS, out)
    return out


async def _live_challenges(db) -> List[str]:
    now = datetime.utcnow()
    return [c["hashtag"] async for c in db.challenges.find(
        {"starts_at": {"$lte": now}, "ends_at": {"$gt": now}}, {"hashtag": 1}).sort("ends_at", 1).limit(50)
        if isinstance(c.get("hashtag"), str)]


async def suggest(db, q: Optional[str], limit: int = MAX_RESULTS) -> List[dict]:
    query = fold(clean_query(q))
    limit = max(1, min(limit, MAX_RESULTS))
    out, seen = [], set()

    def add(tag: str, source: str, posts: int = 0, category: Optional[str] = None):
        k = fold(tag)
        if k in seen or (query and not k.startswith(query)) or len(out) >= limit:
            return
        seen.add(k)
        out.append({"tag": tag, "source": source, "posts": posts, "category": category})

    for tag in await _live_challenges(db):
        add(tag, "challenge")
    for r in await _popular(db, TRENDING_DAYS if query else POPULAR_DAYS):
        add(r["tag"], "trending", r["posts"])
    if len(out) < limit:
        lib = _library_index()
        for t in (lib if query else lib[::max(1, len(lib) // limit)]):
            add(t["tag"], "library", 0, t["category"])
    return out


def library() -> List[dict]:
    return [{"category": c["category"], "tags": c["tags"]} for c in LIBRARY]


def ideas(lang: Optional[str]) -> List[dict]:
    rows = IDEAS.get((lang or "en")[:2].lower()) or IDEAS["en"]
    return [{"text": t, "hashtag": h} for t, h in rows]
