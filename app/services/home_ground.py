"""Home Ground (behind the "home_ground" switch).

Every user represents a country (localization.country, which every account
has) and, once they pick one, a city from CITIES (users.home_ground.city).
The city is picked from this fixed list, never typed, so "Kampala", "Kla" and
"kampala city" can't split one city into three.

Battles: each week, a country's (or city's) score is the sum of the weekly
leaderboard scores (leaderboard.ranking: views once per viewer, own views
never count) of the creators who represent it. Viewers push their place up
by watching and liking its creators. A city takes part once MIN_CITY_CREATORS
of its creators are on the week's board; until then it only counts toward
its country.

Under-18s: their work counts for their city and country, but they are only
ever shown by country (never on a city board, never as a city's leader, and
their profile says the country).

Crowns: when a week has settled, the top creator of each country and of each
battling city gets a badge (crown_country / crown_city), once.

A city can be changed once every CHANGE_DAYS, except within FIX_MINUTES of
picking it (typo fix)."""

import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from bson import ObjectId

MIN_CITY_CREATORS = 3
CHANGE_DAYS = 30
FIX_MINUTES = 60
CACHE_SECONDS = 300

COUNTRIES = {
    "ug": "Uganda", "ke": "Kenya", "tz": "Tanzania", "rw": "Rwanda", "bi": "Burundi", "ss": "South Sudan",
    "cd": "DR Congo", "cg": "Congo", "cm": "Cameroon", "ng": "Nigeria", "gh": "Ghana", "ci": "Côte d'Ivoire",
    "sn": "Senegal", "za": "South Africa", "et": "Ethiopia", "zm": "Zambia", "zw": "Zimbabwe",
    "ao": "Angola", "mz": "Mozambique", "mw": "Malawi", "so": "Somalia", "sd": "Sudan", "ml": "Mali",
    "bf": "Burkina Faso", "bj": "Benin", "tg": "Togo", "ga": "Gabon", "ma": "Morocco", "eg": "Egypt",
}

# (id, name, country)
CITIES = [
    ("kampala", "Kampala", "ug"), ("gulu", "Gulu", "ug"), ("mbarara", "Mbarara", "ug"), ("jinja", "Jinja", "ug"),
    ("entebbe", "Entebbe", "ug"), ("mbale", "Mbale", "ug"), ("lira", "Lira", "ug"), ("arua", "Arua", "ug"),
    ("masaka", "Masaka", "ug"), ("fort_portal", "Fort Portal", "ug"), ("soroti", "Soroti", "ug"),
    ("nairobi", "Nairobi", "ke"), ("mombasa", "Mombasa", "ke"), ("kisumu", "Kisumu", "ke"),
    ("nakuru", "Nakuru", "ke"), ("eldoret", "Eldoret", "ke"),
    ("dar_es_salaam", "Dar es Salaam", "tz"), ("dodoma", "Dodoma", "tz"), ("arusha", "Arusha", "tz"),
    ("mwanza", "Mwanza", "tz"), ("zanzibar", "Zanzibar", "tz"),
    ("kigali", "Kigali", "rw"), ("bujumbura", "Bujumbura", "bi"), ("juba", "Juba", "ss"),
    ("kinshasa", "Kinshasa", "cd"), ("lubumbashi", "Lubumbashi", "cd"), ("goma", "Goma", "cd"),
    ("bukavu", "Bukavu", "cd"), ("kisangani", "Kisangani", "cd"), ("mbuji_mayi", "Mbuji-Mayi", "cd"),
    ("brazzaville", "Brazzaville", "cg"), ("pointe_noire", "Pointe-Noire", "cg"),
    ("douala", "Douala", "cm"), ("yaounde", "Yaoundé", "cm"),
    ("lagos", "Lagos", "ng"), ("abuja", "Abuja", "ng"), ("port_harcourt", "Port Harcourt", "ng"),
    ("ibadan", "Ibadan", "ng"), ("kano", "Kano", "ng"),
    ("accra", "Accra", "gh"), ("kumasi", "Kumasi", "gh"), ("abidjan", "Abidjan", "ci"), ("dakar", "Dakar", "sn"),
    ("johannesburg", "Johannesburg", "za"), ("cape_town", "Cape Town", "za"), ("durban", "Durban", "za"),
    ("addis_ababa", "Addis Ababa", "et"), ("lusaka", "Lusaka", "zm"), ("harare", "Harare", "zw"),
    ("luanda", "Luanda", "ao"), ("maputo", "Maputo", "mz"), ("lilongwe", "Lilongwe", "mw"),
]
CITY = {cid: {"id": cid, "name": name, "country": cc} for cid, name, cc in CITIES}
# Extra spellings people typed into the old free-text city setting.
ALIASES = {"kla": "kampala", "kampala city": "kampala", "nbo": "nairobi", "dar": "dar_es_salaam",
           "dsm": "dar_es_salaam", "kin": "kinshasa", "yaounde": "yaounde", "jozi": "johannesburg",
           "joburg": "johannesburg", "addis": "addis_ababa", "pointe noire": "pointe_noire",
           "mbuji mayi": "mbuji_mayi", "fortportal": "fort_portal"}

_cache: Dict[tuple, tuple] = {}


def clear_cache():
    _cache.clear()


def _fold(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).casefold().replace("-", " ").split())


_BY_NAME = {_fold(c["name"]): cid for cid, c in CITY.items()}


def city_from_text(text) -> Optional[str]:
    if not isinstance(text, str):
        return None
    f = _fold(text)
    return _BY_NAME.get(f) or ALIASES.get(f) or (f.replace(" ", "_") if f.replace(" ", "_") in CITY else None)


def country_of(user: dict) -> Optional[str]:
    loc = user.get("localization") if isinstance(user.get("localization"), dict) else {}
    c = loc.get("country")
    return c.lower() if isinstance(c, str) and len(c) == 2 and c.isalpha() else None


def city_of(user: dict) -> Optional[str]:
    hg = user.get("home_ground") if isinstance(user.get("home_ground"), dict) else {}
    c = hg.get("city")
    return c if isinstance(c, str) and c in CITY else None


def is_minor(user: dict, now: Optional[datetime] = None) -> bool:
    dob = user.get("date_of_birth")
    if not isinstance(dob, datetime):
        return False  # unknown: treated as an adult, as everywhere else
    now = now or datetime.utcnow()
    dob = dob.replace(tzinfo=None)
    years = now.year - dob.year - ((now.month, now.day) < (dob.month, dob.day))
    return years < 18


def place_label(user: dict) -> Optional[dict]:
    """What a profile shows: the city for adults, the country for under-18s."""
    cc = country_of(user)
    city = city_of(user)
    if not city and not cc:
        return None
    if city and not is_minor(user):
        return {"label": CITY[city]["name"], "country": CITY[city]["country"]}
    cc = cc or (CITY[city]["country"] if city else None)
    return {"label": COUNTRIES.get(cc, cc.upper()), "country": cc} if cc else None


def cities_grouped() -> List[dict]:
    out: Dict[str, list] = {}
    for cid, name, cc in CITIES:
        out.setdefault(cc, []).append({"id": cid, "name": name})
    return [{"country": cc, "name": COUNTRIES.get(cc, cc.upper()), "cities": cs} for cc, cs in out.items()]


async def _week(db, start: datetime) -> dict:
    """The week's board joined with who represents where (cached)."""
    key = ("week", start)
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    from app.services import leaderboard
    rows = await leaderboard.ranking(db, start)
    ids = [ObjectId(r["user"]["id"]) for r in rows if ObjectId.is_valid(r["user"]["id"])]
    users = {u["_id"]: u async for u in db.users.find(
        {"_id": {"$in": ids}, "is_active": True},
        {"localization": 1, "home_ground": 1, "date_of_birth": 1})}
    enriched = []
    for r in rows:
        u = users.get(ObjectId(r["user"]["id"])) if ObjectId.is_valid(r["user"]["id"]) else None
        if not u:
            continue
        enriched.append({**r, "country": country_of(u), "city": city_of(u), "minor": is_minor(u)})
    data = {"rows": enriched}
    _cache[key] = (time.monotonic() + CACHE_SECONDS, data)
    return data


def _rerank(rows: List[dict]) -> List[dict]:
    out = []
    for i, r in enumerate(rows, 1):
        out.append({"rank": i, "user": r["user"], "score": r["score"], "views": r["views"], "likes": r["likes"],
                    "posts": r["posts"]})
    return out


def board_rows(week: dict, city: Optional[str] = None, country: Optional[str] = None) -> List[dict]:
    if city:
        rows = [r for r in week["rows"] if r["city"] == city and not r["minor"]]
    else:
        rows = [r for r in week["rows"] if r["country"] == country]
    return _rerank(rows)


def battle_rows(week: dict, level: str) -> List[dict]:
    groups: Dict[str, dict] = {}
    for r in week["rows"]:
        place = r["city"] if level == "city" else r["country"]
        if not place:
            continue
        g = groups.setdefault(place, {"score": 0, "creators": 0, "leader": None})
        g["score"] += r["score"]
        g["creators"] += 1
        if g["leader"] is None and not (level == "city" and r["minor"]):
            g["leader"] = r["user"]   # rows come best first
    out = []
    for place, g in groups.items():
        if level == "city" and g["creators"] < MIN_CITY_CREATORS:
            continue
        if level == "city":
            name, cc = CITY[place]["name"], CITY[place]["country"]
        else:
            name, cc = COUNTRIES.get(place, place.upper()), place
        out.append({"id": place, "name": name, "country": cc, "score": g["score"], "creators": g["creators"],
                    "leader": g["leader"]})
    out.sort(key=lambda x: (-x["score"], -x["creators"], x["id"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out


def standing(rows: List[dict], place: Optional[str]) -> Optional[dict]:
    for i, r in enumerate(rows):
        if r["id"] == place:
            return {"rank": r["rank"],
                    "behind": rows[i - 1]["score"] - r["score"] if i > 0 else None,
                    "ahead_of": r["score"] - rows[i + 1]["score"] if i + 1 < len(rows) else None,
                    "of": len(rows)}
    return None


async def award_crowns(db, now: Optional[datetime] = None) -> dict:
    """Crown last week's top creator of each country and battling city, once."""
    from app.services import features, leaderboard
    out = {"crowns": 0}
    if not (await features.current(db)).get("home_ground"):
        return out
    now = now or datetime.utcnow()
    this_week = leaderboard.week_start(now)
    if leaderboard.creators._naive(now) < this_week + leaderboard.AWARD_DELAY:
        return out
    last = this_week - timedelta(days=7)
    tag = last.strftime("%Y-%m-%d")
    run = f"home_ground:{tag}"
    if await db.leaderboard_runs.find_one({"_id": run}):
        return out
    week = await _week(db, last)
    for level, kind in (("country", "crown_country"), ("city", "crown_city")):
        for row in battle_rows(week, level):
            leader = row["leader"]
            if not leader or not ObjectId.is_valid(leader["id"]):
                continue
            uid = ObjectId(leader["id"])
            if await leaderboard._award(db, uid, kind, now, week=f"{tag}:{row['id']}", place=row["id"],
                                        place_name=row["name"], week_start=tag):
                out["crowns"] += 1
    try:
        await db.leaderboard_runs.insert_one({"_id": run, "at": leaderboard.creators._naive(now)})
    except Exception:
        pass
    return out
