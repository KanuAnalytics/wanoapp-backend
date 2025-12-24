from typing import Optional
from pydantic import BaseModel


class CheckStatusReq(BaseModel):
    """Request model for checking video status"""
    uId: str
    videoId: str