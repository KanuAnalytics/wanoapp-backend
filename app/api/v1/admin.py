"""Admin page endpoints (the page itself is served at /admin, app/routes/admin_page.py).

Everything here needs an admin account. Payouts, the pool and "not original"
use the existing /points/admin/* endpoints; challenges are created with
POST /challenges; moderation uses /reports/admin/*."""

import re
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, StrictBool

from app.api.deps import require_admin
from app.api.v1 import auth as auth_api
from app.api.v1.config import APP_FEATURES
from app.core.database import get_database
from app.core.security import create_access_token, verify_password
from app.services import admin_log, features, moderation, points
from app.services import challenges as challenges_svc

router = APIRouter()

SESSION_HOURS = 8


class LoginIn(BaseModel):
    username: str
    password: str


class FeatureIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Literal[APP_FEATURES]  # type: ignore[valid-type]
    on: StrictBool


@router.post("/login")
async def login(body: LoginIn, request: Request):
    """Like /auth/login/json, but only admins, and the session lasts SESSION_HOURS."""
    db = get_database()
    ip = request.client.host if request.client else None
    key = "admin:" + auth_api._login_key(body.username, ip)
    if auth_api._login_throttled(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts. Try again later.")
    user = await db.users.find_one(auth_api._identifier_query(body.username))
    ok = (user and verify_password(body.password, user.get("password_hash") or "")
          and user.get("is_active") is not False and str(user.get("user_type", "")).lower() == "admin")
    if not ok:
        auth_api._record_login_failure(key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password, or not an admin account")
    auth_api._clear_login_failures(key)
    # The exp claim is set here, not via expires_delta: app logins don't expire
    # (create_access_token ignores expires_delta), but an admin session must.
    exp = int((datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)).timestamp())
    token = create_access_token({"sub": str(user["_id"]), "exp": exp})
    return {"access_token": token, "username": user["username"], "expires_in": SESSION_HOURS * 3600}


@router.get("/me")
async def me(admin: str = Depends(require_admin)):
    u = await get_database().users.find_one({"_id": ObjectId(admin)}, {"username": 1, "display_name": 1})
    return {"id": admin, "username": u.get("username"), "display_name": u.get("display_name")}


# ---------------------------------------------------------------- switches

@router.get("/features")
async def get_features(admin: str = Depends(require_admin)):
    return await features.current(get_database())


@router.put("/features")
async def put_feature(body: FeatureIn, admin: str = Depends(require_admin)):
    db = get_database()
    try:
        out = await features.set_feature(db, body.key, body.on)
    except features.FeatureError as e:
        raise HTTPException(e.status, e.detail)
    await admin_log.record(db, admin, "feature", {"key": body.key, "on": body.on})
    return out


@router.post("/features/all-off")
async def features_all_off(admin: str = Depends(require_admin)):
    db = get_database()
    out = await features.all_off(db)
    await admin_log.record(db, admin, "features_all_off")
    return out


# ---------------------------------------------------------------- overview and log

@router.get("/overview")
async def overview(admin: str = Depends(require_admin)):
    db = get_database()
    waiting, cents = 0, 0
    async for p in db.points_payouts.find({"state": "requested"}, {"cents": 1}):
        waiting += 1
        cents += p.get("cents") if isinstance(p.get("cents"), int) else 0
    now = points._naive(points._utcnow())
    mod = await db.videos.count_documents({"is_active": True, "$or": [
        {"moderation.state": moderation.HELD}, {"moderation.flags": {"$gt": 0}}]})
    live = await db.challenges.count_documents({"starts_at": {"$lte": now}, "ends_at": {"$gt": now}})
    return {"payouts_waiting": waiting, "payouts_waiting_usd": cents / 100,
            "pool": await points.pool_preview(db, points.month_of(now)),
            "moderation_waiting": mod, "challenges_live": live,
            "features": await features.current(db)}


@router.get("/log")
async def log(limit: int = Query(100, ge=1, le=500), admin: str = Depends(require_admin)):
    return await admin_log.recent(get_database(), limit)


# ---------------------------------------------------------------- creators

USERNAME = re.compile(r"^@?([A-Za-z0-9_.]{1,40})$")
VIDEO_LINK = re.compile(r"/(?:v|embed)/([0-9a-fA-F]{24})(?:[/?#]|$)")


async def _find_creator(db, q: str) -> Optional[dict]:
    q = q.strip()
    m = VIDEO_LINK.search(q)
    if m:
        v = await db.videos.find_one({"_id": ObjectId(m.group(1))}, {"creator_id": 1})
        return await db.users.find_one({"_id": v["creator_id"]}) if v and v.get("creator_id") else None
    if ObjectId.is_valid(q) and len(q) == 24:
        u = await db.users.find_one({"_id": ObjectId(q)})
        if u:
            return u
        v = await db.videos.find_one({"_id": ObjectId(q)}, {"creator_id": 1})
        return await db.users.find_one({"_id": v["creator_id"]}) if v and v.get("creator_id") else None
    m = USERNAME.match(q)
    if not m:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Type a username, user ID or post link")
    name = m.group(1)
    return (await db.users.find_one({"username": name})
            or await db.users.find_one({"username": name.lower()})
            or await db.users.find_one({"username": {"$regex": f"^{re.escape(name)}$", "$options": "i"}}))


@router.get("/creators/lookup")
async def lookup(q: str = Query(..., min_length=1, max_length=200), admin: str = Depends(require_admin)):
    """Find a creator by username, @username, user ID, post ID or post link."""
    db = get_database()
    u = await _find_creator(db, q)
    if not u:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No account found")
    uid = u["_id"]
    s = await points.summary(db, uid)
    acct = await db.payout_accounts.find_one({"_id": uid}) or None
    posts = await db.videos.find({"creator_id": uid, "is_active": True},
                                 {"title": 1, "description": 1, "urls.thumbnail": 1, "created_at": 1,
                                  "originality": 1, "views_count": 1}).sort("created_at", -1).limit(12).to_list(12)
    loc = u.get("localization") if isinstance(u.get("localization"), dict) else {}
    return {
        "user": {"id": str(uid), "username": u.get("username"), "display_name": u.get("display_name"),
                 "email": u.get("email"), "phone_number": u.get("phone_number"),
                 "followers_count": u.get("followers_count", 0), "created_at": u.get("created_at"),
                 "is_active": u.get("is_active") is True, "user_type": u.get("user_type"),
                 "country": loc.get("country"), "profile_picture": u.get("profile_picture")},
        "creator_status": s["creator_status"],
        "earnings": s["earnings"],
        "points": {"balance": s["balance"], "pending": s["pending"], "month": s["month"],
                   "posting_days": s["posting_days"], "days_needed": s["days_needed"]},
        "payout_account": {"method": acct.get("method"), "account": acct.get("account"),
                           "country": acct.get("country")} if acct else None,
        "posts": [{"id": str(p["_id"]),
                   "title": p.get("title") if isinstance(p.get("title"), str) else (p.get("description") or ""),
                   "thumbnail": (p.get("urls") or {}).get("thumbnail") if isinstance(p.get("urls"), dict) else None,
                   "views": p.get("views_count", 0), "created_at": p.get("created_at"),
                   "originality": (p.get("originality") or {}).get("state")} for p in posts],
    }


# ---------------------------------------------------------------- challenges

@router.get("/challenges")
async def all_challenges(admin: str = Depends(require_admin)):
    db = get_database()
    now = challenges_svc._utcnow()
    docs = await db.challenges.find({}).sort("starts_at", -1).limit(100).to_list(100)
    return [challenges_svc.public(d, now) for d in docs]


@router.post("/challenges/{challenge_id}/end")
async def end_challenge(challenge_id: str, admin: str = Depends(require_admin)):
    if not ObjectId.is_valid(challenge_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid challenge ID")
    db = get_database()
    now = challenges_svc._naive(challenges_svc._utcnow())
    # An upcoming one ends before it starts: move its start back to now too.
    ch = await db.challenges.find_one_and_update({"_id": ObjectId(challenge_id), "ends_at": {"$gt": now}},
                                                 {"$set": {"ends_at": now, "ended_early_by": ObjectId(admin)},
                                                  "$min": {"starts_at": now}})
    if not ch:
        raise HTTPException(status.HTTP_409_CONFLICT, "Challenge not found or already ended")
    await admin_log.record(db, admin, "challenge_end", {"id": challenge_id, "hashtag": ch.get("hashtag")})
    return {"id": challenge_id, "state": "ended"}


# ---------------------------------------------------------------- payouts

@router.get("/payouts")
async def payouts(state: Literal["requested", "approved", "rejected"] = "requested",
                  limit: int = Query(100, ge=1, le=500), admin: str = Depends(require_admin)):
    """Payout requests by state. shared_account_with: other creators paying to the
    same number or key (often one person with several accounts)."""
    db = get_database()
    order = 1 if state == "requested" else -1
    rows = await db.points_payouts.find({"state": state}).sort("at", order).limit(limit).to_list(limit)
    users = {u["_id"]: u async for u in db.users.find({"_id": {"$in": [r["user_id"] for r in rows]}},
                                                      {"username": 1, "display_name": 1, "phone_number": 1,
                                                       "is_active": 1})}
    out = []
    for r in rows:
        acct = r.get("payout_account") or {}
        others = []
        if acct.get("account"):
            others = [str(x["_id"]) async for x in db.payout_accounts.find(
                {"method": acct.get("method"), "account": acct["account"], "_id": {"$ne": r["user_id"]}}, {"_id": 1})]
        u = users.get(r["user_id"], {})
        out.append({"id": str(r["_id"]), "state": r.get("state"), "usd": (r.get("cents") or 0) / 100,
                    "requested_at": r.get("at"), "decided_at": r.get("decided_at"),
                    "payout_account": acct, "shared_account_with": others,
                    "user": {"id": str(r["user_id"]), "username": u.get("username"),
                             "display_name": u.get("display_name"), "phone_number": u.get("phone_number"),
                             "account_active": u.get("is_active") is True}})
    return out
