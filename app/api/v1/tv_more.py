"""Wano TV round 3 for the app (see app/services/tv_more.py). Every route is
behind its own switch (404 while off)."""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_active_user, get_optional_active_user
from app.core.database import get_database
from app.services import tv_more

router = APIRouter()


@router.get("/me")
async def me(current_user: str = Depends(get_current_active_user)):
    """Titles I put a heart on, and Coming soon titles I asked to be reminded about."""
    return await tv_more.mine(get_database(), current_user)


@router.put("/love/{title_id}")
async def love(title_id: str, current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await tv_more.require(db, "tv_loves")
    return await tv_more.love(db, current_user, title_id, True)


@router.delete("/love/{title_id}")
async def unlove(title_id: str, current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await tv_more.require(db, "tv_loves")
    return await tv_more.love(db, current_user, title_id, False)


@router.get("/coming-soon")
async def coming_soon(current_user: Optional[str] = Depends(get_optional_active_user)):
    return {"items": await tv_more.coming_soon(get_database(), current_user)}


@router.put("/remind/{title_id}")
async def remind(title_id: str, current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await tv_more.require(db, "tv_coming_soon")
    return await tv_more.remind(db, current_user, title_id, True)


@router.delete("/remind/{title_id}")
async def unremind(title_id: str, current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await tv_more.require(db, "tv_coming_soon")
    return await tv_more.remind(db, current_user, title_id, False)


@router.get("/clips")
async def clips():
    """Feed posts that are clips of a Wano TV title: {video_id: {title_id, title}}."""
    return {"clips": await tv_more.clips(get_database())}


@router.get("/download/{title_id}")
async def download(title_id: str, episode: Optional[str] = Query(None, pattern=r"^s\d{1,3}e\d{1,4}$"),
                   current_user: str = Depends(get_current_active_user)):
    """A short-lived link to the film's offline copy (MP4)."""
    return await tv_more.download(get_database(), current_user, title_id, episode)
