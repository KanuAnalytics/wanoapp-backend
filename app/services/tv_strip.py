"""Wano TV strip ("tv_strip" switch, off by default).

A small dark "ticket" (poster, WANO TV · NEW SERIES, title, Watch) slides in
above the caption of some feed videos, to point people at Wano TV.

The app asks GET /tv/strip once a session: which titles to show (newest and
featured first, at most MAX_TITLES), on whose posts (Wano's house accounts for
now), and the rules below, so they can be tuned here without an app release.
It reports what happened (shown / tap / close) to POST /tv/strip/event; the
admin page shows the counts per film.

Data: the strip carries a small poster copy (w160 WebP, a few KB, made by the
small_images job); the trailer itself only loads if someone taps.
"""

import logging
from datetime import date, datetime, timedelta
from typing import List, Optional

from bson import ObjectId
from pymongo import ReturnDocument

logger = logging.getLogger(__name__)

RULES = {
    "after_portion": 0.7,      # shown once this much of the video has played
    "min_posts": 5,            # never in the first 5 posts of a session
    "gap_posts": 8,            # at least this many posts between two strips
    "max_per_session": 2,
    "show_seconds": 6,         # then it slides away by itself
    "closes_per_week": 3,      # closed this many times in 7 days: no more strips that week
}
MAX_TITLES = 4
NEW_DAYS = 45
EVENT_KINDS = ("shown", "tap", "close")
EVENTS_PER_DAY = 60            # per person (or per address when logged out)
KEEP_DAYS = 90


def _kicker(doc: dict, today: date) -> str:
    kind = "SERIES" if doc.get("type") == "series" else "FILM"
    rel = doc.get("release_date")
    if isinstance(rel, datetime):
        rel = rel.date()
    elif isinstance(rel, str):
        try:
            rel = date.fromisoformat(rel[:10])
        except ValueError:
            rel = None
    if isinstance(rel, date) and timedelta(0) <= today - rel <= timedelta(days=NEW_DAYS):
        return f"WANO TV · NEW {kind}"
    return f"WANO TV · {kind}"


def _https(u) -> Optional[str]:
    return u if isinstance(u, str) and u.startswith("https://") and len(u) <= 2048 else None


async def config(db, viewer: Optional[str], today: Optional[date] = None) -> dict:
    from app.services import small_images
    today = today or datetime.utcnow().date()
    docs = await db.tv_titles.find({"is_active": True}, {"title": 1, "type": 1, "poster": 1, "release_date": 1,
                                                         "featured": 1, "trailer": 1}).to_list(200)

    def rel(d):
        r = d.get("release_date")
        return str(r)[:10] if r else ""
    # Featured first, then the newest release.
    docs.sort(key=lambda d: (d.get("featured") is True, rel(d)), reverse=True)
    titles = []
    for d in docs:
        title = d.get("title")
        poster = _https(d.get("poster"))
        if not isinstance(title, str) or not title.strip() or not poster:
            continue
        titles.append({"id": str(d["_id"]), "title": title.strip()[:80], "kicker": _kicker(d, today),
                       "poster": poster, "poster_small": small_images.variant_url(poster, 160)})
        if len(titles) >= MAX_TITLES:
            break
    creators = [str(u["_id"]) async for u in db.users.find({"house_account": True, "is_active": {"$ne": False}},
                                                           {"_id": 1}).limit(50)]
    return {"titles": titles, "creators": creators, "rules": dict(RULES)}


async def record(db, who: str, title_id: str, kind: str, now: Optional[datetime] = None) -> bool:
    """Count one event. False when it was ignored (unknown title, over the cap)."""
    now = now or datetime.utcnow()
    if kind not in EVENT_KINDS or not ObjectId.is_valid(title_id):
        return False
    if not await db.tv_titles.find_one({"_id": ObjectId(title_id), "is_active": True}, {"_id": 1}):
        return False
    day = now.strftime("%Y-%m-%d")
    lim = await db.tv_strip_limits.find_one_and_update(
        {"_id": f"{who}:{day}"}, {"$inc": {"n": 1}, "$setOnInsert": {"at": now}}, upsert=True,
        return_document=ReturnDocument.AFTER)
    if (lim or {}).get("n", 0) > EVENTS_PER_DAY:
        return False
    await db.tv_strip_daily.update_one(
        {"_id": f"{day}:{title_id}"},
        {"$inc": {kind: 1}, "$setOnInsert": {"day": datetime(now.year, now.month, now.day),
                                             "title_id": ObjectId(title_id)}},
        upsert=True)
    return True


async def stats(db, days: int = 7, now: Optional[datetime] = None) -> List[dict]:
    now = now or datetime.utcnow()
    since = datetime(now.year, now.month, now.day) - timedelta(days=days - 1)
    rows = await db.tv_strip_daily.aggregate([
        {"$match": {"day": {"$gte": since}}},
        {"$group": {"_id": "$title_id", "shown": {"$sum": "$shown"}, "tap": {"$sum": "$tap"},
                    "close": {"$sum": "$close"}}},
    ]).to_list(200)
    names = {d["_id"]: d.get("title") async for d in db.tv_titles.find(
        {"_id": {"$in": [r["_id"] for r in rows]}}, {"title": 1})}
    out = []
    for r in rows:
        shown = int(r.get("shown") or 0)
        out.append({"title_id": str(r["_id"]), "title": names.get(r["_id"]) or "(removed)", "shown": shown,
                    "tap": int(r.get("tap") or 0), "close": int(r.get("close") or 0),
                    "tap_rate": round((r.get("tap") or 0) / shown, 3) if shown else None,
                    "close_rate": round((r.get("close") or 0) / shown, 3) if shown else None})
    out.sort(key=lambda x: -x["shown"])
    return out
