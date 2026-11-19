"""Quick posts: Wano Voice notes and Status cards on the ring around a
creator's photo (app/services/quick_posts.py). Behind the "quick_posts" switch."""

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from app.api.deps import get_current_active_user
from app.core.database import get_database
from app.services import creator_push, quick_posts as svc

router = APIRouter()


class CardIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: StrictStr
    color: Literal["green", "night", "sunset", "ocean", "plum", "earth"] = "green"
    comments: StrictBool = False


class CommentsOnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: StrictBool


class CommentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: StrictStr


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
    db = await _db()
    comments = body.comments and await creator_push.on(db, "quick_replies")
    return await svc.make_card(db, me, body.text[:2000], body.color, comments=comments)


@router.post("/voice", status_code=201)
async def make_voice(file: UploadFile = File(...), duration_ms: int = Form(...), comments: bool = Form(False),
                     me: str = Depends(get_current_active_user)):
    db = await _db()
    data = await file.read(svc.VOICE_MAX_BYTES + 1)     # never more than this is read into memory
    if len(data) > svc.VOICE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="That voice note is too big")
    comments = comments is True and await creator_push.on(db, "quick_replies")
    return await svc.make_voice(db, me, data, duration_ms, comments=comments)


async def _replies_db():
    db = await _db()
    await creator_push.require(db, "quick_replies")
    return db


@router.get("/{qid}/comments")
async def comments(qid: str, me: str = Depends(get_current_active_user)):
    return await svc.comments(await _replies_db(), me, qid[:40])


@router.post("/{qid}/comments", status_code=201)
async def add_comment(qid: str, body: CommentIn, me: str = Depends(get_current_active_user)):
    return await svc.add_comment(await _replies_db(), me, qid[:40], body.text[:1000])


@router.post("/{qid}/comments-on")
async def comments_on(qid: str, body: CommentsOnIn, me: str = Depends(get_current_active_user)):
    return await svc.set_comments(await _replies_db(), me, qid[:40], body.on)


@router.delete("/comments/{cid}")
async def delete_comment(cid: str, me: str = Depends(get_current_active_user)):
    # Works with the switch off too, so nothing gets stuck up.
    return await svc.delete_comment(get_database(), me, cid[:40])


@router.post("/comments/{cid}/report")
async def report_comment(cid: str, body: ReportIn, me: str = Depends(get_current_active_user)):
    return await svc.report_comment(await _replies_db(), me, cid[:40], body.reason)


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
