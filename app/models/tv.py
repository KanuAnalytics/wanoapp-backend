"""
Wano TV catalogue models

app/models/tv.py
"""

from datetime import date, datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class TvTitleType(str, Enum):
    MOVIE = "movie"
    SERIES = "series"


class TvTrailer(BaseModel):
    duration_seconds: int = Field(..., ge=0)
    thumbnail: str
    video_url: Optional[str] = None


class TvCredits(BaseModel):
    director: str
    cast: List[str] = Field(default_factory=list)
    production: str
    distributor: str


class TvTitleResponse(BaseModel):
    """A movie or series as the app shows it."""
    id: str
    slug: str
    title: str
    type: TvTitleType
    genres: List[str]
    year: int
    release_date: date
    duration_minutes: int = Field(..., ge=0)
    age_rating: str
    description: str
    synopsis: str
    trailer: TvTrailer
    credits: TvCredits
    poster: str
    backdrop: str
    video_url: str
    featured: bool = False
    popularity: int = 0


class TvWatchResponse(BaseModel):
    title_id: str
    watched_at: datetime
