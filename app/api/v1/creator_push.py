"""Wano Picks, weekly missions, creator stats and trial posts
(app/services/creator_push.py). Each behind its own switch."""

from typing import Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_current_active_user, get_optional_active_user
from app.core.database import get_database
from app.services import creator_push as cp

router = APIRouter()


@router.get("/picks")
async def picks(current_user: Optional[str] = Depends(get_optional_active_user)):
    db = get_database()
    await cp.require(db, "picks")
    return {"items": await cp.current_picks(db)}


@router.get("/missions")
async def missions(current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await cp.require(db, "missions")
    rows = await cp.mission_progress(db, ObjectId(current_user))
    return {"week_start": cp._week_start().strftime("%Y-%m-%d"), "items": rows}


@router.post("/missions/{key}/claim")
async def claim(key: str, current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await cp.require(db, "missions")
    return await cp.claim(db, ObjectId(current_user), key)


@router.get("/stats")
async def stats(days: int = Query(7), current_user: str = Depends(get_current_active_user)):
    if days not in (7, 28):
        raise HTTPException(status_code=422, detail="days must be 7 or 28")
    db = get_database()
    await cp.require(db, "creator_stats")
    return await cp.stats(db, ObjectId(current_user), days)


@router.get("/trials")
async def trials(current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await cp.require(db, "trial_posts")
    return {"items": await cp.my_trials(db, ObjectId(current_user))}


@router.post("/trial/{video_id}/share")
async def share_trial(video_id: str, current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await cp.require(db, "trial_posts")
    return await cp.share_trial(db, ObjectId(current_user), video_id)


# ---------------------------------------------------------------- @mentions

@router.get("/mentions/suggest")
async def mention_suggest(q: str = Query("", max_length=40),
                          current_user: Optional[str] = Depends(get_optional_active_user)):
    from app.services import mentions
    db = get_database()
    await cp.require(db, "mentions")
    return {"items": await mentions.suggest(db, q, current_user)}


@router.get("/mentions/resolve")
async def mention_resolve(username: str = Query(..., max_length=40),
                          current_user: Optional[str] = Depends(get_optional_active_user)):
    from app.services import mentions
    db = get_database()
    await cp.require(db, "mentions")
    u = await mentions.by_username(db, username.lstrip("@").rstrip("."))
    if not u:
        raise HTTPException(status_code=404, detail="No one with that name")
    return {"id": str(u["_id"]), "username": u.get("username")}
