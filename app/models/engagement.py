"""
app/models/engagement.py

"""

from typing import Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import BaseDocument, PyObjectId, ReportReason, NotificationType

class Comment(BaseDocument):
    """Comment model"""
    video_id: PyObjectId
    user_id: PyObjectId
    content: str = Field(..., min_length=1, max_length=150)
    
    # Engagement
    likes_count: int = Field(default=0, ge=0)
    replies_count: int = Field(default=0, ge=0)
    
    # Reply tracking
    is_reply: bool = Field(default=False)
    parent_comment_id: Optional[PyObjectId] = None
    
    # Moderation
    is_flagged: bool = Field(default=False)
    is_hidden: bool = Field(default=False)

class VideoReport(BaseDocument):
    """Video report model"""
    video_id: PyObjectId
    reporter_id: PyObjectId
    reason: ReportReason
    description: Optional[str] = Field(None, max_length=500)
    
    # Moderation
    status: str = Field(default="pending", pattern="^(pending|reviewed|resolved|dismissed)$")
    moderator_id: Optional[PyObjectId] = None
    resolution_notes: Optional[str] = None
    resolved_at: Optional[datetime] = None

class Notification(BaseDocument):
    """Notification model"""
    user_id: PyObjectId
    type: NotificationType
    
    # Notification content
    title: str
    message: str
    
    # Related entities
    from_user_id: Optional[PyObjectId] = None
    video_id: Optional[PyObjectId] = None
    comment_id: Optional[PyObjectId] = None
    
    # Status
    is_read: bool = Field(default=False)
    read_at: Optional[datetime] = None
    
    # Additional data
    data: Dict[str, Any] = Field(default_factory=dict)