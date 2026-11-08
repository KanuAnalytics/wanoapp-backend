"""In-house messaging v1 (app/services/messages.py). Behind the "messages" switch."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from app.api.deps import get_current_active_user
from app.core.database import get_database
from app.services import creator_push, messages as svc

router = APIRouter()


class OpenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: StrictStr


class SendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Optional[StrictStr] = None
    post_id: Optional[StrictStr] = None
    client_id: Optional[StrictStr] = None


class MuteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    muted: StrictBool


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_requests: StrictBool


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Literal["spam", "harassment", "hate", "scam", "nudity", "violence", "self_harm", "other"]


async def _db():
    db = get_database()
    await creator_push.require(db, "messages")
    return db


@router.post("/conversations")
async def open_conversation(body: OpenIn, me: str = Depends(get_current_active_user)):
    return await svc.open_conversation(await _db(), me, body.user_id)


@router.get("/conversations")
async def list_conversations(folder: Literal["inbox", "requests"] = "inbox", cursor: Optional[str] = Query(None, max_length=80),
                             me: str = Depends(get_current_active_user)):
    return await svc.inbox(await _db(), me, folder, cursor)


@router.get("/unread")
async def unread(me: str = Depends(get_current_active_user)):
    return await svc.unread(await _db(), me)


@router.get("/conversations/{cid}/messages")
async def get_messages(cid: str, limit: int = Query(30, ge=1, le=100), before: Optional[str] = Query(None, max_length=40),
                       me: str = Depends(get_current_active_user)):
    return await svc.page(await _db(), me, cid, limit, before)


@router.post("/conversations/{cid}/messages", status_code=201)
async def send_message(cid: str, body: SendIn, response: Response, me: str = Depends(get_current_active_user)):
    msg, created = await svc.send(await _db(), me, cid, body.text, body.post_id, body.client_id)
    if not created:
        response.status_code = 200
    return msg


@router.post("/conversations/{cid}/read")
async def read(cid: str, me: str = Depends(get_current_active_user)):
    return await svc.mark_read(await _db(), me, cid)


@router.post("/conversations/{cid}/typing")
async def typing(cid: str, me: str = Depends(get_current_active_user)):
    return await svc.typing(await _db(), me, cid)


@router.post("/conversations/{cid}/accept")
async def accept(cid: str, me: str = Depends(get_current_active_user)):
    return await svc.accept(await _db(), me, cid)


@router.post("/conversations/{cid}/decline")
async def decline(cid: str, me: str = Depends(get_current_active_user)):
    return await svc.decline(await _db(), me, cid)


@router.post("/conversations/{cid}/mute")
async def mute(cid: str, body: MuteIn, me: str = Depends(get_current_active_user)):
    return await svc.mute(await _db(), me, cid, body.muted)


@router.delete("/conversations/{cid}")
async def delete_conversation(cid: str, me: str = Depends(get_current_active_user)):
    return await svc.clear(await _db(), me, cid)


@router.get("/settings")
async def get_settings(me: str = Depends(get_current_active_user)):
    return await svc.get_settings(await _db(), me)


@router.put("/settings")
async def put_settings(body: SettingsIn, me: str = Depends(get_current_active_user)):
    return await svc.put_settings(await _db(), me, body.allow_requests)


@router.post("/messages/{mid}/report", status_code=201)
async def report(mid: str, body: ReportIn, response: Response, me: str = Depends(get_current_active_user)):
    out, created = await svc.report(await _db(), me, mid, body.reason)
    if not created:
        response.status_code = 200
    return out
