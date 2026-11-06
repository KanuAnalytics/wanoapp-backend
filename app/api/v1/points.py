"""
Wano points (see app/services/points.py).
"""

from typing import Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from app.api.deps import get_current_active_user, get_optional_active_user, require_admin
from app.core.database import get_database
from app.services import points

router = APIRouter()

Category = Literal[tuple(points.CATEGORIES)]  # type: ignore[valid-type]


class BoostRequest(BaseModel):
    video_id: str
    points: int = Field(..., ge=points.MIN_BOOST, le=points.MAX_BOOST)


class CategoryRequest(BaseModel):
    category: Category


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
async def categories(current_user=Depends(get_optional_active_user)):
    """Categories a post must be in to earn points."""
    return [{"id": k, "label": v} for k, v in points.CATEGORIES.items()]


@router.get("/me")
async def my_points(current_user: str = Depends(get_current_active_user)):
    """Balance (spendable), pending this month, posting days so far, rules, history."""
    return await points.summary(get_database(), ObjectId(current_user))


@router.put("/videos/{video_id}/category")
async def set_category(video_id: str, body: CategoryRequest, current_user: str = Depends(get_current_active_user)):
    db = get_database()
    oid = _oid(video_id, "video ID")
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


@router.post("/payouts")
async def request_payout(current_user: str = Depends(get_current_active_user)):
    return await _run(points.request_payout(get_database(), ObjectId(current_user)))


@router.get("/admin/payouts")
async def payouts_to_review(limit: int = Query(100, ge=1, le=500), admin: str = Depends(require_admin)):
    db = get_database()
    rows = await db.points_payouts.find({"state": "requested"}).sort("at", 1).limit(limit).to_list(limit)
    users = {u["_id"]: u async for u in db.users.find({"_id": {"$in": [r["user_id"] for r in rows]}},
                                                      {"username": 1, "display_name": 1, "phone_number": 1})}
    return [{"id": str(r["_id"]), "usd": r.get("cents", 0) / 100, "requested_at": r["at"],
             "user": {"id": str(r["user_id"]), "username": users.get(r["user_id"], {}).get("username"),
                      "display_name": users.get(r["user_id"], {}).get("display_name"),
                      "phone_number": users.get(r["user_id"], {}).get("phone_number")}} for r in rows]


class PoolIn(BaseModel):
    revenue_usd: Optional[float] = Field(None, ge=0, le=10_000_000)
    percent: Optional[float] = Field(None, ge=0, le=100)
    floor_usd: Optional[float] = Field(None, ge=0, le=1_000_000)


Month = Path(..., pattern=r"^20\d\d-(0[1-9]|1[0-2])$")


@router.get("/pool")
async def pool(current_user=Depends(get_optional_active_user)):
    """This month's creator pool and what last month's paid per 1,000 points."""
    return await points.public_pool(get_database())


@router.put("/admin/pool/{month}")
async def set_pool(body: PoolIn, month: str = Month, admin: str = Depends(require_admin)):
    """Enter the month's revenue (and optionally change the % or the floor)."""
    return await _run(points.set_pool(get_database(), month, **body.model_dump()))


@router.get("/admin/pool/{month}")
async def preview_pool(month: str = Month, admin: str = Depends(require_admin)):
    return await points.pool_preview(get_database(), month)


@router.post("/admin/pool/{month}/distribute")
async def distribute_pool(month: str = Month, admin: str = Depends(require_admin)):
    """Share the pool out now (otherwise it happens on the 5th)."""
    return await _run(points.distribute(get_database(), month))


@router.post("/admin/payouts/{payout_id}/{decision}")
async def decide(payout_id: str, decision: Literal["approve", "reject"], admin: str = Depends(require_admin)):
    """Approve once the creator has been paid outside Wano; reject returns the points."""
    return await _run(points.decide_payout(get_database(), _oid(payout_id), decision == "approve", admin))


@router.post("/admin/videos/{video_id}/not-original")
async def mark_not_original(video_id: str, admin: str = Depends(require_admin)):
    return await _run(points.not_original(get_database(), _oid(video_id, "video ID"), admin))
