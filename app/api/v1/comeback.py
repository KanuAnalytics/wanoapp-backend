"""Comeback reminders (app/services/comeback.py, "comeback" switch)."""

from fastapi import APIRouter, Depends

from app.api.deps import get_current_active_user
from app.core.database import get_database
from app.services import comeback

router = APIRouter()


@router.post("/open")
async def opened(me: str = Depends(get_current_active_user)):
    """App 2.3.61 says it was opened (at most once a day). Off: nothing saved."""
    return await comeback.opened(get_database(), me)
