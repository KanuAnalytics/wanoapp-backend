# """
# Comment CRUD operations with buffered counts

# app/api/v1/comments.py

# """
# from typing import List, Optional
# from fastapi import APIRouter, HTTPException, Depends, status
# from bson import ObjectId
# from datetime import datetime
# from app.core.database import get_database
# from app.api.deps import get_current_active_user
# from app.services.metrics_service import metrics_buffer
# from pydantic import BaseModel, Field

# router = APIRouter()  # This was missing!

# class CommentCreate(BaseModel):
#     content: str = Field(..., min_length=1, max_length=150)
#     parent_comment_id: Optional[str] = None

# class CommentResponse(BaseModel):
#     id: str = Field(alias="_id")
#     video_id: str
#     user_id: str
#     content: str
#     likes_count: int
#     replies_count: int
#     is_reply: bool
#     created_at: datetime
    
#     class Config:
#         populate_by_name = True

# @router.post("/videos/{video_id}/comments", response_model=CommentResponse)
# async def create_comment(
#     video_id: str,
#     comment: CommentCreate,
#     current_user: str = Depends(get_current_active_user)
# ):
#     """Create a comment on a video"""
#     db = get_database()
    
#     # Verify video exists
#     video = await db.videos.find_one({"_id": ObjectId(video_id)})
#     if not video:
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail="Video not found"
#         )
    
#     if not video.get("comments_enabled", True):
#         raise HTTPException(
#             status_code=status.HTTP_403_FORBIDDEN,
#             detail="Comments are disabled for this video"
#         )
    
#     comment_doc = {
#         "video_id": ObjectId(video_id),
#         "user_id": ObjectId(current_user),
#         "content": comment.content,
#         "likes_count": 0,
#         "replies_count": 0,
#         "is_reply": bool(comment.parent_comment_id),
#         "parent_comment_id": ObjectId(comment.parent_comment_id) if comment.parent_comment_id else None,
#         "is_flagged": False,
#         "is_hidden": False,
#         "created_at": datetime.utcnow(),
#         "updated_at": datetime.utcnow(),
#         "is_active": True
#     }
    
#     result = await db.comments.insert_one(comment_doc)
    
#     # Increment comment count in buffer
#     await metrics_buffer.increment_comment(video_id)
    
#     # If it's a reply, update parent comment's reply count
#     if comment.parent_comment_id:
#         await db.comments.update_one(
#             {"_id": ObjectId(comment.parent_comment_id)},
#             {"$inc": {"replies_count": 1}}
#         )
    
#     comment_doc["_id"] = str(result.inserted_id)
#     comment_doc["video_id"] = str(comment_doc["video_id"])
#     comment_doc["user_id"] = str(comment_doc["user_id"])
    
#     return CommentResponse(**comment_doc)

# @router.get("/videos/{video_id}/comments", response_model=List[CommentResponse])
# async def get_video_comments(
#     video_id: str,
#     skip: int = 0,
#     limit: int = 20
# ):
#     """Get comments for a video"""
#     db = get_database()
    
#     # Get top-level comments only (not replies)
#     cursor = db.comments.find({
#         "video_id": ObjectId(video_id),
#         "is_reply": False,
#         "is_active": True,
#         "is_hidden": False
#     }).skip(skip).limit(limit).sort("created_at", -1)
    
#     comments = []
#     async for comment in cursor:
#         comment["_id"] = str(comment["_id"])
#         comment["video_id"] = str(comment["video_id"])
#         comment["user_id"] = str(comment["user_id"])
#         comments.append(CommentResponse(**comment))
    
#     return comments

# @router.get("/comments/{comment_id}/replies", response_model=List[CommentResponse])
# async def get_comment_replies(
#     comment_id: str,
#     skip: int = 0,
#     limit: int = 10
# ):
#     """Get replies to a comment"""
#     db = get_database()
    
#     cursor = db.comments.find({
#         "parent_comment_id": ObjectId(comment_id),
#         "is_active": True,
#         "is_hidden": False
#     }).skip(skip).limit(limit).sort("created_at", 1)
    
#     replies = []
#     async for reply in cursor:
#         reply["_id"] = str(reply["_id"])
#         reply["video_id"] = str(reply["video_id"])
#         reply["user_id"] = str(reply["user_id"])
#         replies.append(CommentResponse(**reply))
    
#     return replies

# @router.delete("/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
# async def delete_comment(
#     comment_id: str,
#     current_user: str = Depends(get_current_active_user)
# ):
#     """Delete a comment (soft delete)"""
#     db = get_database()
    
#     comment = await db.comments.find_one({"_id": ObjectId(comment_id)})
#     if not comment:
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail="Comment not found"
#         )
    
#     if str(comment["user_id"]) != current_user:
#         raise HTTPException(
#             status_code=status.HTTP_403_FORBIDDEN,
#             detail="Can only delete your own comments"
#         )
    
#     await db.comments.update_one(
#         {"_id": ObjectId(comment_id)},
#         {"$set": {"is_active": False, "updated_at": datetime.utcnow()}}
#     )
    
#     # Decrement comment count directly for deletes
#     await db.videos.update_one(
#         {"_id": comment["video_id"]},
#         {"$inc": {"comments_count": -1}}
#     )


"""
Updates for app/api/v1/comments.py
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from typing import List, Optional
from datetime import datetime
from bson import ObjectId
from app.core.database import get_database
from app.api.deps import get_current_active_user
from app.models.comment import CommentCreate, CommentUpdate, CommentResponse

router = APIRouter()

@router.post("/", response_model=CommentResponse)
async def create_comment(
    comment: CommentCreate,
    current_user: str = Depends(get_current_active_user)
):
    """Create a new comment or reply"""
    db = get_database()
    
    # Verify video exists
    video = await db.videos.find_one({"_id": ObjectId(comment.video_id)})
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found"
        )
    
    # Get current user's display name
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # If it's a reply, verify parent comment exists
    if comment.parent_id:
        parent_comment = await db.comments.find_one({"_id": ObjectId(comment.parent_id)})
        if not parent_comment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Parent comment not found"
            )
    
    # Create comment document with user display name (fallback to Anonymous)
    comment_doc = {
        "video_id": ObjectId(comment.video_id),
        "user_id": ObjectId(current_user),
        "user_display_name": user.get("display_name", "Anonymous"),  # Fallback to Anonymous
        "content": comment.content,
        "parent_id": ObjectId(comment.parent_id) if comment.parent_id else None,
        "likes_count": 0,
        "replies_count": 0,
        "liked_by": [],
        "is_edited": False,
        "edited_at": None,
        "is_pinned": False,
        "is_hearted": False,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "is_active": True
    }
    
    # Insert comment
    result = await db.comments.insert_one(comment_doc)
    comment_doc["_id"] = str(result.inserted_id)
    
    # Update parent comment's reply count if it's a reply
    if comment.parent_id:
        await db.comments.update_one(
            {"_id": ObjectId(comment.parent_id)},
            {"$inc": {"replies_count": 1}}
        )
    
    # Update video's comment count
    await db.videos.update_one(
        {"_id": ObjectId(comment.video_id)},
        {"$inc": {"comments_count": 1}}
    )
    
    # Convert ObjectIds to strings for response
    response_doc = {
        **comment_doc,
        "_id": str(comment_doc["_id"]),
        "video_id": str(comment_doc["video_id"]),
        "user_id": str(comment_doc["user_id"]),
        "parent_id": str(comment_doc["parent_id"]) if comment_doc["parent_id"] else None,
        "is_liked": False
    }
    
    return CommentResponse(**response_doc)

# Add to app/api/v1/comments.py:

@router.get("/search", response_model=List[CommentResponse])
async def search_comments(
    q: str = Query(..., min_length=1, description="Search query"),
    video_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 20,
    current_user: str = Depends(get_current_active_user)
):
    """Search comments by content or user display name"""
    db = get_database()
    
    # Build search query
    search_filter = {
        "is_active": True,
        "$or": [
            {"content": {"$regex": q, "$options": "i"}},
            {"user_display_name": {"$regex": q, "$options": "i"}}
        ]
    }
    
    # Filter by video if specified
    if video_id:
        search_filter["video_id"] = ObjectId(video_id)
    
    # Execute search
    cursor = db.comments.find(search_filter).sort("created_at", -1).skip(skip).limit(limit)
    
    comments = []
    async for comment in cursor:
        # Check if current user liked this comment
        is_liked = ObjectId(current_user) in comment.get("liked_by", [])
        
        # Ensure user_display_name has fallback
        comment["user_display_name"] = comment.get("user_display_name", "Anonymous")
        
        response_doc = {
            **comment,
            "_id": str(comment["_id"]),
            "video_id": str(comment["video_id"]),
            "user_id": str(comment["user_id"]),
            "parent_id": str(comment["parent_id"]) if comment.get("parent_id") else None,
            "is_liked": is_liked
        }
        
        comments.append(CommentResponse(**response_doc))
    
    return comments

@router.get("/user/{user_id}", response_model=List[CommentResponse])
async def get_user_comments(
    user_id: str,
    skip: int = 0,
    limit: int = 20,
    current_user: str = Depends(get_current_active_user)
):
    """Get all comments by a specific user"""
    db = get_database()
    
    # Verify user exists
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Get user's comments
    cursor = db.comments.find({
        "user_id": ObjectId(user_id),
        "is_active": True
    }).sort("created_at", -1).skip(skip).limit(limit)
    
    comments = []
    async for comment in cursor:
        # Check if current user liked this comment
        is_liked = ObjectId(current_user) in comment.get("liked_by", [])
        
        # Ensure user_display_name has fallback
        comment["user_display_name"] = comment.get("user_display_name", "Anonymous")
        
        response_doc = {
            **comment,
            "_id": str(comment["_id"]),
            "video_id": str(comment["video_id"]),
            "user_id": str(comment["user_id"]),
            "parent_id": str(comment["parent_id"]) if comment.get("parent_id") else None,
            "is_liked": is_liked
        }
        
        comments.append(CommentResponse(**response_doc))
    
    return comments

@router.get("/video/{video_id}", response_model=List[CommentResponse])
async def get_video_comments(
    video_id: str,
    skip: int = 0,
    limit: int = 20,
    current_user: str = Depends(get_current_active_user)
):
    """Get comments for a video (top-level only, not replies)"""
    db = get_database()
    
    # Get top-level comments (no parent_id)
    cursor = db.comments.find({
        "video_id": ObjectId(video_id),
        "parent_id": None,
        "is_active": True
    }).sort([
        ("is_pinned", -1),  # Pinned comments first
        ("created_at", -1)   # Then by newest
    ]).skip(skip).limit(limit)
    
    comments = []
    async for comment in cursor:
        # Check if current user liked this comment
        is_liked = ObjectId(current_user) in comment.get("liked_by", [])
        
        # Ensure user_display_name has fallback
        # comment["user_display_name"] = comment.get("user_display_name", "Anonymous")
        
        user =  await db.users.find_one({"_id": ObjectId(comment["user_id"])})
        
        response_doc = {
            **comment,
            "_id": str(comment["_id"]),
            "video_id": str(comment["video_id"]),
            "user_display_name": user["display_name"] if user and "display_name" in user else "Anonymous",
            "profile_picture": user["profile_picture"] if user and "profile_picture" in user else None,
            "user_id": str(comment["user_id"]),
            "is_user_active": user["is_active"] if user and "is_active" in user else False,
            "parent_id": None,
            "is_liked": is_liked
        }
        
        comments.append(CommentResponse(**response_doc))
    
    return comments

@router.get("/{comment_id}/replies", response_model=List[CommentResponse])
async def get_comment_replies(
    comment_id: str,
    skip: int = 0,
    limit: int = 20,
    current_user: str = Depends(get_current_active_user)
):
    """Get replies to a comment"""
    db = get_database()
    
    # Verify parent comment exists
    parent_comment = await db.comments.find_one({"_id": ObjectId(comment_id)})
    if not parent_comment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Comment not found"
        )
    
    # Get replies
    cursor = db.comments.find({
        "parent_id": ObjectId(comment_id),
        "is_active": True
    }).sort("created_at", 1).skip(skip).limit(limit)  # Oldest first for replies
    
    replies = []
    async for reply in cursor:
        # Check if current user liked this reply
        is_liked = ObjectId(current_user) in reply.get("liked_by", [])
        
        # Ensure user_display_name has fallback
        # reply["user_display_name"] = reply.get("user_display_name", "Anonymous")
        
        user =  await db.users.find_one({"_id": ObjectId(reply["user_id"])})
        
        response_doc = {
            **reply,
            "_id": str(reply["_id"]),
            "video_id": str(reply["video_id"]),
            "user_id": str(reply["user_id"]),
            "user_display_name": user["display_name"] if user and "display_name" in user else "Anonymous",
            "profile_picture": user["profile_picture"] if user and "profile_picture" in user else None,
            "is_user_active": user["is_active"] if user and "is_active" in user else False,
            "parent_id": str(reply["parent_id"]),
            "is_liked": is_liked
        }
        
        replies.append(CommentResponse(**response_doc))
    
    return replies

@router.put("/{comment_id}", response_model=CommentResponse)
async def update_comment(
    comment_id: str,
    comment_update: CommentUpdate,
    current_user: str = Depends(get_current_active_user)
):
    """Update a comment (only by the comment author)"""
    db = get_database()
    
    # Get comment
    comment = await db.comments.find_one({
        "_id": ObjectId(comment_id),
        "is_active": True
    })
    
    if not comment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Comment not found"
        )
    
    # Check if user is the comment author
    if str(comment["user_id"]) != current_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only edit your own comments"
        )
    
    # Update comment
    update_doc = {
        "content": comment_update.content,
        "is_edited": True,
        "edited_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    
    await db.comments.update_one(
        {"_id": ObjectId(comment_id)},
        {"$set": update_doc}
    )
    
    # Get updated comment
    comment = await db.comments.find_one({"_id": ObjectId(comment_id)})
    is_liked = ObjectId(current_user) in comment.get("liked_by", [])
    
    # Ensure user_display_name has fallback
    comment["user_display_name"] = comment.get("user_display_name", "Anonymous")
    
    response_doc = {
        **comment,
        "_id": str(comment["_id"]),
        "video_id": str(comment["video_id"]),
        "user_id": str(comment["user_id"]),
        "parent_id": str(comment["parent_id"]) if comment.get("parent_id") else None,
        "is_liked": is_liked
    }
    
    return CommentResponse(**response_doc)

@router.delete("/{comment_id}")
async def delete_comment(
    comment_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Delete a comment (soft delete)"""
    db = get_database()
    
    # Get comment
    comment = await db.comments.find_one({
        "_id": ObjectId(comment_id),
        "is_active": True
    })
    
    if not comment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Comment not found"
        )
    
    # Check if user is the comment author or video owner
    video = await db.videos.find_one({"_id": comment["video_id"]})
    if str(comment["user_id"]) != current_user and str(video["creator_id"]) != current_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own comments or comments on your videos"
        )
    
    # Soft delete
    await db.comments.update_one(
        {"_id": ObjectId(comment_id)},
        {
            "$set": {
                "is_active": False,
                "updated_at": datetime.utcnow()
            }
        }
    )
    
    # Update parent comment's reply count if it's a reply
    if comment.get("parent_id"):
        await db.comments.update_one(
            {"_id": comment["parent_id"]},
            {"$inc": {"replies_count": -1}}
        )
    
    # Update video's comment count
    await db.videos.update_one(
        {"_id": comment["video_id"]},
        {"$inc": {"comments_count": -1}}
    )
    
    return {"message": "Comment deleted successfully"}

@router.post("/{comment_id}/like")
async def like_comment(
    comment_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Like or unlike a comment"""
    db = get_database()
    
    # Get comment
    comment = await db.comments.find_one({
        "_id": ObjectId(comment_id),
        "is_active": True
    })
    
    if not comment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Comment not found"
        )
    
    user_id = ObjectId(current_user)
    
    # Check if already liked
    if user_id in comment.get("liked_by", []):
        # Unlike
        await db.comments.update_one(
            {"_id": ObjectId(comment_id)},
            {
                "$pull": {"liked_by": user_id},
                "$inc": {"likes_count": -1}
            }
        )
        return {"liked": False, "likes_count": comment["likes_count"] - 1}
    else:
        # Like
        await db.comments.update_one(
            {"_id": ObjectId(comment_id)},
            {
                "$addToSet": {"liked_by": user_id},
                "$inc": {"likes_count": 1}
            }
        )
        return {"liked": True, "likes_count": comment["likes_count"] + 1}
