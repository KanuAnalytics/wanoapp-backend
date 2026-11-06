"""
Weekly creator leaderboard (see app/services/leaderboard.py).

GET /api/v1/leaderboard/weekly?week=current|last&country=gh&limit=50
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_optional_active_user
from app.core.database import get_database
from app.services import leaderboard

router = APIRouter()


@router.get("/weekly")
async def weekly(
    week: Literal["current", "last"] = "current",
    country: Optional[str] = Query(None, pattern=r"^[A-Za-z]{2}$"),
    limit: int = Query(50, ge=1, le=100),
    current_user: Optional[str] = Depends(get_optional_active_user),
):
    """Top creators this week (or last), worldwide or in one country, plus the
    viewer's own place when they are on it. Logged out: send authenticated=false."""
    db = get_database()
    start = leaderboard.week_start(leaderboard._utcnow(), back=1 if week == "last" else 0)
    rows = await leaderboard.ranking(db, start, country.lower() if country else None)
    me = next((r for r in rows if current_user and r["user"]["id"] == current_user), None)
    return {"week_start": start.isoformat() + "Z", "entries": rows[:limit], "me": me}
