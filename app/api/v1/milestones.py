"""Creator milestones (app/services/milestones.py): your tier, progress on each
ladder, and celebrations the app hasn't shown yet. Behind the "milestones" switch."""

from typing import List

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StrictStr

from app.api.deps import get_current_active_user
from app.core.database import get_database
from app.services import milestones

router = APIRouter()


class SeenIn(BaseModel):
    ids: List[StrictStr] = Field(..., max_length=50)


async def _require_on(db):
    if not await milestones._enabled(db):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@router.get("/me")
async def my_milestones(current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await _require_on(db)
    return await milestones.summary(db, ObjectId(current_user))


@router.post("/seen")
async def seen(body: SeenIn, current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await _require_on(db)
    return {"updated": await milestones.mark_seen(db, ObjectId(current_user), body.ids)}
