"""Data for wanoafrica.com's public pages (Nov 2026).

Everything here is what anyone could already see in the app without logging in,
shaped for the website and cached, with one extra rule: the open web never shows
an under-18's name, face or posts (home_ground.is_minor), and banned, deleted or
deactivated accounts don't exist here.

- creator(username)       wanoafrica.com/@username
- creators_index()        the creator pages in the sitemap
- trending(country)       wanoafrica.com/trending
- city(city_id)           wanoafrica.com/<city>
- signups, enquiries and account-deletion requests sent from the website,
  saved here (admin pages list them), rate-limited per sender.
"""

import hashlib
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.services import home_ground

SITE = "https://wanoafrica.com"
CACHE_SECONDS = 600
INDEX_CACHE_SECONDS = 6 * 3600
POSTS_ON_PROFILE = 12
INDEX_MAX = 3000
PER_SENDER_HOUR = 5
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
USERNAME_MAX = 60
SITE_CITIES = ("kampala", "lagos", "nairobi", "accra", "johannesburg", "london")
COUNTRIES = ("ug", "ng", "ke", "gh", "za", "tz", "cd", "cm", "rw", "gb", "us")

_cache: Dict[tuple, tuple] = {}


class Invalid(ValueError):
    pass


class TooMany(Exception):
    pass


def clear_cache():
    _cache.clear()


def _cached(key, seconds=CACHE_SECONDS):
    hit = _cache.get(key)
    return hit[1] if hit and hit[0] > time.monotonic() else None


def _store(key, value, seconds=CACHE_SECONDS):
    if len(_cache) > 5000:
        _cache.clear()
    _cache[key] = (time.monotonic() + seconds, value)
    return value


def _https(u) -> Optional[str]:
    return u if isinstance(u, str) and u.startswith("https://") and len(u) <= 2048 else None


def _clip(text, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _visible_post() -> dict:
    return {"is_active": True, "privacy": "public", "isReadyToStream": {"$ne": False},
            "status": {"$nin": ["scheduled", "publishing", "cancelled"]},
            "moderation.state": {"$nin": ["held", "removed"]}}


def _ok_user(u: Optional[dict]) -> bool:
    return bool(u) and u.get("is_active") is not False and u.get("is_banned") is not True \
        and not u.get("deleted_at") and not home_ground.is_minor(u)


_EMAILISH = re.compile(r"@[^@\s]+\.[A-Za-z]{2,}")
_PHONEISH = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def private_text(text) -> bool:
    """Looks like an email address or a phone number: never shown on the open web."""
    t = str(text or "")
    return bool(_EMAILISH.search(t) or _PHONEISH.search(t))


def public_handle(username) -> Optional[str]:
    name = (username or "").strip() if isinstance(username, str) else ""
    if not name or "/" in name or private_text(name) or "@" in name:
        return None
    return name


def public_name(display, username) -> str:
    for cand in (display, username):
        c = " ".join(str(cand or "").split())
        if c and not private_text(c) and "@" not in c:
            return _clip(c, 60)
    return "Wano creator"


def handle_url(username: str) -> str:
    from urllib.parse import quote
    return f"{SITE}/@{quote(username.strip(), safe='')}"


def _post(v: dict) -> dict:
    from app.services import thumbnails
    photo = bool(v.get("images")) and (v.get("media_type") == "photo" or v.get("type") == "photo" or not v.get("urls"))
    return {"id": str(v["_id"]), "title": _clip(v.get("title") or v.get("description"), 90),
            "thumbnail": _https(thumbnails.best_thumbnail(v)), "views": int(v.get("views_count") or 0),
            "likes": int(v.get("likes_count") or 0), "photo": photo, "url": f"{SITE}/v/{v['_id']}",
            "created_at": (v.get("published_at") or v.get("created_at")).isoformat() + "Z"
            if isinstance(v.get("published_at") or v.get("created_at"), datetime) else None}


def _tier(followers: int) -> Optional[str]:
    from app.services import milestones
    label = None
    for at, _k, name in milestones.TIERS:
        if followers >= at:
            label = name
    return label


USER_FIELDS = {"username": 1, "display_name": 1, "profile_picture": 1, "bio": 1, "followers_count": 1,
               "following_count": 1, "is_active": 1, "is_banned": 1, "deleted_at": 1, "date_of_birth": 1,
               "localization": 1, "created_at": 1, "verified": 1, "is_verified": 1, "house_account": 1}


async def _find_user(db, username: str) -> Optional[dict]:
    name = (username or "").strip()
    if not name or len(name) > USERNAME_MAX:
        return None
    # Usernames are stored as typed (some with a trailing space); match those exactly, then any case.
    # Several accounts can share a name ("Justin" and "Justin ", or two "Justin"s
    # from before names were unique): the page belongs to the one with public
    # posts, then the most followed. Otherwise the sitemap linked the creator
    # with posts and the page showed an empty namesake marked noindex.
    found = await db.users.find({"username": {"$in": [name, name + " "]}}, USER_FIELDS).to_list(5)
    if not found:
        found = await db.users.find({"username": {"$in": [name, name + " "]}}, USER_FIELDS,
                                    collation={"locale": "en", "strength": 2}).to_list(5)
    if len(found) <= 1:
        return found[0] if found else None
    ok = [u for u in found if _ok_user(u)] or found
    for u in sorted(ok, key=lambda u: -int(u.get("followers_count") or 0)):
        if await db.videos.find_one({"creator_id": u["_id"], **_visible_post()}, {"_id": 1}):
            return u
    return max(ok, key=lambda u: int(u.get("followers_count") or 0))


async def creator(db, username: str) -> Optional[dict]:
    key = ("creator", (username or "").strip().lower())
    hit = _cached(key)
    if hit is not None:
        return hit or None
    u = await _find_user(db, username)
    if not _ok_user(u) or not public_handle(u.get("username")):
        _store(key, {}, 120)
        return None
    q = {"creator_id": u["_id"], **_visible_post()}
    posts = await db.videos.find(q, {"title": 1, "description": 1, "urls": 1, "images": 1, "thumbnail": 1,
                                     "thumbnails": 1, "views_count": 1, "likes_count": 1, "created_at": 1,
                                     "published_at": 1, "media_type": 1, "type": 1, "stream_uid": 1}) \
        .sort("created_at", -1).limit(POSTS_ON_PROFILE).to_list(POSTS_ON_PROFILE)
    total = await db.videos.count_documents(q, limit=10_000)
    from app.services import leaderboard
    try:
        badges = await leaderboard.badges_of(db, u["_id"])
    except Exception:
        badges = []
    followers = int(u.get("followers_count") or 0)
    loc = u.get("localization") if isinstance(u.get("localization"), dict) else {}
    country = (loc.get("country") or "").lower() if isinstance(loc.get("country"), str) else ""
    name = (u.get("username") or "").strip()
    out = {
        "id": str(u["_id"]), "username": name, "display_name": public_name(u.get("display_name"), name),
        "profile_picture": _https(u.get("profile_picture")), "bio": "" if private_text(u.get("bio")) else _clip(u.get("bio"), 300),
        "followers": followers, "following": int(u.get("following_count") or 0), "posts_count": total,
        "tier": _tier(followers), "country": country if len(country) == 2 else None,
        "country_name": home_ground.COUNTRIES.get(country) if len(country) == 2 else None,
        "badges": [{"label": b.get("label"), "kind": b.get("kind")} for b in (badges or [])[:6]
                   if isinstance(b, dict) and b.get("label")],
        "verified": bool(u.get("house_account")),
        "posts": [_post(v) for v in posts], "url": handle_url(name),
        "app_link": f"{SITE}/u/{u['_id']}",
    }
    return _store(key, out)


async def creators_index(db) -> List[dict]:
    """Creators with at least one public post, most followed first (adults only)."""
    key = ("index",)
    hit = _cached(key)
    if hit is not None:
        return hit
    since = datetime.utcnow() - timedelta(days=365)
    rows = await db.videos.aggregate([
        {"$match": {**_visible_post(), "created_at": {"$gte": since}}},
        {"$group": {"_id": "$creator_id", "last": {"$max": "$created_at"}}},
    ], maxTimeMS=20000).to_list(None)
    last = {r["_id"]: r["last"] for r in rows if isinstance(r["_id"], ObjectId)}
    out = []
    ids = list(last)
    for i in range(0, len(ids), 1000):
        async for u in db.users.find({"_id": {"$in": ids[i:i + 1000]}, "is_active": {"$ne": False},
                                      **home_ground.adult_filter()},
                                     {"username": 1, "followers_count": 1, "is_banned": 1, "deleted_at": 1}):
            name = public_handle(u.get("username"))
            if not name or u.get("is_banned") is True or u.get("deleted_at"):
                continue
            out.append({"username": name, "followers": int(u.get("followers_count") or 0),
                        "updated": last[u["_id"]].isoformat() + "Z"})
    out.sort(key=lambda r: (-r["followers"], r["username"].lower()))
    return _store(key, out[:INDEX_MAX], INDEX_CACHE_SECONDS)


async def _adults(db, user_ids: List[str]) -> set:
    oids = [ObjectId(i) for i in user_ids if ObjectId.is_valid(i)]
    ok = set()
    async for u in db.users.find({"_id": {"$in": oids}}, {"date_of_birth": 1, "is_active": 1, "is_banned": 1,
                                                         "deleted_at": 1}):
        if _ok_user(u):
            ok.add(str(u["_id"]))
    return ok


def _person(u: dict) -> dict:
    """A creator on a public list. Handles that are emails or phone numbers aren't
    shown; their row links to the app profile page instead."""
    name = public_handle(u.get("username"))
    uid = str(u.get("_id") or u.get("id") or "")
    return {"username": name, "display_name": public_name(u.get("display_name"), name),
            "profile_picture": _https(u.get("profile_picture")),
            "url": handle_url(name) if name else (f"{SITE}/u/{uid}" if uid else None)}


async def _top_posts(db, country: Optional[str], days: int = 7, limit: int = 12) -> List[dict]:
    q = {**_visible_post(), "created_at": {"$gte": datetime.utcnow() - timedelta(days=days)}}
    if country:
        q["country"] = {"$in": [country.upper(), country.lower()]}
    vids = await db.videos.find(q).sort("views_count", -1).limit(limit * 3).to_list(limit * 3)
    adults = await _adults(db, [str(v.get("creator_id")) for v in vids])
    users = {u["_id"]: u async for u in db.users.find(
        {"_id": {"$in": [ObjectId(a) for a in adults]}}, {"username": 1, "display_name": 1, "profile_picture": 1})}
    out = []
    for v in vids:
        u = users.get(v.get("creator_id"))
        if not u:
            continue
        p = _post(v)
        if not p["thumbnail"]:
            continue
        out.append({**p, "creator": _person(u)})
        if len(out) >= limit:
            break
    return out


async def trending(db, country: Optional[str] = None) -> dict:
    from app.services import leaderboard
    cc = country.lower() if country and country.lower() in COUNTRIES else None
    key = ("trending", cc)
    hit = _cached(key)
    if hit is not None:
        return hit
    start = leaderboard.week_start(leaderboard._utcnow())
    rows = await leaderboard.ranking(db, start, cc)
    if len(rows) < 5:   # early in the week: last week's board, if it had more
        prev = await leaderboard.ranking(db, leaderboard.week_start(leaderboard._utcnow(), back=1), cc)
        if len(prev) > len(rows):
            rows = prev
    adults = await _adults(db, [r["user"]["id"] for r in rows[:60]])
    creators = []
    for r in rows[:60]:
        if r["user"]["id"] in adults:
            creators.append({"rank": len(creators) + 1, **_person(r["user"]), "views": r["views"],
                             "likes": r["likes"], "posts": r["posts"]})
        if len(creators) >= 20:
            break
    from app.api.v1.music import trending_sounds
    try:
        sounds_raw = await trending_sounds(country=cc, limit=10, current_user=None)
    except Exception:
        sounds_raw = []
    sounds = [{"title": _clip((s.get("sound") or {}).get("title"), 80), "artist": _clip((s.get("sound") or {}).get("artist"), 60),
               "creators": s.get("creators", 0), "posts": s.get("posts", 0),
               "cover": _https((s.get("cover") or {}).get("thumbnail"))} for s in sounds_raw
              if (s.get("sound") or {}).get("title")]
    out = {"country": cc, "country_name": home_ground.COUNTRIES.get(cc) if cc else None,
           "week_start": start.isoformat() + "Z", "creators": creators, "sounds": sounds,
           "posts": await _top_posts(db, cc), "countries": [{"id": c, "name": home_ground.COUNTRIES.get(c, c.upper())}
                                                            for c in COUNTRIES]}
    return _store(key, out)


async def city(db, city_id: str) -> Optional[dict]:
    from app.services import leaderboard
    await home_ground.load_custom(db)
    place = home_ground.CITY.get(city_id)
    if not place or city_id not in SITE_CITIES:
        return None
    key = ("city", city_id)
    hit = _cached(key)
    if hit is not None:
        return hit
    start = leaderboard.week_start(leaderboard._utcnow())
    week = await home_ground._week(db, start)
    rows = home_ground.board_rows(week, city=city_id)
    if len(rows) < 3:
        prev = home_ground.board_rows(await home_ground._week(db, leaderboard.week_start(leaderboard._utcnow(), back=1)),
                                      city=city_id)
        if len(prev) > len(rows):
            rows = prev
    adults = await _adults(db, [r["user"]["id"] for r in rows[:30]])
    creators = [{"rank": i + 1, **_person(r["user"]), "views": r["views"]}
                for i, r in enumerate([r for r in rows[:30] if r["user"]["id"] in adults][:10])]
    out = {"id": city_id, "name": place["name"], "country": place["country"],
           "country_name": home_ground.COUNTRIES.get(place["country"]), "creators": creators,
           "posts": await _top_posts(db, place["country"], days=14, limit=8)}
    return _store(key, out)


# ---------------------------------------------------------------- things sent from the website

def _who(who) -> str:
    return who[:64] if isinstance(who, str) else ""


async def _limit(db, coll: str, who: str, now: datetime):
    if who and await db[coll].count_documents({"who": who, "created_at": {"$gte": now - timedelta(hours=1)}},
                                              limit=PER_SENDER_HOUR) >= PER_SENDER_HOUR:
        raise TooMany()


def _text(data: dict, key: str, n: int, required: bool = False) -> str:
    v = data.get(key)
    v = " ".join(v.split()) if isinstance(v, str) and key not in ("message", "reason") else (v.strip() if isinstance(v, str) else "")
    if required and not v:
        raise Invalid(f"Please fill in {key.replace('_', ' ')}")
    if len(v) > n:
        raise Invalid(f"{key.replace('_', ' ').capitalize()} is too long")
    return v


def _email(data: dict, key: str = "email", required: bool = True) -> str:
    e = _text(data, key, 254, required).lower()
    if e and not EMAIL.match(e):
        raise Invalid("Please enter a valid email address")
    return e


INTERESTS = ("wano_tv", "creators", "news")


async def subscribe(db, data: dict, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    email = _email(data)
    who = _who(data.get("who"))
    interests = [i for i in (data.get("interests") or []) if i in INTERESTS] if isinstance(data.get("interests"), list) else []
    source = _text(data, "source", 40)
    existing = await db.newsletter.find_one({"_id": email})
    if existing:
        upd = {"interests": sorted(set(existing.get("interests") or []) | set(interests or ["wano_tv"]))}
        if existing.get("unsubscribed_at"):
            upd["resubscribed_at"] = now
        await db.newsletter.update_one({"_id": email}, {"$set": upd, "$unset": {"unsubscribed_at": ""}})
        return {"subscribed": True, "new": False}
    await _limit(db, "newsletter", who, now)
    try:
        await db.newsletter.insert_one({"_id": email, "interests": interests or ["wano_tv"], "source": source,
                                        "token": secrets.token_urlsafe(24), "who": who, "created_at": now})
    except DuplicateKeyError:
        pass
    return {"subscribed": True, "new": True}


async def unsubscribe(db, token: str) -> bool:
    if not isinstance(token, str) or not (16 <= len(token) <= 64):
        return False
    r = await db.newsletter.update_one({"token": token}, {"$set": {"unsubscribed_at": datetime.utcnow()}})
    return r.matched_count == 1


ENQUIRY_KINDS = ("advertise", "press", "partner")


async def enquiry(db, data: dict, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    kind = data.get("kind") if data.get("kind") in ENQUIRY_KINDS else None
    if not kind:
        raise Invalid("Unknown enquiry")
    doc = {"kind": kind, "name": _text(data, "name", 100, True), "company": _text(data, "company", 120),
           "email": _email(data), "phone": _text(data, "phone", 40), "country": _text(data, "country", 60),
           "budget": _text(data, "budget", 60), "message": _text(data, "message", 3000, True),
           "who": _who(data.get("who")), "status": "new", "created_at": now}
    await _limit(db, "site_enquiries", doc["who"], now)
    res = await db.site_enquiries.insert_one(doc)
    return {"ref": str(res.inserted_id)}


async def deletion_request(db, data: dict, now: Optional[datetime] = None) -> dict:
    """Someone who can't open the app asks for their account to be deleted. The
    team checks it's really their account before deleting (Admin > Account deletion)."""
    now = now or datetime.utcnow()
    doc = {"account": _text(data, "account", 120, True), "email": _email(data),
           "reason": _text(data, "reason", 1000), "who": _who(data.get("who")), "status": "new", "created_at": now}
    await _limit(db, "deletion_requests", doc["who"], now)
    ref = "DEL-" + hashlib.sha1(f"{doc['email']}{now.timestamp()}".encode()).hexdigest()[:8].upper()
    doc["ref"] = ref
    await db.deletion_requests.insert_one(doc)
    return {"ref": ref}


# ---------------------------------------------------------------- admin

def _row(d: dict) -> dict:
    out = {k: v for k, v in d.items() if k not in ("who", "token")}
    out["id"] = str(out.pop("_id"))
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


async def admin_list(db, coll: str, limit: int = 300) -> dict:
    rows = [_row(d) async for d in db[coll].find({}).sort("created_at", -1).limit(limit)]
    counts = {}
    if coll == "newsletter":
        counts = {"subscribed": await db.newsletter.count_documents({"unsubscribed_at": {"$exists": False}}),
                  "unsubscribed": await db.newsletter.count_documents({"unsubscribed_at": {"$exists": True}})}
    else:
        counts = {"new": await db[coll].count_documents({"status": "new"})}
    return {"rows": rows, "counts": counts}


async def admin_set_status(db, coll: str, item_id: str, status_: str, admin: str) -> bool:
    if status_ not in ("new", "done", "rejected"):
        raise Invalid("Status must be new, done or rejected")
    q = {"ref": item_id} if coll == "deletion_requests" else (
        {"_id": ObjectId(item_id)} if ObjectId.is_valid(item_id) else None)
    if q is None:
        return False
    r = await db[coll].update_one(q, {"$set": {"status": status_, "status_by": admin, "status_at": datetime.utcnow()}})
    return r.matched_count == 1


async def newsletter_csv(db) -> str:
    lines = ["email,interests,source,joined"]
    async for d in db.newsletter.find({"unsubscribed_at": {"$exists": False}}).sort("created_at", 1):
        def esc(s):
            s = str(s or "")
            s = "'" + s if s[:1] in "=+-@" else s
            return '"' + s.replace('"', '""') + '"'
        lines.append(",".join([esc(d["_id"]), esc(" ".join(d.get("interests") or [])), esc(d.get("source")),
                               esc(d["created_at"].date().isoformat() if isinstance(d.get("created_at"), datetime) else "")]))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- topic, hashtag and sound pages
# wanoafrica.com/videos/<topic>/<country>, /tag/<hashtag>, /sound/<id>. A page only
# exists when enough real public posts back it (MIN_*), so none is empty.

MIN_TOPIC_POSTS = 6
MIN_TAG_POSTS = 5
MIN_SOUND_POSTS = 3
PAGE_POSTS = 24
LIST_CACHE = 6 * 3600
_TAG = re.compile(r"^[\wÀ-ɏ]{2,50}$")


def _categories() -> Dict[str, str]:
    from app.services import points
    return {k: points.MORE_LABELS.get(k, v) for k, v in points.ALL_CATEGORIES.items()}


CI = {"locale": "en", "strength": 2}


async def _posts_with_creators(db, q: dict, limit: int = PAGE_POSTS, sort=("views_count", -1),
                               collation=None) -> List[dict]:
    cur = db.videos.find(q, collation=collation) if collation else db.videos.find(q)
    vids = await cur.sort([sort, ("_id", -1)]).limit(limit * 3).to_list(limit * 3)
    adults = await _adults(db, [str(v.get("creator_id")) for v in vids])
    users = {u["_id"]: u async for u in db.users.find(
        {"_id": {"$in": [ObjectId(a) for a in adults]}}, {"username": 1, "display_name": 1, "profile_picture": 1})}
    out = []
    for v in vids:
        u = users.get(v.get("creator_id"))
        if not u:
            continue
        p = _post(v)
        if p["thumbnail"]:
            out.append({**p, "creator": _person(u)})
        if len(out) >= limit:
            break
    return out


def _top_creators(posts: List[dict], limit: int = 8) -> List[dict]:
    agg: Dict[str, dict] = {}
    for p in posts:
        c = p.get("creator") or {}
        key = c.get("url")
        if not key:
            continue
        a = agg.setdefault(key, {**c, "views": 0})
        a["views"] += p["views"]
    rows = sorted(agg.values(), key=lambda r: -r["views"])[:limit]
    return [{"rank": i + 1, **r} for i, r in enumerate(rows)]


async def topics(db) -> List[dict]:
    """Every (topic, country) with enough public posts, and each topic overall."""
    key = ("topics",)
    hit = _cached(key)
    if hit is not None:
        return hit
    cats = _categories()
    rows = await db.videos.aggregate([
        {"$match": {**_visible_post(), "category": {"$in": list(cats)}}},
        {"$group": {"_id": {"c": "$category", "k": {"$toLower": {"$ifNull": ["$country", ""]}}}, "n": {"$sum": 1}}},
    ], maxTimeMS=20000).to_list(None)
    totals: Dict[str, int] = {}
    out = []
    for r in rows:
        cat, cc = r["_id"]["c"], r["_id"]["k"]
        totals[cat] = totals.get(cat, 0) + r["n"]
        if len(cc) == 2 and cc in home_ground.COUNTRIES and r["n"] >= MIN_TOPIC_POSTS:
            out.append({"topic": cat, "label": cats[cat], "country": cc, "country_name": home_ground.COUNTRIES[cc],
                        "posts": r["n"]})
    for cat, n in totals.items():
        if n >= MIN_TOPIC_POSTS:
            out.append({"topic": cat, "label": cats[cat], "country": None, "country_name": None, "posts": n})
    out.sort(key=lambda x: -x["posts"])
    return _store(key, out, LIST_CACHE)


async def topic(db, cat: str, country: Optional[str]) -> Optional[dict]:
    cats = _categories()
    cc = (country or "").lower() or None
    if cat not in cats or (cc and cc not in home_ground.COUNTRIES):
        return None
    key = ("topic", cat, cc)
    hit = _cached(key)
    if hit is not None:
        return hit or None
    q = {**_visible_post(), "category": cat}
    if cc:
        q["country"] = {"$in": [cc.upper(), cc]}
    total = await db.videos.count_documents(q, limit=100_000)
    if total < MIN_TOPIC_POSTS:
        _store(key, {}, 1800)
        return None
    posts = await _posts_with_creators(db, q)
    countries = [t for t in await topics(db) if t["topic"] == cat and t["country"]]
    out = {"topic": cat, "label": cats[cat], "country": cc, "country_name": home_ground.COUNTRIES.get(cc) if cc else None,
           "posts_count": total, "posts": posts, "creators": _top_creators(posts),
           "countries": [{"id": t["country"], "name": t["country_name"], "posts": t["posts"]} for t in countries][:20],
           "related": [{"topic": t["topic"], "label": t["label"]} for t in await topics(db)
                       if t["country"] == cc and t["topic"] != cat][:10]}
    return _store(key, out)


def clean_tag(tag: str) -> Optional[str]:
    t = (tag or "").strip().lstrip("#").lower()
    return t if _TAG.match(t) else None


async def tags(db, limit: int = 3000) -> List[dict]:
    key = ("tags",)
    hit = _cached(key)
    if hit is not None:
        return hit
    rows = await db.videos.aggregate([
        {"$match": {**_visible_post(), "hashtags.0": {"$exists": True}}},
        {"$project": {"hashtags": 1}}, {"$unwind": "$hashtags"},
        {"$match": {"hashtags": {"$type": "string"}}},
        {"$group": {"_id": {"$toLower": "$hashtags"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gte": 2}}}, {"$sort": {"n": -1}}, {"$limit": limit * 2},
    ], maxTimeMS=20000).to_list(limit * 2)
    merged: Dict[str, int] = {}
    for r in rows:   # "#Kampala" and "kampala" are one tag
        t = clean_tag(r["_id"])
        if t and not private_text(t):
            merged[t] = merged.get(t, 0) + r["n"]
    out = [{"tag": t, "posts": n} for t, n in sorted(merged.items(), key=lambda x: -x[1]) if n >= MIN_TAG_POSTS][:limit]
    return _store(key, out, LIST_CACHE)


async def tag(db, raw: str) -> Optional[dict]:
    t = clean_tag(raw)
    if not t or private_text(t):
        return None
    key = ("tag", t)
    hit = _cached(key)
    if hit is not None:
        return hit or None
    # Hashtags are stored as typed ("#KampalaComedy", "kampalacomedy"): match any case.
    q = {**_visible_post(), "hashtags": {"$regex": f"^#?{re.escape(t)}$", "$options": "i"}}
    total = await db.videos.count_documents(q, limit=100_000)
    if total < MIN_TAG_POSTS:
        _store(key, {}, 1800)
        return None
    posts = await _posts_with_creators(db, q)
    rel = await db.videos.aggregate([
        {"$match": q}, {"$project": {"hashtags": 1}}, {"$limit": 500}, {"$unwind": "$hashtags"},
        {"$group": {"_id": {"$toLower": "$hashtags"}, "n": {"$sum": 1}}}, {"$sort": {"n": -1}}, {"$limit": 30},
    ], maxTimeMS=10000).to_list(30)
    known = {x["tag"] for x in await tags(db)}
    related = list(dict.fromkeys(c for r in rel if (c := clean_tag(r["_id"])) and c != t and c in known))[:12]
    out = {"tag": t, "posts_count": total, "posts": posts, "creators": _top_creators(posts), "related": related}
    return _store(key, out)


async def sounds(db, limit: int = 2000) -> List[dict]:
    key = ("sounds",)
    hit = _cached(key)
    if hit is not None:
        return hit
    rows = await db.videos.aggregate([
        {"$match": {**_visible_post(), "sound.id": {"$type": "string"}}},
        {"$group": {"_id": "$sound.id", "sound": {"$first": "$sound"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gte": MIN_SOUND_POSTS}}}, {"$sort": {"n": -1}}, {"$limit": limit},
    ], maxTimeMS=20000).to_list(limit)
    out = []
    for r in rows:
        s = r.get("sound") or {}
        title = _clip(s.get("title"), 90)
        if title and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", r["_id"] or "") and not private_text(title):
            out.append({"id": r["_id"], "title": title, "artist": _clip(s.get("artist"), 60), "posts": r["n"]})
    return _store(key, out, LIST_CACHE)


async def sound(db, sid: str) -> Optional[dict]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid or ""):
        return None
    key = ("sound", sid)
    hit = _cached(key)
    if hit is not None:
        return hit or None
    q = {**_visible_post(), "sound.id": sid}
    total = await db.videos.count_documents(q, limit=100_000)
    first = await db.videos.find_one(q, {"sound": 1})
    s = (first or {}).get("sound") or {}
    if total < MIN_SOUND_POSTS or not s.get("title") or private_text(s.get("title")):
        _store(key, {}, 1800)
        return None
    posts = await _posts_with_creators(db, q)
    out = {"id": sid, "title": _clip(s.get("title"), 90), "artist": _clip(s.get("artist"), 60), "posts_count": total,
           "posts": posts, "creators": _top_creators(posts)}
    return _store(key, out)


# ------------------------------------------------------------------ video pages
# The website's video page and its video sitemap used to read the app's own
# endpoints, which show every public post with its creator's name and picture,
# a young person's included, and count a view on every page build. These say
# only what an open web page may: public posts by adults; a young person's post
# answers "limited" (the page says "open it in Wano", nothing else).

def _stream_url(v: dict) -> Optional[str]:
    return _https(v.get("remoteUrl_CF")) or _https((v.get("urls") or {}).get("hls_playlist"))


async def _preview_image(db, v: dict) -> Optional[str]:
    """The link-preview picture with a play button, when share_play is on."""
    from app.services import creator_push
    from app.core.config import settings
    if not _stream_url(v) or not await creator_push.on(db, "share_play"):
        return None
    return f"{settings.SHARE_BASE_URL.rstrip('/')}/og/{v['_id']}.jpg"


async def video(db, video_id: str) -> Optional[dict]:
    if not ObjectId.is_valid(video_id):
        return None
    v = await db.videos.find_one({**_visible_post(), "_id": ObjectId(video_id),
                                  "privacy": {"$in": ["public", "unlisted"]}})
    if not v or not v.get("creator_id"):
        return None
    u = await db.users.find_one({"_id": v["creator_id"]},
                                {"username": 1, "display_name": 1, "profile_picture": 1, "date_of_birth": 1,
                                 "is_active": 1, "is_banned": 1, "deleted_at": 1})
    if not u or u.get("is_active") is False or u.get("is_banned") is True or u.get("deleted_at"):
        return None
    if home_ground.is_minor(u):
        return {"_id": str(v["_id"]), "limited": True}
    from app.services import thumbnails
    handle = public_handle(u.get("username"))
    when = v.get("published_at") or v.get("created_at")
    return {
        "_id": str(v["_id"]), "limited": False, "unlisted": v.get("privacy") == "unlisted",
        "title": _clip(v.get("title"), 120) or None, "description": _clip(v.get("description"), 2000),
        "views_count": int(v.get("views_count") or 0), "likes_count": int(v.get("likes_count") or 0),
        "comments_count": int(v.get("comments_count") or 0),
        "remoteUrl_CF": _stream_url(v), "urls": {"thumbnail": _https(thumbnails.best_thumbnail(v))},
        "preview_image": await _preview_image(db, v),
        "photo": bool(v.get("images")) and not _stream_url(v),
        "images": [i for i in (_https(x) for x in (v.get("images") or [])) if i][:10],
        "created_at": when.isoformat() + ("Z" if when.tzinfo is None else "") if isinstance(when, datetime) else None,
        "duration": v.get("duration") if isinstance(v.get("duration"), (int, float)) else None,
        "user": {"username": handle, "display_name": public_name(u.get("display_name"), handle),
                 "profile_picture": _https(u.get("profile_picture"))},
    }


async def sitemap_videos(db, skip: int, limit: int) -> List[dict]:
    """Newest public videos by adults, for the website's video sitemap."""
    key = f"sv:{skip}:{limit}"
    hit = _cached(key)
    if hit is not None:
        return hit
    q = {**_visible_post(), "media_type": {"$ne": "photo"}}
    vids = await db.videos.find(q).sort("_id", -1).skip(skip).limit(limit).to_list(limit)
    adults = await _adults(db, [str(v.get("creator_id")) for v in vids])
    users = {u["_id"]: u async for u in db.users.find(
        {"_id": {"$in": [ObjectId(a) for a in adults]}}, {"username": 1, "display_name": 1})}
    from app.services import thumbnails
    out = []
    for v in vids:
        u = users.get(v.get("creator_id"))
        stream = _stream_url(v)
        if not u or not stream:
            continue
        handle = public_handle(u.get("username"))
        out.append({"id": str(v["_id"]), "title": _clip(v.get("title"), 120) or None,
                    "description": _clip(v.get("description"), 2000) or None,
                    "thumbnail": _https(thumbnails.best_thumbnail(v)), "remoteUrl_CF": stream,
                    "user": {"username": handle, "display_name": public_name(u.get("display_name"), handle)}})
    _store(key, out)
    return out
