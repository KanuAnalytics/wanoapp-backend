"""In-house messaging v1 (app/services/messages.py). Behind the "messages" switch."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
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
    reply_to: Optional[StrictStr] = None


class ReactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emoji: Optional[StrictStr] = None


class EditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: StrictStr


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


async def _db_with(key: str):
    db = await _db()
    await creator_push.require(db, key)
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
                       after: Optional[str] = Query(None, max_length=40),
                       since: Optional[str] = Query(None, max_length=40),
                       me: str = Depends(get_current_active_user)):
    return await svc.page(await _db(), me, cid, limit, before, after, since)


@router.post("/conversations/{cid}/messages", status_code=201)
async def send_message(cid: str, body: SendIn, response: Response, me: str = Depends(get_current_active_user)):
    msg, created = await svc.send(await _db(), me, cid, body.text, body.post_id, body.client_id, reply_to=body.reply_to)
    if not created:
        response.status_code = 200
    return msg


@router.post("/conversations/{cid}/voice", status_code=201)
async def send_voice(cid: str, response: Response, file: UploadFile = File(...), duration_ms: int = Form(...),
                     client_id: Optional[str] = Form(None, max_length=64), reply_to: Optional[str] = Form(None, max_length=40),
                     me: str = Depends(get_current_active_user)):
    db = await _db_with("msg_voice")
    data = await file.read(svc.VOICE_MAX_BYTES + 1)      # never more than this is read into memory
    if len(data) > svc.VOICE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="That voice message is too long")
    msg, created = await svc.send_voice(db, me, cid, data, duration_ms, client_id, reply_to)
    if not created:
        response.status_code = 200
    return msg


@router.post("/messages/{mid}/react")
async def react(mid: str, body: ReactIn, me: str = Depends(get_current_active_user)):
    return await svc.react(await _db_with("msg_plus"), me, mid, body.emoji)


@router.patch("/messages/{mid}")
async def edit_message(mid: str, body: EditIn, me: str = Depends(get_current_active_user)):
    return await svc.edit(await _db_with("msg_plus"), me, mid, body.text[:4000])


@router.post("/messages/{mid}/unsend")
async def unsend(mid: str, me: str = Depends(get_current_active_user)):
    return await svc.unsend(await _db_with("msg_plus"), me, mid)


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
