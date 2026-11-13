"""What's new: in-app announcements ("whats_new" switch, off by default).

Made to be welcome, not noise:
- At most ONE a day per person, and each one only ever once (seen = done).
- Never to accounts younger than NEW_USER_DAYS (everything is new to them).
- Only when the feature it announces is switched on, and only on app versions
  that have it (min_app), so "Quick posts are here" never reaches an old app.
- Text plus an optional small picture from Wano's own storage: a few KB.
- The app shows it as a small card on top of the feed, never pausing a video.
- Each one has a button that opens the feature itself (a fixed list of places).
- Admins write them on the admin page and see shown / tapped / closed counts.
- Everyone can scroll back through the recent ones (Settings > What's new).
"""

import re
from datetime import datetime, timedelta
from typing import List, Optional

from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from app.core.config import settings

NEW_USER_DAYS = 3
GAP = timedelta(hours=24)
LIST_MAX = 10
TITLE_MAX = 60
BODY_MAX = 180
BUTTON_MAX = 24
# Where an announcement's button may take people (the app knows each one).
ACTIONS = ("none", "quick_post", "wano_tv", "my_profile", "settings", "categories", "points")
EVENTS = ("tap", "close")

SUGGESTED = [
    {"key": "less_data", "title": "Wano now uses less data", "feature": "gzip_api", "min_app": "2.3.24",
     "body": "Feeds, pictures and messages now load with a fraction of the data. Same Wano, lighter on your bundle.",
     "button": "", "action": "none", "priority": 1},
    {"key": "tv_strip", "title": "Wano TV, right in your feed", "feature": "tv_strip", "min_app": "2.3.27",
     "body": "Now and then a little ticket shows a new African film or series. Tap it to watch.",
     "button": "See Wano TV", "action": "wano_tv", "priority": 2},
    {"key": "quick_posts", "title": "New: quick posts on your photo", "feature": "quick_posts", "min_app": "2.3.28",
     "body": "Put a 30-second voice note or a short card on your photo for 24 hours. A green-orange ring means something new.",
     "button": "Make one", "action": "quick_post", "priority": 3},
]


def _now() -> datetime:
    return datetime.utcnow()


def version_tuple(v) -> Optional[tuple]:
    if not isinstance(v, str) or not re.fullmatch(r"\d{1,3}(\.\d{1,3}){1,3}", v.strip()):
        return None
    return tuple(int(x) for x in v.strip().split("."))


def _cdn(u) -> Optional[str]:
    base = f"{settings.DO_SPACES_CDN_URL}/"
    return u if isinstance(u, str) and u.startswith(base) and len(u) <= 1024 else None


def public(a: dict) -> dict:
    return {"id": str(a["_id"]), "title": a.get("title") or "", "body": a.get("body") or "",
            "image": _cdn(a.get("image")), "button": a.get("button") or "",
            "action": a.get("action") if a.get("action") in ACTIONS else "none",
            "at": a.get("starts_at")}


async def _live(db, app_version: Optional[str], now: datetime) -> List[dict]:
    from app.services import features
    on = await features.current(db)
    mine = version_tuple(app_version)
    rows = await db.announcements.find({"active": True, "starts_at": {"$lte": now}}).sort(
        [("priority", -1), ("starts_at", 1)]).to_list(100)
    out = []
    for a in rows:
        if isinstance(a.get("ends_at"), datetime) and a["ends_at"] <= now:
            continue
        if a.get("feature") and on.get(a["feature"]) is not True:
            continue
        need = version_tuple(a.get("min_app"))
        if need and (mine is None or mine < need):
            continue
        out.append(a)
    return out


async def next_for(db, me: str, app_version: Optional[str], now: Optional[datetime] = None) -> dict:
    """The one announcement to show this person now, or none."""
    now = now or _now()
    uid = ObjectId(me)
    u = await db.users.find_one({"_id": uid}, {"created_at": 1}) or {}
    made = u.get("created_at")
    if isinstance(made, datetime) and made.replace(tzinfo=None) > now - timedelta(days=NEW_USER_DAYS):
        return {"item": None}
    last = await db.announce_last.find_one({"_id": uid})
    if last and isinstance(last.get("at"), datetime) and last["at"] > now - GAP:
        return {"item": None}
    live = await _live(db, app_version, now)
    if not live:
        return {"item": None}
    seen = {d["a"] async for d in db.announce_seen.find(
        {"_id": {"$in": [f"{a['_id']}:{uid}" for a in live]}}, {"a": 1})}
    for a in live:
        if a["_id"] not in seen:
            return {"item": public(a)}
    return {"item": None}


async def shown(db, me: str, aid: str, now: Optional[datetime] = None) -> dict:
    """The app showed it: it's now seen (never again) and starts today's gap."""
    now = now or _now()
    a = await _get(db, aid)
    uid = ObjectId(me)
    try:
        await db.announce_seen.insert_one({"_id": f"{a['_id']}:{uid}", "a": a["_id"], "at": now, "state": "shown"})
    except DuplicateKeyError:
        return {"ok": True}
    await db.announcements.update_one({"_id": a["_id"]}, {"$inc": {"stats.shown": 1}})
    await db.announce_last.update_one({"_id": uid}, {"$set": {"at": now}}, upsert=True)
    return {"ok": True}


async def event(db, me: str, aid: str, kind: str) -> dict:
    """Tap or close, counted once per person and only after it was shown."""
    a = await _get(db, aid)
    res = await db.announce_seen.update_one({"_id": f"{a['_id']}:{ObjectId(me)}", "state": "shown"},
                                            {"$set": {"state": kind, "done_at": _now()}})
    if res.modified_count:
        await db.announcements.update_one({"_id": a["_id"]}, {"$inc": {f"stats.{kind}": 1}})
    return {"ok": True, "counted": bool(res.modified_count)}


async def recent(db, app_version: Optional[str]) -> List[dict]:
    return [public(a) for a in sorted(await _live(db, app_version, _now()),
                                      key=lambda a: a.get("starts_at") or _now(), reverse=True)[:LIST_MAX]]


async def _get(db, aid: str) -> dict:
    if not isinstance(aid, str) or not ObjectId.is_valid(aid):
        raise HTTPException(status_code=404, detail="Not found")
    a = await db.announcements.find_one({"_id": ObjectId(aid), "active": True})
    if not a:
        raise HTTPException(status_code=404, detail="Not found")
    return a


# ------------------------------------------------------------------ admin

def clean(body: dict) -> dict:
    from app.services.messages import clean_text
    from app.api.v1.config import APP_FEATURES
    title = clean_text(body.get("title"))[:TITLE_MAX + 1]
    text = clean_text(body.get("body"))[:BODY_MAX + 1]
    if not title or len(title) > TITLE_MAX:
        raise HTTPException(status_code=400, detail=f"A title of 1 to {TITLE_MAX} characters")
    if not text or len(text) > BODY_MAX:
        raise HTTPException(status_code=400, detail=f"A message of 1 to {BODY_MAX} characters")
    button = clean_text(body.get("button") or "")
    if len(button) > BUTTON_MAX:
        raise HTTPException(status_code=400, detail=f"Button text up to {BUTTON_MAX} characters")
    action = body.get("action") or "none"
    if action not in ACTIONS:
        raise HTTPException(status_code=400, detail="Unknown button target")
    if action != "none" and not button:
        raise HTTPException(status_code=400, detail="Give the button some text")
    feature = body.get("feature") or None
    if feature is not None and feature not in APP_FEATURES:
        raise HTTPException(status_code=400, detail="Unknown switch")
    min_app = body.get("min_app") or None
    if min_app is not None and not version_tuple(min_app):
        raise HTTPException(status_code=400, detail="App version like 2.3.28")
    image = body.get("image") or None
    if image is not None and not _cdn(image):
        raise HTTPException(status_code=400, detail="Pictures must be on Wano's own storage")
    pr = body.get("priority", 0)
    if not isinstance(pr, int) or isinstance(pr, bool) or not (0 <= pr <= 100):
        raise HTTPException(status_code=400, detail="Priority 0 to 100")
    return {"title": title, "body": text, "button": button, "action": action, "feature": feature,
            "min_app": min_app, "image": image, "priority": pr}


async def admin_list(db) -> List[dict]:
    rows = await db.announcements.find({}).sort("created_at", -1).to_list(100)
    out = []
    for a in rows:
        st = a.get("stats") or {}
        s = int(st.get("shown") or 0)
        out.append({**public(a), "active": a.get("active") is True, "feature": a.get("feature"),
                    "min_app": a.get("min_app"), "priority": int(a.get("priority") or 0),
                    "shown": s, "tap": int(st.get("tap") or 0), "close": int(st.get("close") or 0),
                    "tap_rate": round((st.get("tap") or 0) / s, 3) if s else None})
    return out


async def admin_save(db, admin: str, body: dict, aid: Optional[str] = None) -> dict:
    doc = clean(body)
    active = body.get("active") is True
    now = _now()
    if aid:
        if not ObjectId.is_valid(aid):
            raise HTTPException(status_code=404, detail="Not found")
        old = await db.announcements.find_one({"_id": ObjectId(aid)})
        if not old:
            raise HTTPException(status_code=404, detail="Not found")
        upd = {**doc, "active": active, "updated_at": now}
        if active and not old.get("active"):
            upd["starts_at"] = now           # goes out from the moment it's turned on
        await db.announcements.update_one({"_id": old["_id"]}, {"$set": upd})
        return {"id": aid}
    res = await db.announcements.insert_one({**doc, "active": active, "starts_at": now, "created_at": now,
                                             "by": admin, "stats": {}})
    return {"id": str(res.inserted_id)}


async def add_suggested(db, admin: str) -> int:
    n = 0
    for s in SUGGESTED:
        if await db.announcements.find_one({"key": s["key"]}, {"_id": 1}):
            continue
        body = {k: v for k, v in s.items() if k != "key"}
        doc = clean(body)
        await db.announcements.insert_one({**doc, "key": s["key"], "active": False, "starts_at": _now(),
                                           "created_at": _now(), "by": admin, "stats": {}})
        n += 1
    return n
