"""
app/models/music.py

"""


from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, HttpUrl
from app.models.base import BaseDocument, PyObjectId

class Music(BaseDocument):
    """Music model for artist uploads"""
    artist_id: PyObjectId
    title: str = Field(..., min_length=1, max_length=200)
    artist_name: str
    album: Optional[str] = Field(None, max_length=200)
    genre: List[str] = Field(default_factory=list, max_length=5)
    
    # Files
    audio_url: HttpUrl
    cover_art_url: Optional[HttpUrl] = None
    waveform_url: Optional[HttpUrl] = None
    
    # Metadata
    duration: float = Field(..., gt=0, le=600)
    bpm: Optional[int] = Field(None, gt=0, le=300)
    key: Optional[str] = None
    
    # Licensing
    is_copyrighted: bool = Field(default=True)
    license_type: Optional[str] = None
    usage_rights: Dict[str, Any] = Field(default_factory=dict)
    
    # Moderation
    is_approved: bool = Field(default=False)
    is_explicit: bool = Field(default=False)
    approval_notes: Optional[str] = None
    
    # Usage stats
    usage_count: int = Field(default=0, ge=0)
    featured_in_videos: List[PyObjectId] = Field(default_factory=list)