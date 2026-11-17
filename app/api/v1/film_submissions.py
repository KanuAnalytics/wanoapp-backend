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


_status_tries: dict = {}


@router.post("/status")
async def status(request: Request):
    """{ref, email, who} -> where the film is. Both must match; a sender gets 20 tries an hour."""
    import time
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Send the form as JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Send the form as JSON")
    who = body.get("who") if isinstance(body.get("who"), str) else ""
    now = time.monotonic()
    tries = [t for t in _status_tries.get(who[:64], []) if now - t < 3600]
    if who and len(tries) >= 20:
        raise HTTPException(status_code=429, detail="Too many tries. Please try again in an hour.")
    if who:
        _status_tries[who[:64]] = tries + [now]
        if len(_status_tries) > 20000:
            _status_tries.clear()
    out = await fs.public_status(get_database(), str(body.get("ref") or ""), str(body.get("email") or ""))
    if not out:
        raise HTTPException(status_code=404, detail="We couldn't find a submission with that reference and email.")
    return out
