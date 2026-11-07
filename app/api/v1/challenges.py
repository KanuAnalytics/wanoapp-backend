"""Weekly challenges (see app/services/challenges.py)."""

from datetime import datetime, timedelta
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator

from app.api.deps import get_optional_active_user, require_admin
from app.core.database import get_database
from app.services import admin_log
from app.services import challenges as svc

router = APIRouter()


class ChallengeIn(BaseModel):
    hashtag: str
    title: str = Field(..., min_length=2, max_length=80)
    description: str = Field("", max_length=500)
    starts_at: datetime
    ends_at: datetime
    prize_points: int = Field(0, ge=0, le=100_000)

    @field_validator("hashtag")
    @classmethod
    def _tag(cls, v):
        m = svc.HASHTAG.match(v.strip())
        if not m:
            raise ValueError("hashtag: 2-40 letters, numbers or _")
        return m.group(1).lower()

    @model_validator(mode="after")
    def _window(self):
        start, end = svc._naive(self.starts_at), svc._naive(self.ends_at)
        if end <= start or end <= svc._naive(svc._utcnow()):
            raise ValueError("ends_at must be after starts_at and in the future")
        if end - start > timedelta(days=svc.MAX_DAYS):
            raise ValueError(f"at most {svc.MAX_DAYS} days")
        return self


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(body: ChallengeIn, admin: str = Depends(require_admin)):
    db = get_database()
    doc = {"hashtag": body.hashtag, "title": body.title.strip(), "description": body.description.strip(),
           "starts_at": svc._naive(body.starts_at), "ends_at": svc._naive(body.ends_at),
           "prize_points": body.prize_points, "created_by": ObjectId(admin), "created_at": datetime.utcnow()}
    doc["_id"] = (await db.challenges.insert_one(doc)).inserted_id
    await admin_log.record(db, admin, "challenge_create", {"id": str(doc["_id"]), "hashtag": doc["hashtag"]})
    return svc.public(doc, svc._utcnow())


@router.get("")
async def list_challenges(current_user: Optional[str] = Depends(get_optional_active_user)):
    """Live, upcoming, and ended in the last 14 days. Logged out: authenticated=false."""
    db = get_database()
    now = svc._utcnow()
    docs = await db.challenges.find({"ends_at": {"$gte": svc._naive(now) - timedelta(days=14)}}).to_list(200)
    order = {"live": 0, "upcoming": 1, "ended": 2}
    out = [svc.public(d, now) for d in docs]
    out.sort(key=lambda c: (order[c["state"]], c["ends_at"]))
    return out


@router.get("/{challenge_id}/leaderboard")
async def leaderboard(challenge_id: str, limit: int = Query(50, ge=1, le=100),
                      current_user: Optional[str] = Depends(get_optional_active_user)):
    db = get_database()
    if not ObjectId.is_valid(challenge_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid challenge ID")
    ch = await db.challenges.find_one({"_id": ObjectId(challenge_id)})
    if not ch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Challenge not found")
    rows = await svc.ranking(db, ch)
    return {"challenge": svc.public(ch, svc._utcnow()), "entries": rows[:limit]}
