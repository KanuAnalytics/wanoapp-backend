"""
Comment CRUD operations with buffered counts

app/api/v1/comments.py

"""
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends, status
from bson import ObjectId
from datetime import datetime
from app.core.database import get_database
from app.api.deps import get_current_active_user
from app.services.metrics_service import metrics_buffer
from pydantic import BaseModel, Field

router = APIRouter()  # This was missing!

class CommentCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=150)
    parent_comment_id: Optional[str] = None

class CommentResponse(BaseModel):
    id: str = Field(alias="_id")
    video_id: str
    user_id: str
    content: str
    likes_count: int
    replies_count: int
    is_reply: bool
    created_at: datetime
    
    class Config:
        populate_by_name = True

@router.post("/videos/{video_id}/comments", response_model=CommentResponse)
async def create_comment(
    video_id: str,
    comment: CommentCreate,
    current_user: str = Depends(get_current_active_user)
):
    """Create a comment on a video"""
    db = get_database()
    
    # Verify video exists
    video = await db.videos.find_one({"_id": ObjectId(video_id)})
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found"
        )
    
    if not video.get("comments_enabled", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Comments are disabled for this video"
        )
    
    comment_doc = {
        "video_id": ObjectId(video_id),
        "user_id": ObjectId(current_user),
        "content": comment.content,
        "likes_count": 0,
        "replies_count": 0,
        "is_reply": bool(comment.parent_comment_id),
        "parent_comment_id": ObjectId(comment.parent_comment_id) if comment.parent_comment_id else None,
        "is_flagged": False,
        "is_hidden": False,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "is_active": True
    }
    
    result = await db.comments.insert_one(comment_doc)
    
    # Increment comment count in buffer
    await metrics_buffer.increment_comment(video_id)
    
    # If it's a reply, update parent comment's reply count
    if comment.parent_comment_id:
        await db.comments.update_one(
            {"_id": ObjectId(comment.parent_comment_id)},
            {"$inc": {"replies_count": 1}}
        )
    
    comment_doc["_id"] = str(result.inserted_id)
    comment_doc["video_id"] = str(comment_doc["video_id"])
    comment_doc["user_id"] = str(comment_doc["user_id"])
    
    return CommentResponse(**comment_doc)

@router.get("/videos/{video_id}/comments", response_model=List[CommentResponse])
async def get_video_comments(
    video_id: str,
    skip: int = 0,
    limit: int = 20
):
    """Get comments for a video"""
    db = get_database()
    
    # Get top-level comments only (not replies)
    cursor = db.comments.find({
        "video_id": ObjectId(video_id),
        "is_reply": False,
        "is_active": True,
        "is_hidden": False
    }).skip(skip).limit(limit).sort("created_at", -1)
    
    comments = []
    async for comment in cursor:
        comment["_id"] = str(comment["_id"])
        comment["video_id"] = str(comment["video_id"])
        comment["user_id"] = str(comment["user_id"])
        comments.append(CommentResponse(**comment))
    
    return comments

@router.get("/comments/{comment_id}/replies", response_model=List[CommentResponse])
async def get_comment_replies(
    comment_id: str,
    skip: int = 0,
    limit: int = 10
):
    """Get replies to a comment"""
    db = get_database()
    
    cursor = db.comments.find({
        "parent_comment_id": ObjectId(comment_id),
        "is_active": True,
        "is_hidden": False
    }).skip(skip).limit(limit).sort("created_at", 1)
    
    replies = []
    async for reply in cursor:
        reply["_id"] = str(reply["_id"])
        reply["video_id"] = str(reply["video_id"])
        reply["user_id"] = str(reply["user_id"])
        replies.append(CommentResponse(**reply))
    
    return replies

@router.delete("/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(
    comment_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Delete a comment (soft delete)"""
    db = get_database()
    
    comment = await db.comments.find_one({"_id": ObjectId(comment_id)})
    if not comment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Comment not found"
        )
    
    if str(comment["user_id"]) != current_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only delete your own comments"
        )
    
    await db.comments.update_one(
        {"_id": ObjectId(comment_id)},
        {"$set": {"is_active": False, "updated_at": datetime.utcnow()}}
    )
    
    # Decrement comment count directly for deletes
    await db.videos.update_one(
        {"_id": comment["video_id"]},
        {"$inc": {"comments_count": -1}}
    )