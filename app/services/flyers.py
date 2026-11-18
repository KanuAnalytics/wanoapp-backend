"""Local flyers ("flyers" switch, off by default).

Businesses' flyers in the same small slot as the Wano TV ticket on Wano shorts,
always labelled Sponsored, with one button: WhatsApp, Call or Visit. The TV
ticket and flyers share one set of limits in the app (never in the first
posts, a gap between any two, a few per session), so the feed never feels
like ads.

Who sees a flyer (decided here, never by the app):
- signed in, with a country on the account (logged out: none at all);
- the flyer's countries include theirs. If the flyer names cities, their
  Home Ground city must be one of them; people without a city only ever get
  country-wide flyers. Someone in Nigeria never sees a Ugandan flyer;
- a flyer can also aim at a circle (a place + 0.5, 1, 3 or 10 km; see
  app/services/places.py): then only people whose picked area lies inside it;
- never under-18s (by saved birthday);
- alcohol ("drinks") only to people whose saved birthday shows 18 or older:
  no birthday on file, no alcohol flyer;
- running now (between its start and end), switched on, and under its views
  per day if it has one.
Testers (up to 10 usernames on the flyer) see it anywhere, ahead of Wano TV,
so a flyer for Kampala can be checked from Chicago. Nobody else is affected.
"Testers only" makes a flyer a preview: nobody but its testers sees it.
Never offered as categories: betting, loans, crypto, adult. Only admins make
flyers. Phase 2 (businesses paying by Mobile Money) is not built yet.

Numbers per flyer per day and per city: shown, tapped, closed, and people
reached (each person once a day), for the "your flyer was seen..." report.
"""

import asyncio
import io
import logging
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

CATEGORIES = {
    "food": "Food & restaurants", "drinks": "Drinks (alcohol)", "soft_drinks": "Soft drinks",
    "fashion": "Fashion", "beauty": "Beauty", "events": "Events & shows", "shops": "Shops",
    "services": "Services", "telecom": "Phones & data", "bank": "Banks & mobile money",
    "insurance": "Insurance", "education": "Education", "health": "Health", "transport": "Transport",
    "property": "Property", "music": "Music & entertainment", "other": "Other",
}
ADULTS_ONLY = {"drinks"}
# Never: betting, loans (instant-loan apps), crypto, adult. They simply aren't categories.
BUTTONS = ("whatsapp", "call", "link")
BUTTON_LABEL = {"whatsapp": "WhatsApp", "call": "Call", "link": "Visit"}
MAX_PER_VIEWER = 3                 # flyers sent to the app at once
MAX_TESTERS = 10
EVENT_KINDS = ("shown", "tap", "close")
EVENTS_PER_DAY = 60                # per person
IMAGE_MAX_BYTES = 5_000_000
IMAGE_WIDTHS = (320, 720)
FOLDER = "flyers"
CACHE_SECONDS = 60

_PHONE = re.compile(r"^\+[1-9]\d{7,14}$")
_cache: dict = {"at": 0.0, "db": None, "rows": []}


def _now() -> datetime:
    return datetime.utcnow()


def _oid(x, what="Flyer not found") -> ObjectId:
    if not isinstance(x, str) or not ObjectId.is_valid(x):
        raise HTTPException(status_code=404, detail=what)
    return ObjectId(x)


def _https(u) -> Optional[str]:
    return u if isinstance(u, str) and u.startswith("https://") and len(u) <= 2048 else None


def known_adult(user: dict, now: Optional[datetime] = None) -> bool:
    """A saved, believable birthday that makes them 18 or older."""
    from app.services import home_ground as hg
    years = hg.age_of(user.get("date_of_birth"), now)
    return years is not None and years >= max(18, hg.IMPLAUSIBLE_AGE)


# ------------------------------------------------------------------ admin

def _clean_line(v, n: int) -> str:
    from app.services.messages import clean_text
    return clean_text(v)[:n] if isinstance(v, str) else ""


def button_url(kind: str, value: str) -> str:
    if kind == "whatsapp":
        return f"https://wa.me/{value.lstrip('+')}?text=" + "Hi%2C%20I%20saw%20your%20flyer%20on%20Wano"
    if kind == "call":
        return f"tel:{value}"
    return value


def _check_button(kind, value) -> str:
    if kind not in BUTTONS:
        raise HTTPException(status_code=422, detail="The button is WhatsApp, Call or Visit")
    v = (value or "").strip() if isinstance(value, str) else ""
    if kind in ("whatsapp", "call"):
        v = re.sub(r"[\s\-()]", "", v)
        if not _PHONE.match(v):
            raise HTTPException(status_code=422, detail="Write the number with its country code, like +256772123456")
        return v
    if not v.startswith("https://") or len(v) > 500 or " " in v:
        raise HTTPException(status_code=422, detail="The link must start with https://")
    return v


async def _handle(db, n):
    projection = {"_id": 1, "username": 1}
    u = await db.users.find_one({"username": n}, projection)
    if not u:
        matches = await db.users.find(
            {"username": {"$regex": rf"^{re.escape(n)}$", "$options": "i"}}, projection
        ).limit(2).to_list(length=2)
        if len(matches) > 1:
            raise HTTPException(status_code=422, detail=f"Use the exact capitalization for @{n[:30]}")
        u = next(iter(matches), None)
    if not u:
        raise HTTPException(status_code=422, detail=f"No account called @{n[:30]}")
    return u


def _utc(value):
    t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t


async def validate(db, body: dict) -> dict:
    from app.services import home_ground as hg
    await hg.load_custom(db)
    business = _clean_line(body.get("business"), 40)
    line = _clean_line(body.get("line"), 80)
    if not business:
        raise HTTPException(status_code=422, detail="Write the business name")
    category = body.get("category")
    if category not in CATEGORIES:
        raise HTTPException(status_code=422, detail="Pick a category")
    countries = sorted({c.lower() for c in (body.get("countries") or []) if isinstance(c, str)})
    if not countries or any(c not in hg.COUNTRIES for c in countries) or len(countries) > 20:
        raise HTTPException(status_code=422, detail="Pick at least one country")
    cities = sorted({c for c in (body.get("cities") or []) if isinstance(c, str)})
    for c in cities:
        city = hg.CITY.get(c)
        if not city:
            raise HTTPException(status_code=422, detail=f"Unknown city: {c[:40]}")
        if city["country"] not in countries:
            raise HTTPException(status_code=422, detail=f"{city['name']} isn't in the countries picked")
    if len(cities) > 50:
        raise HTTPException(status_code=422, detail="At most 50 cities")
    image = _https(body.get("image"))
    if not image:
        raise HTTPException(status_code=422, detail="Upload the flyer picture first")
    kind = body.get("button")
    value = _check_button(kind, body.get("button_value"))
    try:
        starts = _utc(body.get("starts_at"))
        ends = _utc(body.get("ends_at"))
    except ValueError:
        raise HTTPException(status_code=422, detail="Pick the start and end dates")
    if ends <= starts or ends - starts > timedelta(days=366):
        raise HTTPException(status_code=422, detail="The end must be after the start (at most a year)")
    names = {n.strip().lstrip("@") for n in (body.get("testers") or []) if isinstance(n, str)}
    names.discard("")
    if len({name.lower() for name in names}) > MAX_TESTERS:
        raise HTTPException(status_code=422, detail=f"At most {MAX_TESTERS} testers")
    testers = {}
    for n in sorted(names):
        u = await _handle(db, n)
        testers[u["_id"]] = u["username"]
    if len(testers) > MAX_TESTERS:
        raise HTTPException(status_code=422, detail=f"At most {MAX_TESTERS} testers")
    names = list(testers.values())
    tester_ids = list(testers)
    if body.get("testers_only") is True and not tester_ids:
        raise HTTPException(status_code=422, detail="Add at least one tester for a testers-only flyer")
    circle = None
    c = body.get("circle")
    if c:
        from app.services import places
        center = await places.get(db, c.get("place_id") if isinstance(c, dict) else None)
        km = c.get("km") if isinstance(c, dict) else None
        if not center:
            raise HTTPException(status_code=422, detail="Pick the circle's centre from the list")
        if isinstance(km, bool) or km not in places.CIRCLE_KM:
            raise HTTPException(status_code=422, detail="The circle is 0.5, 1, 3 or 10 km")
        if center["country"] not in countries:
            raise HTTPException(status_code=422, detail=f"{center['name']} isn't in the countries picked")
        circle = {"place_id": center["id"], "km": float(km)}
    cap = body.get("daily_cap")
    if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or not (1 <= cap <= 10_000_000)):
        raise HTTPException(status_code=422, detail="Views per day must be a number, or empty for no limit")
    return {"business": business, "line": line, "category": category, "countries": countries, "cities": cities,
            "image": image, "image_small": _https(body.get("image_small")), "button": kind, "button_value": value,
            "starts_at": starts, "ends_at": ends, "daily_cap": cap, "active": body.get("active") is not False,
            "testers": names, "tester_ids": tester_ids, "testers_only": body.get("testers_only") is True,
            "circle": circle}


async def create(db, admin: str, body: dict) -> dict:
    doc = await validate(db, body)
    doc.update({"created_by": ObjectId(admin), "created_at": _now(), "updated_at": _now()})
    res = await db.flyers.insert_one(doc)
    _cache["at"] = 0.0
    return _admin_out({**doc, "_id": res.inserted_id})


async def update(db, admin: str, fid: str, body: dict) -> dict:
    oid = _oid(fid)
    if not await db.flyers.find_one({"_id": oid}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="Flyer not found")
    doc = await validate(db, body)
    doc.update({"updated_at": _now(), "updated_by": ObjectId(admin)})
    await db.flyers.update_one({"_id": oid}, {"$set": doc})
    _cache["at"] = 0.0
    return _admin_out(await db.flyers.find_one({"_id": oid}))


async def set_active(db, fid: str, active: bool) -> dict:
    r = await db.flyers.find_one_and_update({"_id": _oid(fid)}, {"$set": {"active": bool(active), "updated_at": _now()}},
                                            return_document=ReturnDocument.AFTER)
    if not r:
        raise HTTPException(status_code=404, detail="Flyer not found")
    _cache["at"] = 0.0
    return _admin_out(r)


def _admin_out(d: dict) -> dict:
    return {"id": str(d["_id"]), "business": d.get("business"), "line": d.get("line"), "category": d.get("category"),
            "countries": d.get("countries") or [], "cities": d.get("cities") or [], "image": d.get("image"),
            "image_small": d.get("image_small"), "button": d.get("button"), "button_value": d.get("button_value"),
            "starts_at": d.get("starts_at"), "ends_at": d.get("ends_at"), "daily_cap": d.get("daily_cap"),
            "active": d.get("active") is True, "testers": d.get("testers") or [],
            "testers_only": d.get("testers_only") is True, "circle": _circle_out(d.get("circle"))}


def _circle_out(c) -> Optional[dict]:
    if not isinstance(c, dict) or not c.get("place_id"):
        return None
    from app.services import places
    places._ensure()
    p = places._PLACES.get(c["place_id"])
    return {"place_id": c["place_id"], "km": c.get("km"), "label": places.out(p)["label"] if p else c["place_id"]}


def _put(key: str, data: bytes) -> None:
    from app.services import small_images
    small_images._put(key, data)


async def store_image(data: bytes) -> dict:
    """A picture admins upload: re-made as WebP (which also drops anything
    hidden in the file), a feed-size copy and a small one."""
    from app.services import small_images
    if not data or len(data) > IMAGE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="The picture must be under 5 MB")
    try:
        variants = await asyncio.to_thread(small_images.make_variants, data, IMAGE_WIDTHS)
    except Exception:
        raise HTTPException(status_code=400, detail="That isn't a picture we can use (JPG, PNG or WebP)")
    from app.core.config import settings
    base = f"{FOLDER}/{uuid.uuid4().hex}"
    urls = {}
    try:
        for w, blob in variants:
            key = f"{base}-w{w}.webp"
            await asyncio.to_thread(_put, key, blob)
            urls[w] = f"{settings.DO_SPACES_CDN_URL}/{settings.DO_SPACES_BUCKET_NAME}/{key}"
    except Exception:
        logger.exception("flyer image upload failed")
        raise HTTPException(status_code=502, detail="Couldn't save the picture. Try again.")
    return {"image": urls[max(IMAGE_WIDTHS)], "image_small": urls[min(IMAGE_WIDTHS)]}


async def admin_list(db, days: int = 30) -> List[dict]:
    rows = await db.flyers.find({}).sort("created_at", -1).to_list(500)
    since = _day(_now() - timedelta(days=days - 1))
    stats = {}
    async for r in db.flyer_daily.aggregate([
            {"$match": {"day": {"$gte": since}}},
            {"$group": {"_id": "$flyer_id", "shown": {"$sum": "$shown"}, "tap": {"$sum": "$tap"},
                        "close": {"$sum": "$close"}, "reach": {"$sum": "$reach"}}}]):
        stats[r["_id"]] = r
    out = []
    for d in rows:
        s = stats.get(d["_id"], {})
        shown = int(s.get("shown") or 0)
        out.append({**_admin_out(d), "stats": {"days": days, "shown": shown, "tap": int(s.get("tap") or 0),
                                               "close": int(s.get("close") or 0), "reach": int(s.get("reach") or 0),
                                               "tap_rate": round((s.get("tap") or 0) / shown, 3) if shown else None}})
    return out


async def report(db, fid: str, days: int = 30) -> dict:
    """Per day and per city, for the business."""
    oid = _oid(fid)
    d = await db.flyers.find_one({"_id": oid})
    if not d:
        raise HTTPException(status_code=404, detail="Flyer not found")
    since = _day(_now() - timedelta(days=days - 1))
    rows = await db.flyer_daily.find({"flyer_id": oid, "day": {"$gte": since}}).to_list(5000)
    by_day: dict = {}
    by_place: dict = {}
    for r in rows:
        k = r["day"].strftime("%Y-%m-%d")
        place = r.get("city") or r.get("country") or "?"
        for bucket, key in ((by_day, k), (by_place, place)):
            b = bucket.setdefault(key, {"shown": 0, "tap": 0, "close": 0, "reach": 0})
            for f in ("shown", "tap", "close", "reach"):
                b[f] += int(r.get(f) or 0)
    preview = {"shown": 0, "tap": 0, "close": 0}
    async for row in db.flyer_preview_daily.find({"flyer_id": oid, "day": {"$gte": since}}):
        for kind in preview:
            preview[kind] += row.get(kind, 0)
    unique = await db.flyer_audience.count_documents({"flyer_id": oid, "last_seen": {"$gte": since}})
    return {"unique_viewers_tracked": unique, "preview": preview, "flyer": _admin_out(d), "days": [{"day": k, **v} for k, v in sorted(by_day.items())],
            "places": sorted([{"place": k, **v} for k, v in by_place.items()], key=lambda x: -x["shown"])}


# ------------------------------------------------------------------ viewers

def _day(t: datetime) -> datetime:
    return datetime(t.year, t.month, t.day)


async def _running(db, now: datetime) -> List[dict]:
    import time
    if _cache["db"] is db and time.monotonic() - _cache["at"] < CACHE_SECONDS:
        rows = _cache["rows"]
    else:
        rows = await db.flyers.find({"active": True, "ends_at": {"$gt": now}}).to_list(1000)
        _cache.update(at=time.monotonic(), db=db, rows=rows)
    return [r for r in rows if r.get("starts_at") and r["starts_at"] <= now < r["ends_at"]]


def is_tester(flyer: dict, viewer: dict) -> bool:
    return viewer.get("_id") is not None and viewer["_id"] in (flyer.get("tester_ids") or [])


def exclusion(flyer, viewer, now=None):
    from app.services import home_ground as hg, places
    if viewer.get("is_active") is False:
        return "Account is inactive."
    if hg.is_minor(viewer, now):
        return "Account does not meet the minimum age."
    if flyer.get("category") in ADULTS_ONLY and not known_adult(viewer, now):
        return "This category requires a confirmed adult birthday."
    if is_tester(flyer, viewer):
        return None
    if flyer.get("testers_only") is True:
        return "This preview is limited to named testers."
    if hg.country_of(viewer) not in (flyer.get("countries") or []):
        return "Account country is missing or outside the target countries."
    if flyer.get("cities") and hg.city_of(viewer) not in flyer["cities"]:
        return "Account city is outside the target cities."
    if flyer.get("circle") and not places.in_circle(viewer, flyer["circle"]):
        return "Account area is outside the target radius."
    return None


def matches(flyer, viewer, now=None):
    return exclusion(flyer, viewer, now) is None


async def delivery_check(db, fid, account):
    from app.services import creator_push, home_ground as hg
    from urllib.parse import urlsplit
    campaign = await db.flyers.find_one({"_id": _oid(fid)})
    if not campaign:
        raise HTTPException(status_code=404, detail="Flyer not found")
    account = account.strip()
    if "://" in account:
        try:
            url = urlsplit(account)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid profile link")
        match = re.fullmatch(r"/u/([0-9a-fA-F]{24})/?", url.path)
        if url.scheme != "https" or url.hostname not in {"devbe.wanoafrica.com", "wanoafrica.com", "www.wanoafrica.com"} or not match:
            raise HTTPException(status_code=422, detail="Enter a Wano profile link or username")
        uid = ObjectId(match[1])
    else:
        uid = (await _handle(db, account.lstrip("@")))["_id"]
    viewer = await db.users.find_one({"_id": uid}, {"username": 1, "localization": 1, "home_ground": 1,
                                                   "area": 1, "date_of_birth": 1, "is_active": 1})
    if not viewer:
        raise HTTPException(status_code=404, detail="Account not found")
    now = _now()
    await hg.load_custom(db)
    if not await creator_push.on(db, "flyers"):
        reason = "Local flyers switch is off."
    elif not campaign.get("active"):
        reason = "Campaign is paused."
    elif not campaign["starts_at"] <= now < campaign["ends_at"]:
        reason = "Outside the campaign schedule."
    else:
        reason = exclusion(campaign, viewer, now)
        if not reason and not is_tester(campaign, viewer) and not await _under_cap(db, campaign, now):
            reason = "Daily impression cap reached."
    if not reason:
        selection = await for_viewer(db, str(uid), now)
        picked = any(x["id"] == fid for x in selection["flyers"])
        reason = "Selected in this check." if picked else "Eligible, but not selected in this check."
    evidence = await db.flyer_audience.find_one({"_id": f"{fid}:{uid}"}, {"last_seen": 1})
    last_seen = evidence["last_seen"].replace(tzinfo=timezone.utc).isoformat() if evidence else None
    return {"result": reason, "country": hg.country_of(viewer), "city": hg.city_of(viewer),
            "last_public_impression": last_seen}


async def _under_cap(db, flyer: dict, now: datetime) -> bool:
    cap = flyer.get("daily_cap")
    if not cap:
        return True
    count = await db.flyer_counts.find_one({"_id": f"{now:%Y%m%d}:{flyer['_id']}"})
    if count is not None:
        return count["n"] < cap
    total = 0
    async for r in db.flyer_daily.find({"flyer_id": flyer["_id"], "day": _day(now)}, {"shown": 1}):
        total += int(r.get("shown") or 0)
    return total < cap


def viewer_out(f: dict, test: bool = False) -> dict:
    out = viewer_out_base(f)
    if test:
        out["test"] = True
    return out


def viewer_out_base(f: dict) -> dict:
    return {"id": str(f["_id"]), "business": f.get("business") or "", "line": f.get("line") or "",
            "image": f.get("image"), "image_small": f.get("image_small"),
            "button": {"kind": f.get("button"), "label": BUTTON_LABEL.get(f.get("button"), "Open"),
                       "url": button_url(f.get("button"), f.get("button_value") or "")},
            "label": "Sponsored"}


async def for_viewer(db, viewer_id: Optional[str], now: Optional[datetime] = None) -> dict:
    from app.services import home_ground as hg, tv_strip
    now = now or _now()
    rules = dict(tv_strip.RULES)
    if not viewer_id or not ObjectId.is_valid(viewer_id):
        return {"flyers": [], "rules": rules}
    await hg.load_custom(db)
    viewer = await db.users.find_one({"_id": ObjectId(viewer_id)},
                                     {"localization": 1, "home_ground": 1, "area": 1, "date_of_birth": 1,
                                      "is_active": 1})
    if not viewer or viewer.get("is_active") is False:
        return {"flyers": [], "rules": rules}
    picks = [f for f in await _running(db, now) if matches(f, viewer, now)]
    random.shuffle(picks)
    picks.sort(key=lambda f: not is_tester(f, viewer))      # a tester's flyers first
    out = []
    for f in picks:
        if is_tester(f, viewer) or await _under_cap(db, f, now):
            out.append(viewer_out(f, test=is_tester(f, viewer)))
        if len(out) >= MAX_PER_VIEWER:
            break
    return {"flyers": out, "rules": rules}


async def record(db, viewer_id: str, fid: str, kind: str, now: Optional[datetime] = None) -> bool:
    """Count one event. False when ignored (unknown flyer, not theirs to see, over the cap)."""
    from app.services import home_ground as hg
    now = now or _now()
    if kind not in EVENT_KINDS or not ObjectId.is_valid(fid) or not ObjectId.is_valid(viewer_id):
        return False
    f = await db.flyers.find_one({"_id": ObjectId(fid)})
    viewer = await db.users.find_one({"_id": ObjectId(viewer_id)}, {"localization": 1, "home_ground": 1,
                                                                    "area": 1, "date_of_birth": 1, "is_active": 1})
    if not f or not f.get("active") or not f["starts_at"] <= now < f["ends_at"] or not viewer or not matches(f, viewer, now):
        return False
    day = _day(now)
    lim = await db.flyer_limits.find_one_and_update(
        {"_id": f"{viewer_id}:{day:%Y%m%d}"}, {"$inc": {"n": 1}, "$setOnInsert": {"at": now}}, upsert=True,
        return_document=ReturnDocument.AFTER)
    if (lim or {}).get("n", 0) > EVENTS_PER_DAY:
        return False
    if is_tester(f, viewer):
        await db.flyer_preview_daily.update_one(
            {"_id": f"{day:%Y%m%d}:{fid}"}, {"$inc": {kind: 1}, "$setOnInsert": {"day": day, "flyer_id": f["_id"]}},
            upsert=True)
        return True
    if kind == "shown":
        key = f"{day:%Y%m%d}:{fid}"
        if not await db.flyer_counts.find_one({"_id": key}, {"_id": 1}):
            previous = sum([int(r.get("shown", 0)) async for r in db.flyer_daily.find(
                {"flyer_id": f["_id"], "day": day}, {"shown": 1})])
            await db.flyer_counts.update_one({"_id": key}, {"$setOnInsert": {"n": previous, "day": day}}, upsert=True)
        condition = {"_id": key}
        if f.get("daily_cap"):
            condition["n"] = {"$lt": f["daily_cap"]}
        if not (await db.flyer_counts.update_one(condition, {"$inc": {"n": 1}})).modified_count:
            return False
    country = hg.country_of(viewer)
    city = hg.city_of(viewer) or ""
    inc = {kind: 1}
    if kind == "shown":
        try:
            await db.flyer_reach.insert_one({"_id": f"{day:%Y%m%d}:{fid}:{viewer_id}", "at": now})
            inc["reach"] = 1
        except DuplicateKeyError:
            pass
    if kind == "shown":
        await db.flyer_audience.update_one(
            {"_id": f"{fid}:{viewer_id}"}, {"$setOnInsert": {"flyer_id": f["_id"]}, "$max": {"last_seen": now}},
            upsert=True)
    await db.flyer_daily.update_one(
        {"_id": f"{day:%Y%m%d}:{fid}:{country}:{city}"},
        {"$inc": inc, "$setOnInsert": {"day": day, "flyer_id": f["_id"], "country": country, "city": city or None}},
        upsert=True)
    return True
