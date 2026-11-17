"""Taps on wanoafrica.com/get?by=<name>, reported by the website."""

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.core.database import get_database
from app.services import get_links

router = APIRouter()


class Tap(BaseModel):
    model_config = ConfigDict(extra="ignore")
    by: Optional[str] = Field(default="", max_length=80)
    platform: Optional[str] = Field(default="other", max_length=10)
    who: Optional[str] = Field(default="", max_length=128)   # the website's hash of the phone's IP + browser


@router.post("/tap")
async def tap(body: Tap):
    counted = await get_links.record(get_database(), body.by, body.platform, body.who or "")
    return {"counted": counted}
