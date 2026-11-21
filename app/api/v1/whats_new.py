"""What's new (app/services/whats_new.py). Behind the "whats_new" switch;
the new looks, banners, tips and update notes also need "whats_new_plus"."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_current_active_user
from app.core.database import get_database
from app.services import creator_push, whats_new as svc

router = APIRouter()


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["shown", "tap", "close", "not_now"]
    tz: int = Field(0, ge=-720, le=840)       # the phone's UTC offset in minutes (local day)


async def _db():
    db = get_database()
    await creator_push.require(db, "whats_new")
    return db


@router.get("/next")
async def next_one(app: Optional[str] = Query(None, max_length=20),
                   platform: Optional[Literal["android", "ios"]] = Query(None),
                   lang: Optional[str] = Query(None, max_length=10),
                   tz: int = Query(0, ge=-720, le=840),
                   me: str = Depends(get_current_active_user)):
    return await svc.next_for(await _db(), me, app, platform=platform, lang=lang, tz=tz)


@router.get("")
async def recent(app: Optional[str] = Query(None, max_length=20), me: str = Depends(get_current_active_user)):
    return {"items": await svc.recent(await _db(), app, me)}


@router.get("/banners")
async def banners(app: Optional[str] = Query(None, max_length=20), me: str = Depends(get_current_active_user)):
    return {"items": await svc.special(await _db(), me, app, "banner")}


@router.get("/tips")
async def tips(app: Optional[str] = Query(None, max_length=20), me: str = Depends(get_current_active_user)):
    return {"items": await svc.special(await _db(), me, app, "tip")}


@router.get("/updated")
async def updated(app: Optional[str] = Query(None, max_length=20), me: str = Depends(get_current_active_user)):
    """The notes for exactly this app version, once."""
    items = await svc.special(await _db(), me, app, "update_notes")
    return {"item": items[0] if items else None}


@router.post("/{aid}/event")
async def event(aid: str, body: EventIn, me: str = Depends(get_current_active_user)):
    db = await _db()
    if body.kind == "shown":
        return await svc.shown(db, me, aid[:40], tz=body.tz)
    return await svc.event(db, me, aid[:40], body.kind)
