"""Home Ground (app/services/home_ground.py): pick your city, country and city
battles, local creator boards. Behind the "home_ground" switch."""

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, StrictStr

from app.api.deps import get_current_active_user, get_optional_active_user
from app.core.database import get_database
from app.services import features, home_ground as hg, leaderboard

router = APIRouter()


class HomeIn(BaseModel):
    city_id: StrictStr


async def _require_on(db):
    if not (await features.current(db)).get("home_ground"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    await hg.load_custom(db)


class CityRequestIn(BaseModel):
    name: StrictStr
    country: Optional[StrictStr] = None


def _start(week: str) -> datetime:
    return leaderboard.week_start(datetime.now(timezone.utc), back=1 if week == "last" else 0)


@router.get("/cities")
async def cities(current_user: Optional[str] = Depends(get_optional_active_user)):
    await _require_on(get_database())
    return hg.cities_grouped()


@router.get("/me")
async def me(current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await _require_on(db)
    u = await db.users.find_one({"_id": ObjectId(current_user)},
                                {"localization": 1, "home_ground": 1, "date_of_birth": 1}) or {}
    city = hg.city_of(u)
    cc = hg.country_of(u)
    suggested = None
    if not city:
        loc = u.get("localization") if isinstance(u.get("localization"), dict) else {}
        s = hg.city_from_text(loc.get("city"))
        suggested = hg.CITY.get(s) if s else None
    set_at = (u.get("home_ground") or {}).get("set_at") if isinstance(u.get("home_ground"), dict) else None
    week = await hg._week(db, _start("current"))
    mine = {"city": None, "country": None}
    if city and not hg.is_minor(u):
        mine["city"] = next((r["rank"] for r in hg.board_rows(week, city=city) if r["user"]["id"] == current_user), None)
    if cc:
        mine["country"] = next((r["rank"] for r in hg.board_rows(week, country=cc) if r["user"]["id"] == current_user), None)
    return {
        "city": hg.CITY[city] if city else None,
        "country": {"id": cc, "name": hg.COUNTRIES.get(cc, cc.upper())} if cc else None,
        "suggested": suggested,
        "can_change_at": (set_at + timedelta(days=hg.CHANGE_DAYS)) if isinstance(set_at, datetime) and city else None,
        "my_rank": mine,
        "city_standing": hg.standing(hg.battle_rows(week, "city"), city) if city else None,
        "country_standing": hg.standing(hg.battle_rows(week, "country"), cc) if cc else None,
    }


@router.put("/me")
async def set_home(body: HomeIn, current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await _require_on(db)
    if body.city_id not in hg.CITY:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pick a city from the list")
    u = await db.users.find_one({"_id": ObjectId(current_user)}, {"home_ground": 1}) or {}
    cur = u.get("home_ground") if isinstance(u.get("home_ground"), dict) else {}
    now = datetime.utcnow()
    set_at = cur.get("set_at") if isinstance(cur.get("set_at"), datetime) else None
    if cur.get("city") == body.city_id:
        return {"city": hg.CITY[body.city_id]}
    if cur.get("city") in hg.CITY and set_at and now - set_at > timedelta(minutes=hg.FIX_MINUTES) \
            and now - set_at < timedelta(days=hg.CHANGE_DAYS):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail=f"You can change your Home Ground once every {hg.CHANGE_DAYS} days")
    # Typo fixes keep the original time, so they can't be used to reset the lock.
    keep = set_at if (set_at and now - set_at <= timedelta(minutes=hg.FIX_MINUTES)) else now
    await db.users.update_one({"_id": ObjectId(current_user)},
                              {"$set": {"home_ground": {"city": body.city_id, "set_at": keep}}})
    hg.clear_cache()
    return {"city": hg.CITY[body.city_id]}


@router.get("/battle")
async def battle(level: Literal["country", "city"] = "country", week: Literal["current", "last"] = "current",
                 current_user: Optional[str] = Depends(get_optional_active_user)):
    db = get_database()
    await _require_on(db)
    rows = hg.battle_rows(await hg._week(db, _start(week)), level)
    return {"level": level, "week_start": _start(week).strftime("%Y-%m-%d"), "items": rows[:50]}


@router.get("/board")
async def board(city: Optional[str] = Query(None, max_length=40), country: Optional[str] = Query(None, max_length=2),
                week: Literal["current", "last"] = "current",
                current_user: Optional[str] = Depends(get_optional_active_user)):
    db = get_database()
    await _require_on(db)
    if not city and not country:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Give a city or a country")
    if city and city not in hg.CITY:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown city")
    cc = country.lower() if country and country.isalpha() else None
    if country and not cc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Country must be a 2-letter code")
    w = await hg._week(db, _start(week))
    rows = hg.board_rows(w, city=city) if city else hg.board_rows(w, country=cc)
    return {"place": hg.CITY[city] if city else {"id": cc, "name": hg.COUNTRIES.get(cc, cc.upper())},
            "entries": rows[:50]}


@router.post("/request-city")
async def request_city(body: CityRequestIn, current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await _require_on(db)
    country = body.country
    if not country:
        u = await db.users.find_one({"_id": ObjectId(current_user)}, {"localization": 1}) or {}
        country = hg.country_of(u)
    return await hg.request_city(db, current_user, body.name, country)


PULSE_DAYS = 14
PULSE_POSTS = 12


@router.get("/pulse")
async def pulse(city: Optional[str] = Query(None, max_length=40), country: Optional[str] = Query(None, max_length=2),
                current_user: Optional[str] = Depends(get_optional_active_user)):
    """What's happening in a place: how many people represent it, who joined this
    week, and its most-watched recent posts. Makes a city page feel alive even
    before it has many creators on the board. Under-18s are never listed by city."""
    from app.services import thumbnails
    from app.services.creators import _showable
    db = get_database()
    await _require_on(db)
    if not city and not country:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Give a city or a country")
    if city and city not in hg.CITY:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown city")
    cc = country.lower() if country and country.isalpha() and len(country) == 2 else None
    if country and not cc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Country must be a 2-letter code")
    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)
    since = now - timedelta(days=PULSE_DAYS)
    adult = hg.adult_filter(now)
    if city:
        who = {"home_ground.city": city, "is_active": {"$ne": False}, **adult}
        members = await db.users.count_documents(who)
        new = await db.users.count_documents({**who, "home_ground.set_at": {"$gte": week_ago}})
        ids = [u["_id"] async for u in db.users.find(who, {"_id": 1}).limit(3000)]
        post_q = {**_showable(), "creator_id": {"$in": ids}, "created_at": {"$gte": since}}
        place = hg.CITY[city]
        faces_q = who
    else:
        members = await db.users.count_documents({"localization.country": {"$in": [cc, cc.upper()]},
                                                  "is_active": {"$ne": False}})
        new = await db.users.count_documents({"localization.country": {"$in": [cc, cc.upper()]},
                                              "is_active": {"$ne": False}, "created_at": {"$gte": week_ago}})
        post_q = {**_showable(), "country": {"$in": [cc, cc.upper()]}, "created_at": {"$gte": since}}
        place = {"id": cc, "name": hg.COUNTRIES.get(cc, cc.upper())}
        faces_q = {"localization.country": {"$in": [cc, cc.upper()]}, "is_active": {"$ne": False}}
    post_q["moderation.state"] = {"$nin": ["held", "removed"]}
    docs = await db.videos.find(post_q).sort([("views_count", -1), ("_id", -1)]).limit(PULSE_POSTS).to_list(PULSE_POSTS)
    users = {u["_id"]: u async for u in db.users.find(
        {"_id": {"$in": list({d.get("creator_id") for d in docs})}, "is_active": {"$ne": False}},
        {"username": 1, "display_name": 1, "profile_picture": 1})}
    posts = []
    for d in docs:
        u = users.get(d.get("creator_id"))
        if not u:
            continue
        posts.append({"id": str(d["_id"]), "thumbnail": thumbnails.best_thumbnail(d),
                      "views_count": d.get("views_count", 0) if isinstance(d.get("views_count"), int) else 0,
                      "creator": {"id": str(u["_id"]), "username": u.get("username"),
                                  "profile_picture": u.get("profile_picture")}})
    # A few faces from the place, people with a photo first, newest first.
    faces = [{"id": str(u["_id"]), "username": u.get("username"), "profile_picture": u.get("profile_picture")}
             async for u in db.users.find({**faces_q, "profile_picture": {"$type": "string", "$ne": ""}},
                                          {"username": 1, "profile_picture": 1}).sort("_id", -1).limit(8)]
    return {"place": place, "members": members, "new_this_week": new, "posts": posts, "faces": faces}
