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


class TvCaption(BaseModel):
    """A subtitle track inside the film's HLS stream (see scripts/add_hls_subtitles.py)."""
    language: str = Field(..., description="BCP 47 code, e.g. en")
    label: str
    url: str


class TvProfile(BaseModel):
    """The Wano account behind a credit (public fields only)."""
    id: str
    username: str
    display_name: Optional[str] = None
    profile_picture: Optional[str] = None


class TvCreditPerson(BaseModel):
    name: str
    role: str
    profile: Optional[TvProfile] = None


class TvCredits(BaseModel):
    director: str
    cast: List[str] = Field(default_factory=list)
    production: str
    distributor: str
    people: List[TvCreditPerson] = Field(default_factory=list)


class TvEpisode(BaseModel):
    id: str = Field(..., pattern=r"^s\d{1,3}e\d{1,4}$")
    season: int = Field(..., ge=0)
    number: int = Field(..., ge=0)
    title: str
    synopsis: str = ""
    duration_minutes: int = Field(..., ge=0)
    thumbnail: Optional[str] = None
    video_url: str
    captions: List[TvCaption] = Field(default_factory=list)


class TvSeason(BaseModel):
    number: int = Field(..., ge=0)
    title: Optional[str] = None
    episodes: List[TvEpisode]


class TvMaker(BaseModel):
    """
    A Wano user credited on a title, shown under "Meet the Makers". The title
    stores only `user_id` and `role`; the rest is read from the user's account
    when the catalogue is served, so it follows profile changes.
    """
    user_id: str
    username: str
    display_name: Optional[str] = None
    profile_picture: Optional[str] = None
    role: str = Field(..., description="e.g. Filmmaker, Featured in film")


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
    captions: List[TvCaption] = Field(default_factory=list)
    # Series: seasons in order, each with its episodes in order. video_url is
    # then the first episode. Empty for movies.
    seasons: List[TvSeason] = Field(default_factory=list)
    episode_count: int = 0
    # Credited people who have a Wano account ("Meet the Makers" in the app).
    makers: List[dict] = Field(default_factory=list)
    # History only: the episode the viewer last started.
    last_episode: Optional[str] = None
    # Wano users credited on the title ("Meet the Makers").
    makers: List[TvMaker] = Field(default_factory=list)


class TvGenreResponse(BaseModel):
    name: str
    image: str


class TvWatchResponse(BaseModel):
    title_id: str
    watched_at: datetime
    episode: Optional[str] = None
