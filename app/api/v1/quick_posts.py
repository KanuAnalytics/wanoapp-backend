"""Quick posts: Wano Voice notes and Status cards on the ring around a
creator's photo (app/services/quick_posts.py). Behind the "quick_posts" switch."""

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, StrictStr

from app.api.deps import get_current_active_user
from app.core.database import get_database
from app.services import creator_push, quick_posts as svc

router = APIRouter()


class CardIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: StrictStr
    color: Literal["green", "night", "sunset", "ocean", "plum", "earth"] = "green"


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Literal["spam", "harassment", "hate", "scam", "nudity", "violence", "self_harm",
                    "false_information", "other"]


async def _db():
    db = get_database()
    await creator_push.require(db, "quick_posts")
    return db


@router.get("/rings")
async def rings(ids: str = Query(..., max_length=60 * 25), me: str = Depends(get_current_active_user)):
    """ids: comma-separated user ids on screen. Only people with something to see come back."""
    return {"rings": await svc.rings(await _db(), me, ids.split(","))}


@router.get("/users/{uid}")
async def for_user(uid: str, me: str = Depends(get_current_active_user)):
    return await svc.for_user(await _db(), me, uid[:40])


@router.post("/card", status_code=201)
async def make_card(body: CardIn, me: str = Depends(get_current_active_user)):
    return await svc.make_card(await _db(), me, body.text[:2000], body.color)


@router.post("/voice", status_code=201)
async def make_voice(file: UploadFile = File(...), duration_ms: int = Form(...),
                     me: str = Depends(get_current_active_user)):
    db = await _db()
    data = await file.read(svc.VOICE_MAX_BYTES + 1)     # never more than this is read into memory
    if len(data) > svc.VOICE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="That voice note is too big")
    return await svc.make_voice(db, me, data, duration_ms)


@router.post("/{qid}/seen")
async def seen(qid: str, me: str = Depends(get_current_active_user)):
    return await svc.mark_seen(await _db(), me, qid[:40])


@router.post("/{qid}/report")
async def report(qid: str, body: ReportIn, me: str = Depends(get_current_active_user)):
    return await svc.report(await _db(), me, qid[:40], body.reason)


@router.delete("/{qid}")
async def delete(qid: str, me: str = Depends(get_current_active_user)):
    # Deleting your own works even with the switch off, so nothing gets stuck up.
    return await svc.delete(get_database(), me, qid[:40])
