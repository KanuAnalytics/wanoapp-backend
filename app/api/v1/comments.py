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
import re
from fastapi import APIRouter, Depends, HTTPException, Query, status, BackgroundTasks
from typing import Dict, Iterable, List, Optional
from datetime import datetime
from bson import ObjectId
from app.core.database import get_database
from app.api.deps import get_current_active_user
from app.models.comment import CommentCreate, CommentUpdate, CommentResponse
from app.services.expo import send_push_message
from app.services import recommendations, trending
from app.services.recombee_service import recombee_send

router = APIRouter()

SEARCH_MAX_LENGTH = 100
MAX_PAGE_SIZE = 100


def _object_id(value, label: str = "ID") -> ObjectId:
    """Parse an id from the request; 400 instead of a 500 on garbage."""
    if isinstance(value, ObjectId):
        return value
    if not isinstance(value, str) or not ObjectId.is_valid(value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {label}",
        )
    return ObjectId(value)


async def _users_by_id(db, comments: Iterable[dict]) -> Dict[ObjectId, dict]:
    """One $in lookup for every commenter on the page (instead of one per comment)."""
    ids = {c.get("user_id") for c in comments if c.get("user_id") is not None}
    if not ids:
        return {}
    cursor = db.users.find(
        {"_id": {"$in": list(ids)}},
        {"display_name": 1, "profile_picture": 1, "is_active": 1},
    )
    return {u["_id"]: u async for u in cursor}


def _live_user_fields(user: Optional[dict]) -> dict:
    return {
        "user_display_name": user["display_name"] if user and "display_name" in user else "Anonymous",
        "profile_picture": user["profile_picture"] if user and "profile_picture" in user else None,
        "is_user_active": user["is_active"] if user and "is_active" in user else False,
    }


def _stored_comment_response(comment: dict, current_user: str) -> CommentResponse:
    """Response built from the cached display name stored on the comment."""
    is_liked = ObjectId(current_user) in comment.get("liked_by", [])
    comment["user_display_name"] = comment.get("user_display_name", "Anonymous")
    return CommentResponse(**{
        **comment,
        "_id": str(comment["_id"]),
        "video_id": str(comment["video_id"]),
        "user_id": str(comment["user_id"]),
        "parent_id": str(comment["parent_id"]) if comment.get("parent_id") else None,
        "is_liked": is_liked,
    })


@router.post("/", response_model=CommentResponse)
async def create_comment(
    comment: CommentCreate,
    current_user: str = Depends(get_current_active_user),
    background_tasks: BackgroundTasks = None,
):
    """Create a new comment or reply"""
    db = get_database()
    video_oid = _object_id(comment.video_id, "video ID")

    # Verify the video exists and the caller may comment on it
    video = await db.videos.find_one({"_id": video_oid})
    if (
        not video
        or not video.get("is_active", True)
        or (video.get("privacy") == "private" and str(video.get("creator_id")) != str(current_user))
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found"
        )
    if video.get("comments_enabled") is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Comments are disabled for this video"
        )

    # Get current user's display name
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # If it's a reply, verify the parent is a live top-level comment on this video
    parent_comment = None
    parent_oid = None
    if comment.parent_id:
        parent_oid = _object_id(comment.parent_id, "parent comment ID")
        parent_comment = await db.comments.find_one({"_id": parent_oid})
        if not parent_comment or not parent_comment.get("is_active", False):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Parent comment not found"
            )
        if parent_comment.get("video_id") != video_oid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Parent comment belongs to a different video"
            )
        if parent_comment.get("parent_id"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot reply to a reply"
            )

    # Create comment document with user display name (fallback to Anonymous)
    comment_doc = {
        "video_id": video_oid,
        "user_id": ObjectId(current_user),
        "user_display_name": user.get("display_name", "Anonymous"),  # Fallback to Anonymous
        "content": comment.content,
        "parent_id": parent_oid,
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
    if parent_oid:
        await db.comments.update_one(
            {"_id": parent_oid},
            {"$inc": {"replies_count": 1}}
        )

    # Update video's comment count
    await db.videos.update_one(
        {"_id": video_oid},
        {"$inc": {"comments_count": 1}}
    )

    if parent_comment:
        notif_recipient = parent_comment.get("user_id")
        notif_type = "comment_reply"
    else:
        notif_recipient = video.get("creator_id")
        notif_type = "comment"

    if notif_recipient and str(notif_recipient) != str(current_user):
        await db.notifications.insert_one({
            "recipient_id": ObjectId(notif_recipient),
            "type": notif_type,
            "user_id": ObjectId(current_user),
            "post_id": video_oid,
            "comment_id": result.inserted_id,
            "date": datetime.utcnow(),
        })

    # A comment is strong engagement; it is NOT a bookmark (that would
    # overwrite or delete the person's real bookmark).
    await recommendations.report_comment(current_user, comment.video_id)
    await trending.record_once(db, comment.video_id, actor=current_user, creator=video.get("creator_id"),
                               kind="comment", comments=1)

    if background_tasks is not None:
        display_name = user.get("display_name") or user.get("username") or "Someone"
        comment_text = (comment.content or "").strip()
        comment_preview = comment_text[:30] + ("..." if len(comment_text) > 30 else "")
        thumbnail_url = (video.get("urls") or {}).get("thumbnail")

        creator_id = video.get("creator_id")
        if creator_id and str(creator_id) != str(current_user):
            recipient = await db.users.find_one(
                {"_id": ObjectId(creator_id)},
                {"expo_push_tokens": 1},
            )
            recipient_tokens = (recipient or {}).get("expo_push_tokens") or []
            for token in recipient_tokens:
                background_tasks.add_task(
                    send_push_message,
                    token,
                    comment_preview,
                    {"video_id": comment.video_id, "commenter_id": str(current_user)},
                    f"{display_name} commented on your video",
                    thumbnail_url,
                )

        if parent_comment:
            parent_user_id = parent_comment.get("user_id")
            if parent_user_id and str(parent_user_id) != str(current_user) and str(parent_user_id) != str(creator_id):
                parent_user = await db.users.find_one(
                    {"_id": ObjectId(parent_user_id)},
                    {"expo_push_tokens": 1},
                )
                parent_tokens = (parent_user or {}).get("expo_push_tokens") or []
                for token in parent_tokens:
                    background_tasks.add_task(
                        send_push_message,
                        token,
                        comment_preview,
                        {"video_id": comment.video_id, "commenter_id": str(current_user), "parent_comment_id": str(parent_comment["_id"])},
                        f"{display_name} replied to your comment",
                        thumbnail_url,
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
    q: str = Query(..., min_length=1, max_length=SEARCH_MAX_LENGTH, description="Search query"),
    video_id: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    current_user: str = Depends(get_current_active_user)
):
    """Search comments by content or user display name"""
    db = get_database()

    # The query is matched literally - user input is never a regex.
    pattern = re.escape(q)
    search_filter = {
        "is_active": True,
        "$or": [
            {"content": {"$regex": pattern, "$options": "i"}},
            {"user_display_name": {"$regex": pattern, "$options": "i"}}
        ]
    }

    # Filter by video if specified
    if video_id:
        search_filter["video_id"] = _object_id(video_id, "video ID")

    cursor = db.comments.find(search_filter).sort("created_at", -1).skip(skip).limit(limit)
    return [_stored_comment_response(c, current_user) async for c in cursor]

@router.get("/user/{user_id}", response_model=List[CommentResponse])
async def get_user_comments(
    user_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    current_user: str = Depends(get_current_active_user)
):
    """Get all comments by a specific user"""
    db = get_database()
    user_oid = _object_id(user_id, "user ID")

    # Verify user exists
    user = await db.users.find_one({"_id": user_oid}, {"_id": 1})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    cursor = db.comments.find({
        "user_id": user_oid,
        "is_active": True
    }).sort("created_at", -1).skip(skip).limit(limit)
    return [_stored_comment_response(c, current_user) async for c in cursor]

@router.get("/video/{video_id}", response_model=List[CommentResponse])
async def get_video_comments(
    video_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    current_user: str = Depends(get_current_active_user)
):
    """Get comments for a video (top-level only, not replies)"""
    db = get_database()
    video_oid = _object_id(video_id, "video ID")

    # Get top-level comments (no parent_id)
    docs = await db.comments.find({
        "video_id": video_oid,
        "parent_id": None,
        "is_active": True
    }).sort([
        ("is_pinned", -1),  # Pinned comments first
        ("created_at", -1)   # Then by newest
    ]).skip(skip).limit(limit).to_list(length=limit)

    users = await _users_by_id(db, docs)
    viewer = ObjectId(current_user)
    comments = []
    for comment in docs:
        comments.append(CommentResponse(**{
            **comment,
            "_id": str(comment["_id"]),
            "video_id": str(comment["video_id"]),
            **_live_user_fields(users.get(comment.get("user_id"))),
            "user_id": str(comment["user_id"]),
            "parent_id": None,
            "is_liked": viewer in comment.get("liked_by", []),
        }))
    return comments

@router.get("/{comment_id}/replies", response_model=List[CommentResponse])
async def get_comment_replies(
    comment_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    current_user: str = Depends(get_current_active_user)
):
    """Get replies to a comment"""
    db = get_database()
    comment_oid = _object_id(comment_id, "comment ID")

    # Verify parent comment exists
    parent_comment = await db.comments.find_one({"_id": comment_oid}, {"_id": 1})
    if not parent_comment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Comment not found"
        )

    docs = await db.comments.find({
        "parent_id": comment_oid,
        "is_active": True
    }).sort("created_at", 1).skip(skip).limit(limit).to_list(length=limit)  # Oldest first for replies

    users = await _users_by_id(db, docs)
    viewer = ObjectId(current_user)
    replies = []
    for reply in docs:
        replies.append(CommentResponse(**{
            **reply,
            "_id": str(reply["_id"]),
            "video_id": str(reply["video_id"]),
            "user_id": str(reply["user_id"]),
            **_live_user_fields(users.get(reply.get("user_id"))),
            "parent_id": str(reply["parent_id"]),
            "is_liked": viewer in reply.get("liked_by", []),
        }))
    return replies

@router.put("/{comment_id}", response_model=CommentResponse)
async def update_comment(
    comment_id: str,
    comment_update: CommentUpdate,
    current_user: str = Depends(get_current_active_user)
):
    """Update a comment (only by the comment author)"""
    db = get_database()
    comment_oid = _object_id(comment_id, "comment ID")

    # Get comment
    comment = await db.comments.find_one({
        "_id": comment_oid,
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

    update_doc = {
        "content": comment_update.content,
        "is_edited": True,
        "edited_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }

    await db.comments.update_one(
        {"_id": comment_oid, "is_active": True},
        {"$set": update_doc}
    )

    comment = await db.comments.find_one({"_id": comment_oid})
    return _stored_comment_response(comment, current_user)

@router.delete("/{comment_id}")
async def delete_comment(
    comment_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Delete a comment (soft delete)"""
    db = get_database()
    comment_oid = _object_id(comment_id, "comment ID")

    comment = await db.comments.find_one({
        "_id": comment_oid,
        "is_active": True
    })

    if not comment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Comment not found"
        )

    # The comment author or the video owner may delete it. The video may be
    # gone already; then only the author can.
    video = await db.videos.find_one({"_id": comment["video_id"]}, {"creator_id": 1})
    is_author = str(comment["user_id"]) == current_user
    is_video_owner = bool(video) and str(video.get("creator_id")) == current_user
    if not is_author and not is_video_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own comments or comments on your videos"
        )

    # Soft delete atomically: only the request that flips is_active adjusts
    # the counters, so concurrent/repeated deletes count once.
    deleted = await db.comments.find_one_and_update(
        {"_id": comment_oid, "is_active": True},
        {"$set": {"is_active": False, "updated_at": datetime.utcnow()}},
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Comment not found"
        )

    if deleted.get("parent_id"):
        await db.comments.update_one(
            {"_id": deleted["parent_id"], "replies_count": {"$gt": 0}},
            {"$inc": {"replies_count": -1}}
        )

    if video:
        await db.videos.update_one(
            {"_id": deleted["video_id"], "comments_count": {"$gt": 0}},
            {"$inc": {"comments_count": -1}}
        )

    await db.notifications.delete_one({
        "comment_id": comment_oid,
    })

    # The engagement belongs to the commenter, not to whoever deleted it.
    await recommendations.report_comment_deleted(str(deleted["user_id"]), str(deleted["video_id"]))

    return {"message": "Comment deleted successfully"}

@router.post("/{comment_id}/like")
async def like_comment(
    comment_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Like or unlike a comment (toggle)"""
    db = get_database()
    comment_oid = _object_id(comment_id, "comment ID")

    comment = await db.comments.find_one(
        {"_id": comment_oid, "is_active": True},
        {"liked_by": 1, "likes_count": 1},
    )

    if not comment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Comment not found"
        )

    user_id = ObjectId(current_user)

    # Each write is conditional on the current state, so a repeated or racing
    # request can never count the same user twice or drive the count negative.
    if user_id in comment.get("liked_by", []):
        await db.comments.update_one(
            {"_id": comment_oid, "liked_by": user_id},
            {
                "$pull": {"liked_by": user_id},
                "$inc": {"likes_count": -1}
            }
        )
        liked = False
    else:
        await db.comments.update_one(
            {"_id": comment_oid, "is_active": True, "liked_by": {"$ne": user_id}},
            {
                "$addToSet": {"liked_by": user_id},
                "$inc": {"likes_count": 1}
            }
        )
        liked = True

    fresh = await db.comments.find_one({"_id": comment_oid}, {"likes_count": 1}) or {}
    return {"liked": liked, "likes_count": max(int(fresh.get("likes_count", 0) or 0), 0)}
