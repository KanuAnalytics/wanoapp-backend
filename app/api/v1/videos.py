"""
Video CRUD operations with buffered metrics

app/api/v1/vidoes.py

"""
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends, status, UploadFile, File, Form
from bson import ObjectId
from datetime import datetime
from app.models.video import Video, VideoType, VideoPrivacy
from app.core.database import get_database
from app.api.deps import get_current_active_user, get_verified_user
from app.services.metrics_service import metrics_buffer
from pydantic import BaseModel, Field

router = APIRouter()

class VideoCreate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    video_type: VideoType
    privacy: VideoPrivacy = VideoPrivacy.PUBLIC
    hashtags: List[str] = []
    categories: List[str] = []
    remix_enabled: bool = True
    comments_enabled: bool = True

class VideoUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    privacy: Optional[VideoPrivacy] = None
    hashtags: Optional[List[str]] = None
    categories: Optional[List[str]] = None
    remix_enabled: Optional[bool] = None
    comments_enabled: Optional[bool] = None

class VideoResponse(BaseModel):
    id: str = Field(alias="_id")
    creator_id: str
    title: Optional[str]
    description: Optional[str]
    video_type: VideoType
    privacy: VideoPrivacy
    views_count: int
    likes_count: int
    comments_count: int
    created_at: datetime
    
    # Include buffered counts for real-time feel
    buffered_views: int = 0
    buffered_likes: int = 0
    buffered_comments: int = 0

    FEid: Optional[str] = None
    start: float = 0.0
    end: Optional[float] = None
    remoteUrl: Optional[str] = None
    
    class Config:
        populate_by_name = True

# Modify create_video function
# Update the create_video endpoint to use get_verified_user
@router.post("/", response_model=VideoResponse, status_code=status.HTTP_201_CREATED)
async def create_video(
    video_data: VideoCreate,
    current_user: str = Depends(get_verified_user)  # This will now work
):
    """Create a new video post - requires verified email"""
    db = get_database()
    
    # Get user info
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    
    video_doc = {
        **video_data.dict(),
        "creator_id": ObjectId(current_user),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "is_active": True,
        "views_count": 0,
        "likes_count": 0,
        "comments_count": 0,
        "shares_count": 0,
        "bookmarks_count": 0,
        "is_approved": True,
        "is_flagged": False,
        "report_count": 0,
        "is_remix": False,
        "remix_count": 0,
        # Placeholder URLs - in production, these would come from storage service
        "urls": {
            "original": "https://storage.wano.com/video.mp4",
            "hls_playlist": "https://storage.wano.com/video.m3u8",
            "thumbnail": "https://storage.wano.com/thumb.jpg"
        },
        "metadata": {
            "duration": 30.0,
            "width": 1080,
            "height": 1920,
            "fps": 30.0,
            "file_size": 10000000
        },
        "country": user.get("localization", {}).get("country", "NG"),
        "language": user.get("localization", {}).get("languages", ["en"])[0]
    }
    
    result = await db.videos.insert_one(video_doc)
    
    # Update user's video count
    await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {"$inc": {"videos_count": 1}}
    )
    
    video_doc["_id"] = str(result.inserted_id)
    video_doc["creator_id"] = str(video_doc["creator_id"])
    
    return VideoResponse(**video_doc)

@router.get("/", response_model=List[VideoResponse])
async def get_videos(
    skip: int = 0,
    limit: int = 20,
    video_type: Optional[VideoType] = None,
    privacy: Optional[VideoPrivacy] = None
):
    """Get all videos with pagination"""
    db = get_database()
    
    query = {"is_active": True}
    if video_type:
        query["video_type"] = video_type
    if privacy:
        query["privacy"] = privacy
    
    cursor = db.videos.find(query).skip(skip).limit(limit).sort("created_at", -1)
    videos = []
    async for video in cursor:
        video["_id"] = str(video["_id"])
        video["creator_id"] = str(video["creator_id"])
        
        # Get buffered counts
        video_id = video["_id"]
        buffered = await metrics_buffer.get_buffered_counts(video_id)
        
        response = VideoResponse(**video)
        response.buffered_views = buffered["views"]
        response.buffered_likes = buffered["likes"]
        response.buffered_comments = buffered["comments"]
        
        videos.append(response)
    
    return videos

@router.get("/{video_id}", response_model=VideoResponse)
async def get_video(video_id: str):
    """Get a specific video by ID and increment view count"""
    db = get_database()
    
    if not ObjectId.is_valid(video_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid video ID"
        )
    
    video = await db.videos.find_one({
        "_id": ObjectId(video_id),
        "is_active": True
    })
    
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found"
        )
    
    # Increment view count in buffer
    await metrics_buffer.increment_view(video_id)
    
    # Get buffered counts for immediate display
    buffered = await metrics_buffer.get_buffered_counts(video_id)
    
    video["_id"] = str(video["_id"])
    video["creator_id"] = str(video["creator_id"])
    
    # Add buffered counts to response
    response = VideoResponse(**video)
    response.buffered_views = buffered["views"]
    response.buffered_likes = buffered["likes"]
    response.buffered_comments = buffered["comments"]
    
    return response

@router.put("/{video_id}", response_model=VideoResponse)
async def update_video(
    video_id: str,
    video_update: VideoUpdate,
    current_user: str = Depends(get_current_active_user)
):
    """Update a video (only creator can update)"""
    db = get_database()
    
    # Verify ownership
    video = await db.videos.find_one({"_id": ObjectId(video_id)})
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found"
        )
    
    if str(video["creator_id"]) != current_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only update your own videos"
        )
    
    update_data = {
        k: v for k, v in video_update.dict(exclude_unset=True).items()
    }
    update_data["updated_at"] = datetime.utcnow()
    
    await db.videos.update_one(
        {"_id": ObjectId(video_id)},
        {"$set": update_data}
    )
    
    video = await db.videos.find_one({"_id": ObjectId(video_id)})
    video["_id"] = str(video["_id"])
    video["creator_id"] = str(video["creator_id"])
    
    # Get buffered counts
    buffered = await metrics_buffer.get_buffered_counts(video_id)
    
    response = VideoResponse(**video)
    response.buffered_views = buffered["views"]
    response.buffered_likes = buffered["likes"]
    response.buffered_comments = buffered["comments"]
    
    return response

@router.delete("/{video_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_video(
    video_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Soft delete a video"""
    db = get_database()
    
    # Verify ownership
    video = await db.videos.find_one({"_id": ObjectId(video_id)})
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found"
        )
    
    if str(video["creator_id"]) != current_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only delete your own videos"
        )
    
    await db.videos.update_one(
        {"_id": ObjectId(video_id)},
        {"$set": {"is_active": False, "updated_at": datetime.utcnow()}}
    )
    
    # Update user's video count
    await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {"$inc": {"videos_count": -1}}
    )

@router.post("/{video_id}/like", status_code=status.HTTP_200_OK)
async def like_video(
    video_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Like a video (buffered)"""
    db = get_database()
    
    # Check if already liked
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    if ObjectId(video_id) in user.get("liked_videos", []):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video already liked"
        )
    
    # Add to user's liked videos
    await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {"$addToSet": {"liked_videos": ObjectId(video_id)}}
    )
    
    # Increment like count in buffer
    await metrics_buffer.increment_like(video_id)
    
    # Get current buffered count for response
    buffered = await metrics_buffer.get_buffered_counts(video_id)
    
    return {
        "message": "Video liked successfully",
        "current_likes": buffered["likes"]
    }

@router.delete("/{video_id}/like", status_code=status.HTTP_200_OK)
async def unlike_video(
    video_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Unlike a video (buffered)"""
    db = get_database()
    
    # Remove from user's liked videos
    await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {"$pull": {"liked_videos": ObjectId(video_id)}}
    )
    
    # Decrement like count in buffer
    await metrics_buffer.decrement_like(video_id)
    
    # Get current buffered count for response
    buffered = await metrics_buffer.get_buffered_counts(video_id)
    
    return {
        "message": "Video unliked successfully",
        "current_likes": buffered["likes"]
    }

@router.post("/{video_id}/bookmark", status_code=status.HTTP_200_OK)
async def bookmark_video(
    video_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Bookmark a video"""
    db = get_database()
    
    # Add to user's bookmarked videos
    await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {"$addToSet": {"bookmarked_videos": ObjectId(video_id)}}
    )
    
    # Increment bookmark count directly (not buffered for MVP)
    await db.videos.update_one(
        {"_id": ObjectId(video_id)},
        {"$inc": {"bookmarks_count": 1}}
    )
    
    return {"message": "Video bookmarked successfully"}

@router.delete("/{video_id}/bookmark", status_code=status.HTTP_200_OK)
async def unbookmark_video(
    video_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Remove bookmark from a video"""
    db = get_database()
    
    # Remove from user's bookmarked videos
    await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {"$pull": {"bookmarked_videos": ObjectId(video_id)}}
    )
    
    # Decrement bookmark count
    await db.videos.update_one(
        {"_id": ObjectId(video_id)},
        {"$inc": {"bookmarks_count": -1}}
    )
    
    return {"message": "Bookmark removed successfully"}