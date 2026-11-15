"""Places: towns and neighbourhoods people can search and pick as "your area"
(behind the "places" switch, off by default).

Why: Home Ground has a short, hand-made list of cities (364), so a local flyer
can only aim at a whole city. This adds a much bigger list that is searched,
never downloaded whole: you type "Ntin" and get Ntinda, Kampala.

- The list ships with the server (app/data/places.jsonl, one place a line, built by
  scripts/build_places.py from OpenStreetMap and GeoNames; both need credit:
  "© OpenStreetMap contributors", "GeoNames"). Each place has a point on the
  map, a kind (city, town, area) and, where we know it, the Home Ground city
  it belongs to (Ntinda belongs to Kampala).
- Admins can hide a place or fix its name (place_edits); nothing else writes it.
- A person's area is what they pick (users.area = {id, at}). It is never read
  from the phone's location. Under-18s can pick one too, but flyers never
  reach them anyway, and an area is never shown on a profile.
- The old /home-ground/cities list, Home Ground battles and the cities people
  already picked are untouched: an area is only used for flyers.
- Flyers can aim at a circle (a place + 0.5 to 10 km). Reach estimates are
  rounded and anything under 20 says "fewer than 20", so a small circle can't
  point at one household.
"""

import json
import math
import os
import time
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import HTTPException

KINDS = ("city", "town", "area")
KIND_RANK = {"city": 0, "town": 1, "area": 2}
SEARCH_MAX = 20
CIRCLE_KM = (0.5, 1.0, 3.0, 10.0)
FEWER_THAN = 20
AREA_CHANGES_PER_DAY = 5
EDITS_CACHE_SECONDS = 60
DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "places.jsonl")

# id -> {"id", "name", "kind", "country", "lat", "lng", "city", "pop"}
_PLACES: Dict[str, dict] = {}
_WORDS: Dict[str, List[str]] = {}      # first 2 letters of a folded word -> place ids
_CITY_PLACE: Dict[str, List[str]] = {}  # Home Ground city id -> the place that is that city
_loaded = [False]
_edits = {"at": 0.0, "db": None, "rows": {}}


def _fold(s: str) -> str:
    from app.services.home_ground import _fold as f
    return f(s)


def load(rows: Optional[List[list]] = None) -> int:
    """Load the list (from the data file, or rows given by tests)."""
    if rows is None:
        rows = []
        try:
            with open(DATA_FILE, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("["):
                        rows.append(json.loads(line))
        except FileNotFoundError:
            rows = []
    _PLACES.clear()
    _WORDS.clear()
    _CITY_PLACE.clear()
    for r in rows:
        pid, name, kind, cc, lat, lng, city, pop = (list(r) + [None] * 8)[:8]
        if not (isinstance(pid, str) and isinstance(name, str) and kind in KINDS and isinstance(cc, str)):
            continue
        p = {"id": pid, "name": name, "kind": kind, "country": cc.lower(), "lat": float(lat), "lng": float(lng),
             "city": city if isinstance(city, str) else None, "pop": int(pop or 0)}
        _PLACES[pid] = p
        for w in set(_fold(name).split()):
            _WORDS.setdefault(w[:2], []).append(pid)
        if p["city"] and p["kind"] in ("city", "town"):
            _CITY_PLACE.setdefault(p["city"], []).append(pid)
    _loaded[0] = True
    return len(_PLACES)


def _ensure():
    if not _loaded[0]:
        load()


async def _load_edits(db) -> Dict[str, dict]:
    if _edits["db"] is db and time.monotonic() - _edits["at"] < EDITS_CACHE_SECONDS:
        return _edits["rows"]
    try:
        rows = {r["_id"]: r for r in await db.place_edits.find({}).to_list(5000)}
    except Exception:
        rows = {}
    _edits.update(at=time.monotonic(), db=db, rows=rows)
    return rows


def clear_cache():
    _edits.update(at=0.0, db=None, rows={})


def _view(p: dict, edits: Dict[str, dict]) -> Optional[dict]:
    e = edits.get(p["id"]) or {}
    if e.get("hidden") is True:
        return None
    return {**p, "name": e.get("name") or p["name"]}


def out(p: dict) -> dict:
    """What the app sees: the name and what it's in, never the coordinates."""
    from app.services import home_ground as hg
    city = hg.CITY.get(p.get("city") or "")
    inside = city["name"] if city and p["kind"] == "area" and city["name"] != p["name"] else None
    country = hg.COUNTRIES.get(p["country"], p["country"].upper())
    label = ", ".join(x for x in (p["name"], inside, country) if x)
    return {"id": p["id"], "name": p["name"], "kind": p["kind"], "country": p["country"], "in": inside,
            "label": label}


async def search(db, q: str, country: Optional[str] = None, include_hidden: bool = False) -> List[dict]:
    """Up to 20 places whose words start with what was typed. The person's own
    country first, then cities, towns, areas, bigger places first."""
    _ensure()
    words = _fold(q or "").split()
    if not words or len(" ".join(words)) < 2:
        return []
    edits = await _load_edits(db)
    first = words[0]
    found = []
    for pid in _WORDS.get(first[:2], []):
        p = _PLACES[pid]
        e = edits.get(pid) or {}
        name = e.get("name") or p["name"]
        have = _fold(name).split()
        if all(any(h.startswith(w) for h in have) for w in words):
            if e.get("hidden") is True and not include_hidden:
                continue
            found.append({**p, "name": name, "hidden": e.get("hidden") is True})
    # Nicknames Home Ground already knows ("kla" is Kampala).
    from app.services import home_ground as hg
    nick = hg.ALIASES.get(" ".join(words))
    if nick:
        for pid in _CITY_PLACE.get(nick, []):
            e = edits.get(pid) or {}
            if pid not in {p["id"] for p in found} and (include_hidden or e.get("hidden") is not True):
                found.append({**_PLACES[pid], "name": e.get("name") or _PLACES[pid]["name"],
                              "hidden": e.get("hidden") is True, "_nick": True})
    cc = (country or "").lower()
    found.sort(key=lambda p: (not p.get("_nick"), p["country"] != cc,
                              not _fold(p["name"]).startswith(" ".join(words)),
                              KIND_RANK[p["kind"]], -p["pop"], len(p["name"]), p["name"]))
    return [{k: v for k, v in p.items() if k != "_nick"} for p in found[:SEARCH_MAX]]


async def get(db, pid: str) -> Optional[dict]:
    _ensure()
    p = _PLACES.get(pid) if isinstance(pid, str) else None
    return _view(p, await _load_edits(db)) if p else None


def km_between(a: dict, b: dict) -> float:
    r = 6371.0
    la1, la2 = math.radians(a["lat"]), math.radians(b["lat"])
    dla, dlo = la2 - la1, math.radians(b["lng"] - a["lng"])
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def within(center: dict, km: float) -> List[str]:
    """Ids of every place inside the circle (in the same country)."""
    _ensure()
    return [pid for pid, p in _PLACES.items()
            if p["country"] == center["country"] and km_between(center, p) <= km]


def area_of(user: dict) -> Optional[str]:
    a = user.get("area") if isinstance(user.get("area"), dict) else {}
    pid = a.get("id")
    return pid if isinstance(pid, str) else None


def in_circle(user: dict, circle: Optional[dict]) -> bool:
    """A flyer's circle: the person's picked area lies inside it."""
    if not circle:
        return True
    _ensure()
    pid = area_of(user)
    here, center = _PLACES.get(pid or ""), _PLACES.get(circle.get("place_id") or "")
    if not here or not center:
        return False
    return km_between(center, here) <= float(circle.get("km") or 0)


def rounded(n: int) -> dict:
    """A count that can't single anyone out: under 20 says so, else to the nearest 10."""
    if n < FEWER_THAN:
        return {"people": None, "label": f"fewer than {FEWER_THAN}"}
    r = int(round(n / 10.0)) * 10
    return {"people": r, "label": f"about {r:,}"}


async def reach(db, place_id: str, km: float) -> dict:
    center = await get(db, place_id)
    if not center:
        raise HTTPException(status_code=404, detail="Unknown place")
    if km not in CIRCLE_KM:
        raise HTTPException(status_code=422, detail="Pick 0.5, 1, 3 or 10 km")
    from app.services import home_ground as hg
    ids = within(center, km)
    n = await db.users.count_documents({"area.id": {"$in": ids}, **hg.adult_filter()}) if ids else 0
    return {"place": out(center), "km": km, "places_inside": len(ids), **rounded(n)}


async def set_area(db, user_id, pid: Optional[str]) -> dict:
    """Pick (or clear, with None) your area. A few changes a day at most."""
    from bson import ObjectId
    uid = ObjectId(user_id)
    now = datetime.utcnow()
    u = await db.users.find_one({"_id": uid}, {"area": 1}) or {}
    a = u.get("area") if isinstance(u.get("area"), dict) else {}
    day = now.strftime("%Y%m%d")
    changes = int(a.get("changes") or 0) if a.get("day") == day else 0
    if changes >= AREA_CHANGES_PER_DAY:
        raise HTTPException(status_code=429, detail="You've changed your area a few times today. Try again tomorrow.")
    if pid is None:
        await db.users.update_one({"_id": uid}, {"$set": {"area": {"day": day, "changes": changes + 1, "at": now}}})
        return {"area": None}
    p = await get(db, pid)
    if not p:
        raise HTTPException(status_code=422, detail="Pick a place from the list")
    await db.users.update_one({"_id": uid}, {"$set": {"area": {"id": p["id"], "at": now, "day": day,
                                                               "changes": changes + 1}}})
    return {"area": out(p)}


async def my_area(db, user: dict) -> Optional[dict]:
    pid = area_of(user)
    p = await get(db, pid) if pid else None
    return out(p) if p else None


async def edit(db, admin: str, pid: str, hidden: Optional[bool], name: Optional[str]) -> dict:
    _ensure()
    from bson import ObjectId
    if pid not in _PLACES:
        raise HTTPException(status_code=404, detail="Unknown place")
    doc = {"updated_at": datetime.utcnow(), "updated_by": ObjectId(admin)}
    if hidden is not None:
        doc["hidden"] = bool(hidden)
    if name is not None:
        clean = " ".join(name.split())
        if not (1 <= len(clean) <= 60) or any(c in clean for c in "<>{}"):
            raise HTTPException(status_code=422, detail="Write a name of up to 60 letters")
        doc["name"] = clean
    await db.place_edits.update_one({"_id": pid}, {"$set": doc}, upsert=True)
    clear_cache()
    p = await get(db, pid)
    base = _PLACES[pid]
    return {**(out(p) if p else out(base)), "hidden": p is None, "original": base["name"]}


def stats() -> dict:
    _ensure()
    by: Dict[str, int] = {}
    for p in _PLACES.values():
        by[p["kind"]] = by.get(p["kind"], 0) + 1
    return {"places": len(_PLACES), "by_kind": by}
