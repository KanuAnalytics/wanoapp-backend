"""
Wano points (see app/services/points.py).
"""

from typing import Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from app.services import admin_log
from app.api.deps import get_current_active_user, get_optional_active_user, require_admin
from app.core.database import get_database
from app.services import points

router = APIRouter()

Category = Literal[tuple(points.ALL_CATEGORIES)]  # type: ignore[valid-type]


class BoostRequest(BaseModel):
    video_id: str
    points: int = Field(..., ge=points.MIN_BOOST, le=points.MAX_BOOST)


class CategoryRequest(BaseModel):
    category: Category


class PoolIn(BaseModel):
    revenue_usd: Optional[float] = Field(None, ge=0, le=100_000_000)
    percent: Optional[float] = Field(None, ge=0, le=100)
    floor_usd: Optional[float] = Field(None, ge=0, le=1_000_000)
    # Needed when the resulting pool is above POINTS_POOL_MAX_USD (typo guard).
    confirm: bool = False


class PayoutAccountIn(BaseModel):
    method: Literal["mobile_money", "pix", "bank"]
    account: str = Field(..., min_length=3, max_length=80)
    country: str = Field(..., pattern=r"^[A-Za-z]{2}$")


def _oid(v: str, what: str = "ID") -> ObjectId:
    if not ObjectId.is_valid(v):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid {what}")
    return ObjectId(v)


async def _run(coro):
    try:
        return await coro
    except points.PointsError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


@router.get("/categories")
async def categories(all: bool = Query(False), current_user=Depends(get_optional_active_user)):
    """Categories a post must be in to earn points. The full list (more_categories)
    only goes to apps that ask for it with ?all=1 (2.3.25+): older apps' category
    sheet doesn't scroll, so they keep the six they were built for."""
    db = get_database()
    cats = await points.active_categories(db) if all else points.CATEGORIES
    return [{"id": k, "label": v} for k, v in cats.items()]


class SuggestIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.post("/categories/suggest")
async def suggest_category(body: SuggestIn, current_user: str = Depends(get_current_active_user)):
    """My content fits none of the categories: tell the Wano team (more_categories)."""
    db = get_database()
    if not await points.more_on(db):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return await points.suggest_category(db, ObjectId(current_user), body.name)


@router.get("/me")
async def my_points(current_user: str = Depends(get_current_active_user)):
    """Balance (spendable), pending this month, posting days so far, rules, history."""
    return await points.summary(get_database(), ObjectId(current_user))


@router.put("/videos/{video_id}/category")
async def set_category(video_id: str, body: CategoryRequest, current_user: str = Depends(get_current_active_user)):
    db = get_database()
    oid = _oid(video_id, "video ID")
    if await points.allowed_category(db, body.category) is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pick one of the categories shown")
    res = await db.videos.update_one({"_id": oid, "creator_id": ObjectId(current_user)},
                                     {"$set": {"category": body.category}})
    if not res.matched_count:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your post")
    return {"video_id": video_id, "category": body.category}


@router.post("/boost")
async def boost(body: BoostRequest, current_user: str = Depends(get_current_active_user)):
    """Spend points to show your post to more people in For You."""
    return await _run(points.boost(get_database(), ObjectId(current_user), _oid(body.video_id, "video ID"),
                                   body.points))


@router.put("/payout-account")
async def set_payout_account(body: PayoutAccountIn, current_user: str = Depends(get_current_active_user)):
    """Where to send the creator's money: mobile money number, Pix key, or bank account."""
    return await _run(points.set_payout_account(get_database(), ObjectId(current_user), body.method,
                                                body.account, body.country))


@router.post("/payouts")
async def request_payout(current_user: str = Depends(get_current_active_user)):
    return await _run(points.request_payout(get_database(), ObjectId(current_user)))


@router.get("/admin/payouts")
async def payouts_to_review(limit: int = Query(100, ge=1, le=500), admin: str = Depends(require_admin)):
    """Requests waiting for a decision. shared_account_with lists other creators
    paying to the same number or key (often one person with several accounts)."""
    db = get_database()
    rows = await db.points_payouts.find({"state": "requested"}).sort("at", 1).limit(limit).to_list(limit)
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
        out.append({"id": str(r["_id"]), "usd": r.get("cents", 0) / 100, "requested_at": r["at"],
                    "payout_account": acct, "shared_account_with": others,
                    "user": {"id": str(r["user_id"]), "username": u.get("username"),
                             "display_name": u.get("display_name"), "phone_number": u.get("phone_number"),
                             "account_active": u.get("is_active") is True}})
    return out


Month = Path(..., pattern=r"^20\d\d-(0[1-9]|1[0-2])$")


@router.get("/pool")
async def pool(current_user=Depends(get_optional_active_user)):
    """This month's creator pool and what last month's paid per 1,000 points."""
    return await points.public_pool(get_database())


@router.put("/admin/pool/{month}")
async def set_pool(body: PoolIn, month: str = Month, admin: str = Depends(require_admin)):
    """Enter the month's revenue (and optionally change the % or the floor)."""
    data = body.model_dump()
    out = await _run(points.set_pool(get_database(), month, confirm=data.pop("confirm"), **data))
    await admin_log.record(get_database(), admin, "pool", {"month": month, **{k: v for k, v in data.items() if v is not None},
                                                           "pool_usd": out.get("pool_usd")})
    return out


@router.get("/admin/pool/{month}")
async def preview_pool(month: str = Month, admin: str = Depends(require_admin)):
    """What sharing out would pay now: only the top N Wano Creators, with names."""
    db = get_database()
    out = await points.pool_preview(db, month, as_paid=True)
    ids = [ObjectId(t["user_id"]) for t in out["top"]]
    names = {str(u["_id"]): u.get("username") async for u in db.users.find({"_id": {"$in": ids}}, {"username": 1})}
    for t in out["top"]:
        t["username"] = names.get(t["user_id"])
    return out


@router.post("/admin/pool/{month}/distribute")
async def distribute_pool(month: str = Month, admin: str = Depends(require_admin)):
    """Share the pool out now (otherwise it happens on the 5th)."""
    out = await _run(points.distribute(get_database(), month))
    await admin_log.record(get_database(), admin, "distribute", {"month": month, "paid_usd": out.get("paid_usd"),
                                                                 "creators": out.get("creators")})
    return out


@router.post("/admin/payouts/{payout_id}/{decision}")
async def decide(payout_id: str, decision: Literal["approve", "reject"], admin: str = Depends(require_admin)):
    """Approve once the creator has been paid outside Wano; reject returns the points."""
    out = await _run(points.decide_payout(get_database(), _oid(payout_id), decision == "approve", admin))
    await admin_log.record(get_database(), admin, "payout_" + decision, {"id": payout_id, "usd": out.get("usd")})
    return out


@router.post("/admin/videos/{video_id}/not-original")
async def mark_not_original(video_id: str, admin: str = Depends(require_admin)):
    out = await _run(points.not_original(get_database(), _oid(video_id, "video ID"), admin))
    await admin_log.record(get_database(), admin, "not_original", {"video_id": video_id})
    return out


@router.post("/admin/videos/{video_id}/original-again")
async def mark_original_again(video_id: str, admin: str = Depends(require_admin)):
    out = await _run(points.original_again(get_database(), _oid(video_id, "video ID"), admin))
    await admin_log.record(get_database(), admin, "original_again", {"video_id": video_id})
    return out
