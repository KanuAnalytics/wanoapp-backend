"""The Wano Team thread (app/services/team.py, "wano_team" switch) and the
shared attention limit (app/services/attention.py, "attention_cap")."""

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.api.deps import get_current_active_user
from app.core.database import get_database
from app.services import attention, staff as staff_svc, team

router = APIRouter()


async def _db():
    db = get_database()
    await team.require_on(db)
    return db


@router.get("/thread")
async def thread(platform: Optional[Literal["android", "ios"]] = Query(None), lang: Optional[str] = Query(None, max_length=10),
                 me: str = Depends(get_current_active_user)):
    return await team.thread(await _db(), me, platform, lang)


@router.post("/thread/read")
async def read(me: str = Depends(get_current_active_user)):
    return await team.mark_read(await _db(), me)


class MuteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    muted: StrictBool


@router.post("/thread/mute")
async def mute(body: MuteIn, me: str = Depends(get_current_active_user)):
    return await team.mute(await _db(), me, body.muted)


class PostIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., max_length=200)
    body: str = Field(..., max_length=2000)
    kind: Literal["update", "tip", "safety", "event"] = "update"
    image: Optional[str] = Field(None, max_length=1024)
    alt: Optional[str] = Field(None, max_length=300)
    audience: Optional[Dict[str, Any]] = None


@router.post("/posts")
async def post(body: PostIn, s=Depends(staff_svc.need("owner"))):
    """Only owners post as Wano Team."""
    return await team.post(await _db(), s, body.model_dump())


@router.get("/posts")
async def posts(s=Depends(staff_svc.need("admin"))):
    db = await _db()
    rows = await db.team_posts.find({"active": True}).sort("at", -1).limit(50).to_list(50)
    return {"items": [{"id": str(p["_id"]), "title": p.get("title"), "body": p.get("body"), "kind": p.get("kind"),
                       "image": p.get("image"), "audience": p.get("audience"), "at": p.get("at")} for p in rows],
            "account": bool(await team.team_account(db))}


@router.delete("/posts/{pid}")
async def remove(pid: str, s=Depends(staff_svc.need("owner"))):
    return await team.remove_post(await _db(), s, pid[:40])


class TakeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["whats_new", "tv_strip", "flyer", "quick_ring", "review_prompt"]
    tz: int = Field(0, ge=-720, le=840)


@router.post("/attention/take")
async def take(body: TakeIn, me: str = Depends(get_current_active_user)):
    """May this pop-up show now? (Always yes while attention_cap is off.)"""
    return {"ok": await attention.take(get_database(), me, body.kind, body.tz)}
