"""Wano TV, round 3 (Nov 2026). Each part has its own switch, off by default.

- tv_age_gate: titles rated above a viewer's age (16+, 18+ ...) are left out of
  the catalogue, History, playing and downloads for accounts whose saved
  birthday makes them under that age. Unknown birthdays count as adult, as
  everywhere else in Wano (home_ground.is_minor).
- tv_loves: a heart on each title; the count shows on the title.
- tv_coming_soon: titles marked "Coming soon" in the admin show in their own
  row with "Remind me"; when the title goes live, everyone who asked gets a
  push.
- tv_clips: feed posts linked to a title (Admin > Wano TV titles) carry a
  "Watch the full film" button.
- tv_downloads: an offline copy (MP4) of a film to watch with no data, for
  signed-in people, DOWNLOADS_PER_DAY a day.
Admin "Tell everyone" sends one "New on Wano TV" push per title.
"""

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException, status
from pymongo.errors import DuplicateKeyError
from pymongo import ReturnDocument

from app.services import home_ground

logger = logging.getLogger(__name__)

DOWNLOADS_PER_DAY = 20
OFFLINE_DAYS = 30            # the app keeps a download this long, then checks again
LOVE_CACHE_SECONDS = 60
CLIPS_CACHE_SECONDS = 120
MAX_CLIPS = 500
MAX_REMINDERS_LISTED = 200
_RATING = re.compile(r"(\d{1,2})")

_cache: dict = {}


def clear_cache():
    _cache.clear()


async def on(db, key: str) -> bool:
    from app.services import creator_push
    return await creator_push.on(db, key)


async def require(db, key: str):
    from app.services import creator_push
    await creator_push.require(db, key)


def _oid(x: str) -> ObjectId:
    if not isinstance(x, str) or not ObjectId.is_valid(x):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid title ID")
    return ObjectId(x)


# ---------------------------------------------------------------- age gate

def min_age(rating) -> int:
    """'16+' -> 16, '18' -> 18, 'PG-13' -> 13, 'All' / 'G' / unknown -> 0."""
    if not isinstance(rating, str):
        return 0
    m = _RATING.search(rating)
    n = int(m.group(1)) if m else 0
    return n if 0 < n <= 21 else 0


async def viewer_age(db, user_id: Optional[str]) -> Optional[int]:
    """The viewer's age when the gate applies to them (switch on, a minor), else None."""
    if not user_id or not ObjectId.is_valid(user_id) or not await on(db, "tv_age_gate"):
        return None
    u = await db.users.find_one({"_id": ObjectId(user_id)}, {"date_of_birth": 1})
    if not u or not home_ground.is_minor(u):
        return None
    return home_ground.age_of(u.get("date_of_birth"))


def allowed(doc: dict, age: Optional[int]) -> bool:
    return age is None or min_age(doc.get("age_rating")) <= age


async def check_allowed(db, doc: dict, user_id: Optional[str]) -> None:
    if not allowed(doc, await viewer_age(db, user_id)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"This title is rated {doc.get('age_rating')}")


# ---------------------------------------------------------------- loves

async def love_counts(db) -> Dict[ObjectId, int]:
    hit = _cache.get("loves")
    if hit and time.monotonic() - hit[0] < LOVE_CACHE_SECONDS:
        return hit[1]
    rows = await db.tv_loves.aggregate([{"$group": {"_id": "$title_id", "n": {"$sum": 1}}}]).to_list(1000)
    out = {r["_id"]: int(r["n"]) for r in rows}
    _cache["loves"] = (time.monotonic(), out)
    return out


async def _live_title(db, title_id: str, projection: Optional[dict] = None) -> dict:
    """projection=None reads the whole title."""
    doc = await db.tv_titles.find_one({"_id": _oid(title_id), "is_active": True}, projection)
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Title not found")
    return doc


async def love(db, me: str, title_id: str, on_: bool) -> dict:
    doc = await _live_title(db, title_id, {"_id": 1})
    key = f"{doc['_id']}:{me}"
    if on_:
        try:
            await db.tv_loves.insert_one({"_id": key, "title_id": doc["_id"], "user_id": ObjectId(me),
                                          "at": datetime.utcnow()})
        except DuplicateKeyError:
            pass
    else:
        await db.tv_loves.delete_one({"_id": key})
    _cache.pop("loves", None)
    n = await db.tv_loves.count_documents({"title_id": doc["_id"]})
    return {"loved": on_, "loves": n}


async def mine(db, me: str) -> dict:
    out = {"loved": [], "reminders": []}
    uid = ObjectId(me)
    if await on(db, "tv_loves"):
        out["loved"] = [str(r["title_id"]) async for r in db.tv_loves.find({"user_id": uid}, {"title_id": 1}).limit(500)]
    if await on(db, "tv_coming_soon"):
        out["reminders"] = [str(r["title_id"]) async for r in db.tv_reminders.find(
            {"user_id": uid}, {"title_id": 1}).limit(MAX_REMINDERS_LISTED)]
    return out


# ---------------------------------------------------------------- coming soon

def _https(u) -> Optional[str]:
    return u if isinstance(u, str) and u.startswith("https://") and len(u) <= 2048 else None


def coming_card(doc: dict) -> dict:
    from app.services.tv_release import preview, episodes
    trailer = doc.get("trailer") if isinstance(doc.get("trailer"), dict) else {}
    return {"id": str(doc["_id"]), "slug": doc.get("slug") or "", "title": doc.get("title") or "",
            "type": doc.get("type") or "movie", "genres": [g for g in (doc.get("genres") or []) if isinstance(g, str)],
            "age_rating": doc.get("age_rating") or "", "synopsis": doc.get("synopsis") or doc.get("description") or "",
            "poster": _https(doc.get("poster")), "backdrop": _https(doc.get("backdrop")),
            "trailer": {"video_url": _https(trailer.get("video_url")), "thumbnail": _https(trailer.get("thumbnail"))},
            "upcoming_episodes": [preview(e) for e in episodes(doc) if e.get("is_published") is not False and e.get("publish_at")],
            "coming_at": doc.get("coming_at") if isinstance(doc.get("coming_at"), str) else None}


async def coming_soon(db, user_id: Optional[str]) -> List[dict]:
    await require(db, "tv_coming_soon")
    age = await viewer_age(db, user_id)
    docs = await db.tv_titles.find({"status": "coming_soon"}).to_list(100)
    docs = [d for d in docs if allowed(d, age) and isinstance(d.get("title"), str) and _https(d.get("poster"))]
    docs.sort(key=lambda d: (d.get("coming_at") or "9999", str(d["_id"])))
    return [coming_card(d) for d in docs]


async def remind(db, me: str, title_id: str, on_: bool) -> dict:
    doc = await db.tv_titles.find_one({"_id": _oid(title_id), "$or": [{"status": "coming_soon"}, {"type": "series", "is_active": True}]}, {"_id": 1})
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Title not found")
    key = f"{doc['_id']}:{me}"
    if on_:
        await db.tv_reminders.update_one({"_id": key}, {"$setOnInsert": {
            "title_id": doc["_id"], "user_id": ObjectId(me), "at": datetime.utcnow(), "sent": False}}, upsert=True)
    else:
        await db.tv_reminders.delete_one({"_id": key})
    return {"reminded": on_}


# ---------------------------------------------------------------- pushes

def _tokens(user: dict) -> List[str]:
    t = user.get("expo_push_tokens")
    return [x for x in t if isinstance(x, str) and x] if isinstance(t, list) else []


async def _send(messages: List[dict]) -> int:
    from app.services import growth_notifications as gn
    sent = 0
    for i in range(0, len(messages), gn.EXPO_BATCH):
        if await gn._send_batch(messages[i:i + gn.EXPO_BATCH]):
            sent += len(messages[i:i + gn.EXPO_BATCH])
    return sent


def _message(token: str, doc: dict, title: str, body: str) -> dict:
    return {"to": token, "title": title[:65], "body": body[:178], "sound": "default",
            "data": {"type": "tv_title", "title_id": str(doc["_id"])}}


async def send_reminders(db, doc: dict) -> int:
    """The title just went live: push everyone who tapped Remind me (once each)."""
    sent_people = 0
    min_a = min_age(doc.get("age_rating"))
    while True:
        rows = await db.tv_reminders.find({"title_id": doc["_id"], "sent": {"$ne": True}}).limit(500).to_list(500)
        if not rows:
            break
        await db.tv_reminders.update_many({"_id": {"$in": [r["_id"] for r in rows]}},
                                          {"$set": {"sent": True, "sent_at": datetime.utcnow()}})
        users = await db.users.find({"_id": {"$in": [r["user_id"] for r in rows]}, "is_active": {"$ne": False}},
                                    {"expo_push_tokens": 1, "date_of_birth": 1}).to_list(500)
        msgs = []
        for u in users:
            if min_a and home_ground.is_minor(u) and (home_ground.age_of(u.get("date_of_birth")) or 0) < min_a:
                continue
            toks = _tokens(u)
            if toks:
                sent_people += 1
                msgs += [_message(t, doc, f"{doc.get('title')} is on Wano TV",
                                  "You asked us to remind you. It's out now: watch free.") for t in toks]
        await _send(msgs)
    return sent_people


async def notify_everyone(db, doc: dict, now: Optional[datetime] = None) -> int:
    """'New on Wano TV: <title>' to everyone with the app's notifications on,
    skipping people in their quiet hours and anyone the rating leaves out."""
    from app.services import growth_notifications as gn
    now = now or datetime.utcnow()
    min_a = min_age(doc.get("age_rating"))
    title = f"New on Wano TV: {doc.get('title')}"
    body = (doc.get("description") or doc.get("synopsis") or "Watch free in the Wano app.")
    body = gn._clip(body, gn.BODY_MAX)
    people = 0
    last = None
    while True:
        q = {"is_active": {"$ne": False}, "expo_push_tokens.0": {"$exists": True}}
        if last is not None:
            q["_id"] = {"$gt": last}
        users = await db.users.find(q, {"expo_push_tokens": 1, "date_of_birth": 1, "localization": 1}) \
            .sort("_id", 1).limit(500).to_list(500)
        if not users:
            break
        last = users[-1]["_id"]
        msgs = []
        for u in users:
            if gn._quiet(u, now):
                continue
            if min_a and home_ground.is_minor(u, now) and (home_ground.age_of(u.get("date_of_birth"), now) or 0) < min_a:
                continue
            people += 1
            msgs += [_message(t, doc, title, body) for t in _tokens(u)]
        await _send(msgs)
    return people


# ---------------------------------------------------------------- clips in the feed

async def clips(db) -> Dict[str, dict]:
    await require(db, "tv_clips")
    hit = _cache.get("clips")
    if hit and time.monotonic() - hit[0] < CLIPS_CACHE_SECONDS:
        return hit[1]
    # Wano's own links first, then viewers' clips, newest first.
    rows = await db.tv_clips.find({}).sort([("by_viewer", 1), ("at", -1)]).limit(MAX_CLIPS).to_list(MAX_CLIPS)
    titles = {d["_id"]: d async for d in db.tv_titles.find(
        {"_id": {"$in": list({r["title_id"] for r in rows})}, "is_active": True},
        {"title": 1, "poster": 1, "type": 1, "age_rating": 1})}
    out = {}
    for r in rows:
        t = titles.get(r["title_id"])
        if t:
            out[r["_id"]] = {"title_id": str(t["_id"]), "title": t.get("title") or "", "type": t.get("type") or "movie",
                             "age_rating": t.get("age_rating") or ""}
    _cache["clips"] = (time.monotonic(), out)
    return out


async def link_clip(db, video_id: str, title_id: Optional[str]) -> dict:
    if not ObjectId.is_valid(video_id or ""):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Paste a post link or ID")
    if not await db.videos.find_one({"_id": ObjectId(video_id), "is_active": True}, {"_id": 1}):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Post not found")
    if title_id is None:
        await db.tv_clips.delete_one({"_id": video_id})
    else:
        t = await db.tv_titles.find_one({"_id": _oid(title_id)}, {"_id": 1})
        if not t:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Title not found")
        await db.tv_clips.update_one({"_id": video_id}, {"$set": {"title_id": t["_id"], "at": datetime.utcnow()}},
                                     upsert=True)
    _cache.pop("clips", None)
    return {"video_id": video_id, "title_id": title_id}


# ---------------------------------------------------------------- downloads

async def download(db, me: str, title_id: str, episode: Optional[str]) -> dict:
    from app.services import tv_cf
    await require(db, "tv_downloads")
    doc = await _live_title(db, title_id)
    await check_allowed(db, doc, me)
    src = doc
    if episode:
        src = None
        for s in doc.get("seasons") or []:
            for e in (s.get("episodes") or []) if isinstance(s, dict) else []:
                if isinstance(e, dict) and e.get("id") == episode:
                    src = e
        if src is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Episode not found")
    from app.services.tv_release import released, episodes
    if (episode and not released(src)) or (not episode and doc.get("type") == "series" and episodes(doc)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Episode not available")
    day = datetime.utcnow().strftime("%Y-%m-%d")
    lim = await db.tv_download_limits.find_one_and_update(
        {"_id": f"{me}:{day}"}, {"$inc": {"n": 1}, "$setOnInsert": {"at": datetime.utcnow()}},
        upsert=True, return_document=ReturnDocument.AFTER)
    if (lim or {}).get("n", 0) > DOWNLOADS_PER_DAY:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "That's a lot of downloads today. Try again tomorrow.")
    url = None
    uid = _download_uid(src)
    if uid:
        try:
            url = await asyncio.to_thread(tv_cf.download_url, uid)
        except tv_cf.CfError:
            url = None
    if not url:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This title can't be downloaded yet")
    await db.tv_downloads.update_one({"_id": f"{doc['_id']}:{episode or ''}:{me}"},
                                     {"$set": {"title_id": doc["_id"], "user_id": ObjectId(me), "episode": episode,
                                               "at": datetime.utcnow()}, "$inc": {"n": 1}}, upsert=True)
    return {"url": url, "keep_days": OFFLINE_DAYS, "expires_in": 6 * 3600}


def _download_uid(doc: dict) -> Optional[str]:
    """The Cloudflare video whose MP4 is the offline copy: the film itself, or a
    separate upload for films kept on Spaces."""
    from app.services import tv_cf
    if doc.get("download_ready") is not True:
        return None
    for k in ("cf_uid", "download_cf_uid"):
        if tv_cf.valid_uid(doc.get(k)):
            return doc[k]
    return None


def downloadable(doc: dict) -> bool:
    return _download_uid(doc) is not None
