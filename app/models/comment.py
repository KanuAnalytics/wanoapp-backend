"""
app/models/comment.py

Updated comment model to include user display name
"""
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import BaseDocument, PyObjectId

class CommentBase(BaseModel):
    """Base comment model"""
    video_id: PyObjectId
    user_id: PyObjectId
    user_display_name: str = Field(..., description="Cached display name of the commenter")
    content: str = Field(..., min_length=1, max_length=500)
    parent_id: Optional[PyObjectId] = None  # For replies
    is_edited: bool = Field(default=False)
    edited_at: Optional[datetime] = None

class Comment(CommentBase, BaseDocument):
    """Comment document model"""
    likes_count: int = Field(default=0, ge=0)
    replies_count: int = Field(default=0, ge=0)
    liked_by: List[PyObjectId] = Field(default_factory=list)
    is_pinned: bool = Field(default=False)
    is_hearted: bool = Field(default=False)  # Hearted by video creator
    
class CommentCreate(BaseModel):
    """Create comment request model"""
    video_id: str
    content: str = Field(..., min_length=1, max_length=500)
    parent_id: Optional[str] = None  # For replies

class CommentUpdate(BaseModel):
    """Update comment request model"""
    content: str = Field(..., min_length=1, max_length=500)

class CommentResponse(BaseModel):
    """Comment response model"""
    id: str = Field(alias="_id")
    video_id: str
    user_id: str
    profile_picture: Optional[str] = None
    user_display_name: Optional[str] = "Anonymous"
    content: str
    parent_id: Optional[str] = None
    likes_count: int = 0
    replies_count: int = 0
    is_user_active: bool = False
    is_liked: bool = False  # Whether current user liked it
    is_edited: bool = False
    edited_at: Optional[datetime] = None
    is_pinned: bool = False
    is_hearted: bool = False
    created_at: datetime
    updated_at: datetime
    
    class Config:
        populate_by_name = True
