from typing import List, Dict, Any, Optional, Literal
from datetime import datetime
from pydantic import BaseModel, Field, validator
from app.models.base import BaseDocument, PyObjectId

class ClipModel(BaseModel):
    """Individual clip in a draft"""
    id: str = Field(..., description="Unique clip identifier")
    userId: Optional[str] = Field(None, description="ID of the user who created this video/photo")
    type: Literal["video", "photo"] = Field(..., description="Clip type")
    duration: float = Field(..., ge=0, description="Clip duration in seconds")
    start: float = Field(..., ge=0, description="Start time")
    end: float = Field(..., ge=0, description="End time")
    remoteUrl: str = Field(..., description="URL of the media file")
    trimmedUri: Optional[str] = Field("", description="URI of trimmed version")
    isTrimmed: bool = Field(False, description="Whether clip is trimmed")
    isSplitted: bool = Field(False, description="Whether clip is split")
    
    @validator('isSplitted', pre=True)
    def validate_is_splitted(cls, v):
        """Ensure isSplitted is always boolean"""
        if isinstance(v, str):
            return v.lower() in ('true', '1', 'yes')
        return bool(v)

class DraftBase(BaseModel):
    """Base draft model"""
    clips: List[ClipModel] = Field(..., min_items=1, description="List of clips")
    ratio: str = Field("3:4", pattern="^\\d+:\\d+$", description="Video aspect ratio")
    audioUrl: Optional[str] = Field(None, description="Background audio URL")

class Draft(DraftBase, BaseDocument):
    """Draft document model"""
    user_id: PyObjectId = Field(..., description="Owner user ID")
    name: Optional[str] = Field(None, max_length=200, description="Draft name")

class DraftCreate(DraftBase):
    """Create draft request model"""
    name: Optional[str] = Field(None, max_length=200, description="Optional draft name")

class DraftUpdate(DraftBase):
    """Update draft request model - requires all fields"""
    name: Optional[str] = Field(None, max_length=200, description="Optional draft name")

class DraftPatch(BaseModel):
    """Patch draft request model - all fields optional"""
    clips: Optional[List[ClipModel]] = None
    ratio: Optional[str] = Field(None, pattern="^\\d+:\\d+$")
    audioUrl: Optional[str] = None
    name: Optional[str] = Field(None, max_length=200)

class DraftResponse(BaseModel):
    """Draft response model"""
    id: str = Field(alias="_id")
    user_id: str
    clips: List[ClipModel]
    ratio: str
    audioUrl: Optional[str] = None
    name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    
    class Config:
        populate_by_name = True