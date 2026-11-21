"""wanoafrica.com's public pages (see app/services/public_site.py). No login."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.database import get_database
from app.services import public_site as ps

router = APIRouter()


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Send the form as JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Send the form as JSON")
    return body


def _errors(fn):
    async def run(*a, **kw):
        try:
            return await fn(*a, **kw)
        except ps.Invalid as e:
            raise HTTPException(status_code=400, detail=str(e))
        except ps.TooMany:
            raise HTTPException(status_code=429, detail="Too many tries. Please try again in an hour.")
    return run


@router.get("/creators/{username}")
async def creator(username: str):
    out = await ps.creator(get_database(), username[:ps.USERNAME_MAX + 5])
    if not out:
        raise HTTPException(status_code=404, detail="Not found")
    return out


@router.get("/videos/{video_id}")
async def video(video_id: str):
    out = await ps.video(get_database(), video_id[:30])
    if not out:
        raise HTTPException(status_code=404, detail="Not found")
    return out


@router.get("/sitemap-videos")
async def sitemap_videos(skip: int = Query(0, ge=0, le=100000), limit: int = Query(50, ge=1, le=50)):
    return {"items": await ps.sitemap_videos(get_database(), skip, limit)}


@router.get("/creators")
async def creators_index():
    return {"items": await ps.creators_index(get_database())}


@router.get("/trending")
async def trending(country: Optional[str] = Query(None, pattern=r"^[A-Za-z]{2}$")):
    return await ps.trending(get_database(), country)


@router.get("/cities/{city_id}")
async def city(city_id: str):
    out = await ps.city(get_database(), city_id[:40])
    if not out:
        raise HTTPException(status_code=404, detail="Not found")
    return out


@router.post("/newsletter")
async def subscribe(request: Request):
    return await _errors(ps.subscribe)(get_database(), await _json(request))


@router.post("/newsletter/unsubscribe")
async def unsubscribe(request: Request):
    body = await _json(request)
    if not await ps.unsubscribe(get_database(), body.get("token")):
        raise HTTPException(status_code=404, detail="That link has expired or was already used")
    return {"unsubscribed": True}


@router.post("/enquiries")
async def enquiry(request: Request):
    return await _errors(ps.enquiry)(get_database(), await _json(request))


@router.post("/account-deletion")
async def account_deletion(request: Request):
    return await _errors(ps.deletion_request)(get_database(), await _json(request))


@router.get("/topics")
async def topics():
    return {"items": await ps.topics(get_database())}


@router.get("/topics/{cat}")
async def topic(cat: str, country: Optional[str] = Query(None, pattern=r"^[A-Za-z]{2}$")):
    out = await ps.topic(get_database(), cat[:40], country)
    if not out:
        raise HTTPException(status_code=404, detail="Not found")
    return out


@router.get("/tags")
async def tags():
    return {"items": await ps.tags(get_database())}


@router.get("/tags/{tag}")
async def tag(tag: str):
    out = await ps.tag(get_database(), tag[:60])
    if not out:
        raise HTTPException(status_code=404, detail="Not found")
    return out


@router.get("/sounds")
async def sounds():
    return {"items": await ps.sounds(get_database())}


@router.get("/sounds/{sound_id}")
async def sound(sound_id: str):
    out = await ps.sound(get_database(), sound_id[:70])
    if not out:
        raise HTTPException(status_code=404, detail="Not found")
    return out
