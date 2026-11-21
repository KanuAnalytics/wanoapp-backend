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

"whats_new_plus" (Nov 2026; the new looks need app 2.3.60, older apps get the
plain card, which every announcement still has):
- style: card (as before), poster (a full-width picture), story (2-4 swipeable
  pages, shown only when the app opens), update_notes (the first open of one
  app version), tip (the first time someone reaches a feature), banner (an
  event across Discover until its end date, which it must have).
- A picture needs a description for screen readers (alt).
- audience: creators / viewers, countries, languages, platform, adults only.
  Teens never get adults-only items, and an item tied to a feature reaches
  exactly the people that feature's switch reaches.
- Sending safely (the server enforces it, not the Admin page): a new item
  first goes to staff only ("team"); then 1%, then 10%, then everyone, one
  step at a time. Once anyone has seen it, its pictures can't change.
- Buttons can open Challenges or one challenge (apps before 2.3.60 get no button).
- Counts include "Not now", not just taps and closes.
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
ACTIONS = ("none", "quick_post", "wano_tv", "my_profile", "settings", "categories", "points",
           "challenges", "challenge")
OLD_ACTIONS = ("none", "quick_post", "wano_tv", "my_profile", "settings", "categories", "points")
PLUS_APP = (2, 3, 60)
EVENTS = ("tap", "close", "not_now")
STYLES = ("card", "poster", "story", "update_notes", "tip", "banner")
REACH_STEPS = ("team", 1, 10, 100)
PAGES_MAX = 4
ALT_MAX = 200

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


def public(a: dict, app_version: Optional[str] = None) -> dict:
    new_app = (version_tuple(app_version) or (0,)) >= PLUS_APP
    action = a.get("action") if a.get("action") in (ACTIONS if new_app else OLD_ACTIONS) else "none"
    out = {"id": str(a["_id"]), "title": a.get("title") or "", "body": a.get("body") or "",
           "image": _cdn(a.get("image")), "button": (a.get("button") or "") if action != "none" else "",
           "action": action, "at": a.get("starts_at")}
    if new_app:
        out.update({"style": a.get("style") or "card", "alt": a.get("alt") or "",
                    "pages": [{"image": _cdn(p.get("image")), "title": p.get("title") or "", "body": p.get("body") or "",
                               "alt": p.get("alt") or ""} for p in (a.get("pages") or [])],
                    "target": a.get("target"), "ends_at": a.get("ends_at"), "tip_key": a.get("tip_key"),
                    "version": a.get("version")})
    return out


def _bucket(uid, aid) -> int:
    import hashlib
    return int(hashlib.sha1(f"{uid}:{aid}".encode()).hexdigest()[:8], 16) % 100


async def _reaches(db, a: dict, user: Optional[dict], plus: bool) -> bool:
    """Audience and step-by-step reach (team first). Old items reach everyone."""
    from app.services import staff, team
    reach = a.get("reach", 100)
    if reach == "team":
        return staff.is_staff(user)
    if isinstance(reach, int) and reach < 100 and user is not None and not staff.is_staff(user):
        if _bucket(user["_id"], a["_id"]) >= reach:
            return False
    if a.get("audience") and not team.matches(a["audience"], user):
        return False
    return True


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


async def _plus(db) -> bool:
    from app.services import creator_push
    return await creator_push.on(db, "whats_new_plus")


SPECIAL = ("update_notes", "tip", "banner")      # never the daily card


async def next_for(db, me: str, app_version: Optional[str], now: Optional[datetime] = None,
                   platform: Optional[str] = None, lang: Optional[str] = None, tz: int = 0) -> dict:
    """The one announcement to show this person now, or none."""
    now = now or _now()
    uid = ObjectId(me)
    u = await db.users.find_one({"_id": uid}, {"created_at": 1, "user_type": 1, "staff_role": 1, "is_active": 1,
                                               "date_of_birth": 1, "videos_count": 1, "localization": 1}) or {}
    plus = await _plus(db)
    from app.services import attention
    if not await attention.available(db, me, "whats_new", tz):
        return {"item": None}
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
        if a["_id"] in seen or (a.get("style") in SPECIAL):
            continue
        if not await _reaches(db, a, {**u, "_id": uid}, plus):
            continue
        if (a.get("audience") or {}).get("platform") not in (None, "any") and platform \
                and a["audience"]["platform"] != platform:
            continue
        return {"item": public(a, app_version)}
    return {"item": None}


async def shown(db, me: str, aid: str, now: Optional[datetime] = None, tz: int = 0) -> dict:
    """The app showed it: it's now seen (never again) and starts today's gap."""
    now = now or _now()
    a = await _get(db, aid)
    uid = ObjectId(me)
    try:
        await db.announce_seen.insert_one({"_id": f"{a['_id']}:{uid}", "a": a["_id"], "at": now, "state": "shown"})
    except DuplicateKeyError:
        return {"ok": True}
    await db.announcements.update_one({"_id": a["_id"]}, {"$inc": {"stats.shown": 1}})
    if a.get("style") not in SPECIAL:
        await db.announce_last.update_one({"_id": uid}, {"$set": {"at": now}}, upsert=True)
        from app.services import attention
        await attention.take(db, me, "whats_new", tz)
    return {"ok": True}


async def event(db, me: str, aid: str, kind: str) -> dict:
    """Tap or close, counted once per person and only after it was shown."""
    a = await _get(db, aid)
    res = await db.announce_seen.update_one({"_id": f"{a['_id']}:{ObjectId(me)}", "state": "shown"},
                                            {"$set": {"state": kind, "done_at": _now()}})
    if res.modified_count:
        await db.announcements.update_one({"_id": a["_id"]}, {"$inc": {f"stats.{kind}": 1}})
    return {"ok": True, "counted": bool(res.modified_count)}


async def recent(db, app_version: Optional[str], me: Optional[str] = None) -> List[dict]:
    user = None
    if me:
        user = await db.users.find_one({"_id": ObjectId(me)}, {"user_type": 1, "staff_role": 1, "is_active": 1,
                                                                "date_of_birth": 1, "videos_count": 1, "localization": 1})
    plus = await _plus(db)
    rows = [a for a in await _live(db, app_version, _now())
            if a.get("style") not in ("tip",) and await _reaches(db, a, user, plus)]
    return [public(a, app_version) for a in sorted(rows, key=lambda a: a.get("starts_at") or _now(),
                                                  reverse=True)[:LIST_MAX]]


async def special(db, me: str, app_version: Optional[str], style: str) -> List[dict]:
    """whats_new_plus: banners (all live), tips (not yet seen) or the update
    notes for exactly the installed version (once)."""
    if not await _plus(db):
        return []
    uid = ObjectId(me)
    user = await db.users.find_one({"_id": uid}, {"user_type": 1, "staff_role": 1, "is_active": 1,
                                                   "date_of_birth": 1, "videos_count": 1, "localization": 1})
    rows = [a for a in await _live(db, app_version, _now()) if a.get("style") == style]
    if style == "update_notes":
        rows = [a for a in rows if a.get("version") and a.get("version") == (app_version or "").strip()]
    out = []
    for a in rows:
        if not await _reaches(db, a, user, True):
            continue
        if style in ("tip", "update_notes") and await db.announce_seen.find_one({"_id": f"{a['_id']}:{uid}"}, {"_id": 1}):
            continue
        out.append(public(a, app_version))
    return out[:20]


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
    out = {"title": title, "body": text, "button": button, "action": action, "feature": feature,
           "min_app": min_app, "image": image, "priority": pr}
    out.update(_clean_plus(body, image, action))
    return out


def _clean_plus(body: dict, image, action) -> dict:
    """The whats_new_plus fields (saved whatever the switch; used only with it on)."""
    from app.services.messages import clean_text
    from app.services import team
    style = body.get("style") or "card"
    if style not in STYLES:
        raise HTTPException(400, "Unknown style")
    alt = clean_text(body.get("alt") or "")[:ALT_MAX]
    if image and style != "card" and not alt:
        raise HTTPException(400, "Describe the picture (for people using a screen reader)")
    pages = []
    if style == "story":
        raw = body.get("pages") or []
        if not isinstance(raw, list) or not (2 <= len(raw) <= PAGES_MAX):
            raise HTTPException(400, f"A story has 2 to {PAGES_MAX} pages")
        for p in raw:
            p = p if isinstance(p, dict) else {}
            pi = p.get("image") or None
            if pi is not None and not _cdn(pi):
                raise HTTPException(400, "Pictures must be on Wano's own storage")
            palt = clean_text(p.get("alt") or "")[:ALT_MAX]
            if pi and not palt:
                raise HTTPException(400, "Describe every picture (for screen readers)")
            pt, pb = clean_text(p.get("title") or "")[:TITLE_MAX], clean_text(p.get("body") or "")[:BODY_MAX]
            if not pt and not pb and not pi:
                raise HTTPException(400, "Every page needs something on it")
            pages.append({"image": pi, "alt": palt, "title": pt, "body": pb})
    if style == "poster" and not image:
        raise HTTPException(400, "A poster needs a picture")
    ends_at = body.get("ends_at")
    if isinstance(ends_at, str):
        try:
            ends_at = datetime.fromisoformat(ends_at.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            raise HTTPException(400, "End date like 2026-12-31T18:00")
    if ends_at is not None and not isinstance(ends_at, datetime):
        raise HTTPException(400, "End date like 2026-12-31T18:00")
    if style == "banner" and not ends_at:
        raise HTTPException(400, "A banner needs an end date")
    version = body.get("version") or None
    if style == "update_notes" and not version_tuple(version):
        raise HTTPException(400, "Update notes need the app version, like 2.3.60")
    tip_key = body.get("tip_key") or None
    if style == "tip" and not (isinstance(tip_key, str) and re.fullmatch(r"[a-z_]{2,40}", tip_key)):
        raise HTTPException(400, "A tip needs the feature it's about")
    target = body.get("target") or None
    if action == "challenge" and not (isinstance(target, str) and ObjectId.is_valid(target)):
        raise HTTPException(400, "Pick the challenge")
    aud = team.clean_audience(body.get("audience")) if body.get("audience") is not None else None
    if team.money_promise(f"{body.get('title', '')} {body.get('body', '')}"):
        raise HTTPException(400, "Cash payouts are locked: no announcements about earning money")
    return {"style": style, "alt": alt, "pages": pages, "ends_at": ends_at, "version": version,
            "tip_key": tip_key, "target": target if action == "challenge" else None, "audience": aud}


async def admin_list(db) -> List[dict]:
    rows = await db.announcements.find({}).sort("created_at", -1).to_list(100)
    out = []
    for a in rows:
        st = a.get("stats") or {}
        s = int(st.get("shown") or 0)
        out.append({**public(a, "9.9.9"), "active": a.get("active") is True, "feature": a.get("feature"),
                    "min_app": a.get("min_app"), "priority": int(a.get("priority") or 0),
                    "reach": a.get("reach", 100), "audience": a.get("audience"),
                    "not_now": int(st.get("not_now") or 0),
                    "shown": s, "tap": int(st.get("tap") or 0), "close": int(st.get("close") or 0),
                    "tap_rate": round((st.get("tap") or 0) / s, 3) if s else None})
    return out


def _next_reach(old, wanted):
    """Reach only goes up one step at a time, and only after the team saw it."""
    if wanted is None:
        return old
    if wanted not in REACH_STEPS:
        raise HTTPException(400, "Reach is team, 1, 10 or 100 (percent)")
    oi, wi = REACH_STEPS.index(old if old in REACH_STEPS else 100), REACH_STEPS.index(wanted)
    if wi > oi + 1:
        raise HTTPException(409, f"Go one step at a time: next is {REACH_STEPS[oi + 1]}")
    return wanted


async def admin_save(db, admin: str, body: dict, aid: Optional[str] = None) -> dict:
    doc = clean(body)
    active = body.get("active") is True
    now = _now()
    plus = await _plus(db)
    if aid:
        if not ObjectId.is_valid(aid):
            raise HTTPException(status_code=404, detail="Not found")
        old = await db.announcements.find_one({"_id": ObjectId(aid)})
        if not old:
            raise HTTPException(status_code=404, detail="Not found")
        if int((old.get("stats") or {}).get("shown") or 0) > 0 and (
                doc.get("image") != old.get("image") or doc.get("pages") != (old.get("pages") or [])):
            raise HTTPException(409, "People have already seen it, so its pictures can't change. Make a new one.")
        reach = _next_reach(old.get("reach", 100), body.get("reach"))
        if reach != old.get("reach", 100) and reach != "team":
            if old.get("reach") == "team" and int((old.get("stats") or {}).get("shown") or 0) == 0:
                raise HTTPException(409, "Nobody on the team has seen it yet: check it on your phone first")
        upd = {**doc, "active": active, "updated_at": now, "reach": reach}
        if active and not old.get("active"):
            upd["starts_at"] = now           # goes out from the moment it's turned on
        await db.announcements.update_one({"_id": old["_id"]}, {"$set": upd})
        return {"id": aid}
    starts = body.get("starts_at")
    if isinstance(starts, str):
        try:
            starts = datetime.fromisoformat(starts.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            raise HTTPException(400, "Start like 2026-12-01T08:00")
    starts = starts if isinstance(starts, datetime) and starts > now else now
    res = await db.announcements.insert_one({**doc, "active": active, "starts_at": starts, "created_at": now,
                                             "by": admin, "stats": {}, "reach": "team" if plus else 100})
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
