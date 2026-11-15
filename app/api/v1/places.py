"""Places for people (app/services/places.py): search towns and neighbourhoods
and pick your area. Behind the "places" switch, off by default."""

from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_current_active_user
from app.core.database import get_database
from app.services import creator_push, home_ground as hg, places

router = APIRouter()


class AreaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    place_id: Optional[str] = Field(None, max_length=80)


async def _db():
    db = get_database()
    await creator_push.require(db, "places")
    await hg.load_custom(db)
    return db


@router.get("/search")
async def search(q: str = Query("", max_length=60), country: Optional[str] = Query(None, max_length=2),
                 me: str = Depends(get_current_active_user)):
    """Up to 20 places matching what was typed; your own country first."""
    db = await _db()
    if not country:
        u = await db.users.find_one({"_id": ObjectId(me)}, {"localization": 1}) or {}
        country = hg.country_of(u)
    return {"items": [places.out(p) for p in await places.search(db, q, country)]}


@router.get("/me")
async def my_area(me: str = Depends(get_current_active_user)):
    db = await _db()
    u = await db.users.find_one({"_id": ObjectId(me)}, {"area": 1}) or {}
    return {"area": await places.my_area(db, u)}


@router.put("/me")
async def set_my_area(body: AreaIn, me: str = Depends(get_current_active_user)):
    """Pick your area from the list (or send null to clear it)."""
    return await places.set_area(await _db(), me, body.place_id)
