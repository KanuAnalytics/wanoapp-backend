"""
app/models/video.py

"""


from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator
from app.models.base import BaseDocument, VideoType, VideoOrientation, VideoPrivacy, PyObjectId

class VideoDraft(BaseModel):
    """Video draft data model"""
    timeline: Dict[str, Any]
    effects: List[Dict[str, Any]]
    audio_tracks: List[Dict[str, Any]]
    text_overlays: List[Dict[str, Any]]
    filters: List[str]
    transitions: List[Dict[str, Any]]
    thumbnail_timestamp: Optional[float] = None
    version: int = Field(default=1)

class VideoMetadata(BaseModel):
    """Video metadata model"""
    duration: float = Field(..., gt=0, le=600)
    width: int = Field(..., gt=0)
    height: int = Field(..., gt=0)
    fps: float = Field(..., gt=0)
    bitrate: Optional[int] = None
    codec: Optional[str] = None
    file_size: int = Field(..., gt=0)
    
    @property
    def orientation(self) -> VideoOrientation:
        aspect_ratio = self.width / self.height
        return VideoOrientation.HORIZONTAL if aspect_ratio > 1 else VideoOrientation.VERTICAL

class VideoUrls(BaseModel):
    """Video URLs model"""
    original: HttpUrl
    hls_playlist: HttpUrl  # .m3u8 file
    thumbnail: Optional[HttpUrl] = None
    preview: Optional[HttpUrl] = None
    download: Optional[HttpUrl] = None

class Video(BaseDocument):
    """Video model"""
    creator_id: PyObjectId
    title: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    
    # Video properties
    video_type: VideoType
    privacy: VideoPrivacy = Field(default=VideoPrivacy.PUBLIC)
    metadata: VideoMetadata
    urls: VideoUrls
    draft_data: Optional[VideoDraft] = None
    
    # Features
    remix_enabled: bool = Field(default=True)
    comments_enabled: bool = Field(default=True)
    download_enabled: bool = Field(default=True)
    
    # Categorization
    hashtags: List[str] = Field(default_factory=list, max_length=30)
    categories: List[str] = Field(default_factory=list, max_length=10)
    language: str = Field(default="en")
    country: str
    
    # Engagement metrics
    views_count: int = Field(default=0, ge=0)
    likes_count: int = Field(default=0, ge=0)
    comments_count: int = Field(default=0, ge=0)
    shares_count: int = Field(default=0, ge=0)
    bookmarks_count: int = Field(default=0, ge=0)
    
    # Remix tracking
    is_remix: bool = Field(default=False)
    original_video_id: Optional[PyObjectId] = None
    remix_count: int = Field(default=0, ge=0)
    
    # Moderation
    is_approved: bool = Field(default=True)
    is_flagged: bool = Field(default=False)
    report_count: int = Field(default=0, ge=0)


    FEid: Optional[str] = None
    start: float = Field(default=0.0)
    end: Optional[float] = None  # Will be set to duration if not specified
    remoteUrl: Optional[str] = None
    
    # Music used (if any)
    music_id: Optional[PyObjectId] = None
    
    @model_validator(mode='after')
    def validate_video_type_duration(self):
        """Validate video type against duration"""
        if self.metadata:
            duration = self.metadata.duration
            # Set end to duration if not specified
            if self.end is None:
                self.end = duration
            if self.video_type == VideoType.BITS and duration > 15:
                raise ValueError("Bits videos must be 15 seconds or less")
            elif self.video_type == VideoType.REGULAR and (duration < 15 or duration > 120):
                raise ValueError("Regular videos must be between 15 and 90 seconds")
        return self
    
    @model_validator(mode='after')
    def validate_orientation_for_bits(self):
        """Validate orientation for bits videos"""
        if self.video_type == VideoType.BITS and self.metadata:
            if self.metadata.orientation != VideoOrientation.VERTICAL:
                raise ValueError("Bits videos must be vertical only")
        return self