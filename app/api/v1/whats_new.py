"""What's new (app/services/whats_new.py). Behind the "whats_new" switch."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from app.api.deps import get_current_active_user
from app.core.database import get_database
from app.services import creator_push, whats_new as svc

router = APIRouter()


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["shown", "tap", "close"]


async def _db():
    db = get_database()
    await creator_push.require(db, "whats_new")
    return db


@router.get("/next")
async def next_one(app: Optional[str] = Query(None, max_length=20), me: str = Depends(get_current_active_user)):
    return await svc.next_for(await _db(), me, app)


@router.get("")
async def recent(app: Optional[str] = Query(None, max_length=20), me: str = Depends(get_current_active_user)):
    return {"items": await svc.recent(await _db(), app)}


@router.post("/{aid}/event")
async def event(aid: str, body: EventIn, me: str = Depends(get_current_active_user)):
    db = await _db()
    if body.kind == "shown":
        return await svc.shown(db, me, aid[:40])
    return await svc.event(db, me, aid[:40], body.kind)
