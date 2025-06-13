"""
app/models/report.py

Video report model for handling user reports on videos
"""

from typing import Optional
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field, validator
from app.models.base import BaseDocument, ReportReason, PyObjectId

class ReportCategory(str, Enum):
    """Extended report categories"""
    VIOLENCE = "violence"
    HATE_SPEECH = "hate_speech"
    NUDITY = "nudity"
    SUICIDE_SELF_HARM = "suicide_self_harm"
    FALSE_INFORMATION = "false_information"
    BULLYING = "bullying"
    TERRORISM = "terrorism"
    CHILD_EXPLOITATION = "child_exploitation"
    DANGEROUS_ACTS = "dangerous_acts"
    REGULATED_GOODS = "regulated_goods"
    INTELLECTUAL_PROPERTY = "intellectual_property"
    SPAM = "spam"
    OTHER = "other"

class VideoReport(BaseDocument):
    """Video report model"""
    video_id: PyObjectId = Field(..., description="ID of the reported video")
    reporter_id: PyObjectId = Field(..., description="ID of the user making the report")
    
    # Report details
    reason: ReportReason = Field(..., description="Primary reason for the report")
    category: ReportCategory = Field(..., description="Specific category of the violation")
    comment: Optional[str] = Field(None, max_length=500, description="Additional details from reporter")
    
    # Optional timestamp for specific moment in video
    timestamp: Optional[float] = Field(None, ge=0, description="Specific timestamp in video (seconds)")
    
    # Status tracking
    status: str = Field(default="pending", description="Report status: pending, reviewed, resolved, dismissed")
    reviewed_by: Optional[PyObjectId] = Field(None, description="Admin who reviewed this report")
    reviewed_at: Optional[datetime] = Field(None, description="When the report was reviewed")
    admin_notes: Optional[str] = Field(None, max_length=1000, description="Internal admin notes")
    
    @validator('comment')
    def validate_comment(cls, v):
        if v is not None:
            # Remove extra whitespace and ensure it's not empty after stripping
            cleaned = v.strip()
            return cleaned if cleaned else None
        return v
    
    @validator('timestamp')
    def validate_timestamp(cls, v):
        if v is not None and v < 0:
            raise ValueError("Timestamp must be non-negative")
        return v

class ReportCreate(BaseModel):
    """Request model for creating a video report"""
    reason: ReportReason = Field(..., description="Primary reason for the report")
    category: ReportCategory = Field(..., description="Specific category of the violation")
    comment: Optional[str] = Field(None, max_length=500, description="Additional details about the violation")
    timestamp: Optional[float] = Field(None, ge=0, description="Specific timestamp in video where violation occurs (seconds)")
    
    @validator('comment')
    def validate_comment(cls, v):
        if v is not None:
            cleaned = v.strip()
            return cleaned if cleaned else None
        return v

class ReportResponse(BaseModel):
    """Response model for report operations"""
    id: str = Field(alias="_id")
    video_id: str
    reporter_id: str
    reason: ReportReason
    category: ReportCategory
    comment: Optional[str]
    timestamp: Optional[float]
    status: str
    created_at: datetime
    
    class Config:
        populate_by_name = True