"""Local flyers for viewers (app/services/flyers.py). Behind the "flyers" switch."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, StrictStr

from app.api.deps import get_current_active_user, get_optional_active_user
from app.core.database import get_database
from app.services import creator_push, flyers as svc

router = APIRouter()


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flyer_id: StrictStr
    kind: Literal["shown", "tap", "close"]


async def _db():
    db = get_database()
    await creator_push.require(db, "flyers")
    return db


@router.get("")
async def my_flyers(clip_topic: Optional[str] = None, current_user: Optional[str] = Depends(get_optional_active_user)):
    """The flyers this person may see now (none when logged out), and the slot's rules."""
    return await svc.for_viewer(await _db(), current_user, clip_topic=clip_topic)


@router.post("/event")
async def event(body: EventIn, me: str = Depends(get_current_active_user)):
    return {"counted": await svc.record(await _db(), me, body.flyer_id[:40], body.kind)}
