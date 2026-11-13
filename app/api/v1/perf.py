"""Speed timings from the app (app/services/perf.py). Behind the "perf" switch."""

import time
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt
from typing import Union

from app.core.database import get_database
from app.services import creator_push, perf

router = APIRouter()

PER_MINUTE = 20
_hits: dict = {}


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal["first_video", "video_start", "stall", "feed_load", "swipe_lag"]
    ms: Union[StrictInt, StrictFloat] = Field(ge=0, le=perf.MAX_MS)


class Batch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: List[Event] = Field(max_length=perf.MAX_EVENTS)
    tz: Optional[str] = Field(None, max_length=64)
    net: Literal["wifi", "cellular", "unknown"] = "unknown"


def _allowed(request: Request) -> bool:
    from app.api.v1.videos import _client_ip
    ip = _client_ip(request) or "?"
    minute = int(time.time() // 60)
    if len(_hits) > 50_000:
        _hits.clear()
    k = (ip, minute)
    _hits[k] = _hits.get(k, 0) + 1
    return _hits[k] <= PER_MINUTE


@router.post("")
async def send_timings(body: Batch, request: Request):
    """No sign-in needed; nothing personal is stored (see app/services/perf.py)."""
    db = get_database()
    await creator_push.require(db, "perf")
    if not _allowed(request):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many")
    n = await perf.record(db, [e.model_dump() for e in body.events], body.tz, body.net)
    return {"recorded": n}
