"""Film submissions sent by wanoafrica.com/tv/submit (see services/film_submissions)."""

from fastapi import APIRouter, HTTPException, Request

from app.core.database import get_database
from app.services import film_submissions as fs

router = APIRouter()


@router.post("")
async def submit(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Send the form as JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Send the form as JSON")
    who = body.get("who") if isinstance(body.get("who"), str) else ""
    try:
        return await fs.create(get_database(), body, who)
    except fs.Invalid as e:
        raise HTTPException(status_code=400, detail=str(e))
    except fs.TooMany:
        raise HTTPException(status_code=429, detail="Too many submissions. Please try again in an hour.")
