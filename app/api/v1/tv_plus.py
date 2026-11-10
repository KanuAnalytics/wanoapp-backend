"""Wano TV extras (tv_plus switch): where you stopped watching (Continue
watching, Resume) and My List. The catalogue itself stays in tv.py."""

from datetime import datetime, timedelta
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt
from typing import Union

from app.api.deps import get_current_active_user
from app.api.v1.tv import _has_episode, _title_object_id
from app.core.database import get_database
from app.services import creator_push

router = APIRouter()

CONTINUE_LIMIT = 20
LIST_LIMIT = 200
# Under this you've barely started; past DONE_AT of the runtime (or the last
# minute) you've finished: neither belongs in Continue watching.
MIN_SECONDS = 20
DONE_AT = 0.93
KEEP_DAYS = 120

Num = Union[StrictInt, StrictFloat]


class ProgressIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: Num = Field(ge=0, le=12 * 3600)
    duration: Num = Field(gt=0, le=12 * 3600)
    episode: Optional[str] = Field(None, pattern=r"^s\d{1,3}e\d{1,4}$")


async def _ready(db):
    await creator_push.require(db, "tv_plus")


async def _title(db, title_id: str) -> dict:
    oid = _title_object_id(title_id)
    doc = await db.tv_titles.find_one({"_id": oid, "is_active": True}, {"_id": 1, "seasons": 1})
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Title not found")
    return doc


def _done(position: float, duration: float) -> bool:
    return position >= duration * DONE_AT or duration - position <= 60 and duration > 180


@router.get("/me")
async def my_tv(current_user: str = Depends(get_current_active_user)):
    """{continue: [{title_id, episode, position, duration, done, updated_at}], list: [title_id]}.
    Continue holds unfinished titles and series whose last episode you finished
    (the app moves you to the next one), newest first."""
    db = get_database()
    await _ready(db)
    me = ObjectId(current_user)
    since = datetime.utcnow() - timedelta(days=KEEP_DAYS)
    rows = await db.tv_progress.find({"user_id": me, "updated_at": {"$gte": since}}) \
        .sort("updated_at", -1).limit(CONTINUE_LIMIT * 3).to_list(None)
    live = {d["_id"] async for d in db.tv_titles.find(
        {"_id": {"$in": [r["title_id"] for r in rows]}, "is_active": True}, {"_id": 1})}
    cont = []
    for r in rows:
        if r["title_id"] not in live:
            continue
        # A finished film is done; a finished episode stays (the app offers the next one).
        if r.get("done") and not r.get("episode"):
            continue
        cont.append({"title_id": str(r["title_id"]), "episode": r.get("episode"),
                     "position": r.get("position", 0), "duration": r.get("duration", 0),
                     "done": bool(r.get("done")), "updated_at": r.get("updated_at")})
        if len(cont) >= CONTINUE_LIMIT:
            break
    lst = await db.tv_list.find({"user_id": me}).sort("added_at", -1).limit(LIST_LIMIT).to_list(None)
    alive = {d["_id"] async for d in db.tv_titles.find(
        {"_id": {"$in": [x["title_id"] for x in lst]}, "is_active": True}, {"_id": 1})}
    return {"continue": cont, "list": [str(x["title_id"]) for x in lst if x["title_id"] in alive]}


@router.put("/progress/{title_id}")
async def save_progress(title_id: str, body: ProgressIn, current_user: str = Depends(get_current_active_user)):
    """Called every few seconds while playing and on pause/exit."""
    db = get_database()
    await _ready(db)
    doc = await _title(db, title_id)
    if body.episode and not _has_episode(doc, body.episode):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Episode not found")
    position = float(min(body.position, body.duration))
    duration = float(body.duration)
    me = ObjectId(current_user)
    key = {"user_id": me, "title_id": doc["_id"]}
    if position < MIN_SECONDS:
        # Just started: don't push something you only opened into Continue watching,
        # but don't wipe progress you made on another episode either.
        return {"saved": False}
    done = _done(position, duration)
    await db.tv_progress.update_one(key, {"$set": {
        "episode": body.episode, "position": position, "duration": duration, "done": done,
        "updated_at": datetime.utcnow()}}, upsert=True)
    return {"saved": True, "done": done}


@router.delete("/progress/{title_id}")
async def drop_progress(title_id: str, current_user: str = Depends(get_current_active_user)):
    """"Remove from Continue watching"."""
    db = get_database()
    await _ready(db)
    oid = _title_object_id(title_id)
    await db.tv_progress.delete_one({"user_id": ObjectId(current_user), "title_id": oid})
    return {"removed": True}


@router.put("/list/{title_id}")
async def add_to_list(title_id: str, current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await _ready(db)
    doc = await _title(db, title_id)
    me = ObjectId(current_user)
    if await db.tv_list.count_documents({"user_id": me}, limit=LIST_LIMIT) >= LIST_LIMIT and \
            not await db.tv_list.find_one({"user_id": me, "title_id": doc["_id"]}, {"_id": 1}):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="My List is full")
    await db.tv_list.update_one({"user_id": me, "title_id": doc["_id"]},
                                {"$setOnInsert": {"added_at": datetime.utcnow()}}, upsert=True)
    return {"in_list": True}


@router.delete("/list/{title_id}")
async def remove_from_list(title_id: str, current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await _ready(db)
    oid = _title_object_id(title_id)
    await db.tv_list.delete_one({"user_id": ObjectId(current_user), "title_id": oid})
    return {"in_list": False}
