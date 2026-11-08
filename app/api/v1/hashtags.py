"""Hashtag suggestions, the library and video ideas (app/services/hashtags.py).
Public; logged out apps send authenticated=false."""

from typing import Optional

from fastapi import APIRouter, Query

from app.core.database import get_database
from app.services import hashtags

router = APIRouter()


@router.get("/suggest")
async def suggest(q: Optional[str] = Query(None, max_length=200), limit: int = Query(20, ge=1, le=20)):
    """Tags starting with q (or popular ones when q is empty): live challenges,
    then trending, then the library."""
    return await hashtags.suggest(get_database(), q, limit)


@router.get("/library")
async def library():
    return hashtags.library()


@router.get("/ideas")
async def ideas(lang: Optional[str] = Query(None, max_length=10)):
    return hashtags.ideas(lang)
