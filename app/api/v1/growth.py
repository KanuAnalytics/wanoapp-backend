"""Installs from shared links (wa_share switch, app/services/wa_growth.py)."""

from typing import Optional

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core.database import get_database
from app.services import wa_growth

router = APIRouter()


class Install(BaseModel):
    model_config = ConfigDict(extra="ignore")
    referrer: str = Field(default="", max_length=1000)


@router.post("/install")
async def install(body: Install, request: Request, x_device_id: Optional[str] = Header(None, max_length=100)):
    """App 2.3.62's first launch on Android sends Play's install referrer. Only
    referrers from Wano share links count, once per device, at most 30 a day
    from one connection. No sign-in needed."""
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    network = fwd or (request.client.host if request.client else "")
    return {"counted": await wa_growth.installed(get_database(), x_device_id or "", body.referrer, network=network)}
