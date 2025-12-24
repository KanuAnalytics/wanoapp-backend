"""
Video CRUD operations with buffered metrics

app/api/v1/vidoes.py

"""
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends, status, UploadFile, File, Form, Query, BackgroundTasks
from bson import ObjectId
from datetime import datetime
from app.models.video import Video, VideoType, VideoPrivacy
from app.core.database import get_database
from app.api.deps import get_current_active_user, get_verified_user
from app.services.expo import send_push_message
from app.services.metrics_service import metrics_buffer
from pydantic import BaseModel,HttpUrl, Field
import re
import json
from bson.json_util import dumps
from app.models.user import UserType

router = APIRouter()

class VideoPost(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    video_type: Optional[VideoType] = VideoType.REGULAR
    privacy: VideoPrivacy = VideoPrivacy.PUBLIC
    remoteUrl: str
    thumbnail: Optional[str] = 'https://wano-africadev.lon1.digitaloceanspaces.com/wanoafrica-dospaces-key/profile-pictures/thumbnail_placeholder.png'
    duration: Optional[float] = 0.0
    start: Optional[float] = 0.0
    end: Optional[float] = None
    remix_enabled: Optional[bool] = True
    comments_enabled: bool = True
    categoryId: Optional[str] = None
    subcategoryId: Optional[str] = None
    isReadyToStream: Optional[bool] = False

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
    
    buffered_views: int = 0
    buffered_likes: int = 0
    buffered_comments: int = 0

    FEid: Optional[str] = None
    start: float = 0.0
    end: Optional[float] = None
    remoteUrl: Optional[str] = None
    
    is_following: Optional[bool] = None
    is_liked: Optional[bool] = None  

    class Config:
        populate_by_name = True

@router.post("/post", status_code=status.HTTP_201_CREATED)
async def post_video(
    input: VideoPost,
    current_user: str = Depends(get_verified_user)
):
    try: 
        """Endpoint to handle video posting logic"""
        db = get_database()
        user = await db.users.find_one({"_id": ObjectId(current_user)})
        
        video_doc = {
            "creator_id": ObjectId(current_user),
            "title": input.title,  # Can be updated later by user
            "description": input.description,
            "video_type": "regular",
            "privacy": input.privacy,
            "isReadyToStream": input.isReadyToStream,
            "metadata": {
                "duration": input.duration,
                "width": 1080,  # You might want to detect this from the actual video
                "height": 1920,
                "fps": 30.0,
                "file_size": 0  # You can calculate this during upload
            },
            "urls": {
                "original": input.remoteUrl,
                "hls_playlist": input.remoteUrl,  # In production, generate HLS separately
                "thumbnail": input.thumbnail,  # In production, generate thumbnail separately
                "download": input.remoteUrl
            },
            "categoryId" : input.categoryId,
            "subcategoryId" : input.subcategoryId,
            # Additional fields for compatibility
            "FEid": None,
            "start": 0,
            "end": input.end,
            "duration": input.duration,
            "remoteUrl": input.remoteUrl,
            "type": 'video',
            # Standard fields
            "hashtags": [],
            "categories": [],
            "remix_enabled": True,
            "comments_enabled": input.comments_enabled,
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
            "country": user.get("localization", {}).get("country", "NG"),
            "language": user.get("localization", {}).get("languages", ["en"])[0]
        }
        
        # Insert into database
        result = await db.videos.insert_one(video_doc)
        
        # Update user's video count
        await db.users.update_one(
            {"_id": ObjectId(current_user)},
            {"$inc": {"videos_count": 1}}
        )
        
        # This is a placeholder implementation
        return {"message": "Video posted successfully", "video_id": str(result.inserted_id)}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
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

# --- Search endpoint ---
@router.get("/search")
async def search_videos(
    q: str = Query(..., description="Search query"),
    skip: int = 0,
    limit: int = 20,
):
    """
    Search videos by description field only.
    """
    db = get_database()
    regex = {"$regex": f"^{re.escape(q.strip())}", "$options": "i"}
    pipeline = [
        {"$match": {"description": regex, "is_active": True}},        
        {"$sort": {"created_at": -1}},
        {"$skip": skip},
        {"$limit": limit},
        {"$lookup": {
            "from": "users",
            "localField": "creator_id",
            "foreignField": "_id",
            "as": "creator"
        }},
        {"$unwind": {"path": "$creator", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "_id": {"$toString": "$_id"},
            "creator_id": {"$toString": "$creator_id"},
            "description": 1,
            "created_at": {"$dateToString": {"format": "%Y-%m-%dT%H:%M:%S.%LZ", "date": "$created_at"}},
            "thumbnail": "$urls.thumbnail",
            "user": {
                "username": "$creator.username",
                "display_name": "$creator.display_name",
                "profile_picture": "$creator.profile_picture"
            }
        }}
    ]
    cursor = db.videos.aggregate(pipeline)
    docs = await cursor.to_list(length=limit)
    return json.loads(dumps(docs))

@router.get("/{video_id}", response_model=VideoResponse)
async def get_video(
    video_id: str,
    current_user: str = Depends(get_current_active_user),
    background_tasks: BackgroundTasks = None,
):
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

    total_views = video.get("views_count", 0) + buffered["views"]
    if total_views == 25 and background_tasks is not None:
        recipient = await db.users.find_one(
            {"_id": video["creator_id"]},
            {"expo_push_tokens": 1},
        )
        recipient_tokens = (recipient or {}).get("expo_push_tokens") or []
        thumbnail_url = (video.get("urls") or {}).get("thumbnail")
        description = (video.get("description") or "").strip()
        description_preview = description[:30] + ("..." if len(description) > 30 else "")
        for token in recipient_tokens:
            background_tasks.add_task(
                send_push_message,
                token,
                description_preview,
                {"video_id": video_id},
                "Your post is getting attention 🔥",
                thumbnail_url,
            )

    milestone_labels = {50: "50", 100: "100", 1000: "1k", 5000: "5k"}
    if total_views in milestone_labels and background_tasks is not None:
        recipient = await db.users.find_one(
            {"_id": video["creator_id"]},
            {"expo_push_tokens": 1},
        )
        recipient_tokens = (recipient or {}).get("expo_push_tokens") or []
        thumbnail_url = (video.get("urls") or {}).get("thumbnail")
        title = f"Your video just hit {milestone_labels[total_views]} views"
        description = (video.get("description") or "").strip()
        description_preview = description[:30] + ("..." if len(description) > 30 else "")
        for token in recipient_tokens:
            background_tasks.add_task(
                send_push_message,
                token,
                description_preview,
                {"video_id": video_id},
                title,
                thumbnail_url,
            )
    
    # Get current user data
    user_doc = await db.users.find_one({"_id": ObjectId(current_user)})

    # Check if current user has liked this video
    is_liked = ObjectId(video_id) in (user_doc.get("liked_videos") or [])
    
    # Check if current user is following the video creator
    creator_doc = await db.users.find_one({"_id": video["creator_id"]})
    current_user_oid = ObjectId(current_user)
    is_following = current_user_oid in creator_doc.get("followers", [])
    
    video["_id"] = str(video["_id"])
    video["creator_id"] = str(video["creator_id"])
    
    response = VideoResponse(**video)
    response.buffered_views = buffered["views"]
    response.buffered_likes = buffered["likes"]
    response.buffered_comments = buffered["comments"]
    response.is_following = is_following
    response.is_liked = is_liked
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
    """Soft delete a video. Only creator or admin can delete."""
    db = get_database()

    # Get current user document
    user_doc = await db.users.find_one({"_id": ObjectId(current_user)})
    if not user_doc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )

    # Verify video exists
    video = await db.videos.find_one({"_id": ObjectId(video_id)})
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found"
        )

    # Check permissions (creator or admin)
    is_creator = str(video["creator_id"]) == current_user
    is_admin = user_doc.get("user_type") == UserType.ADMIN  # adjust if your field is different

    if not (is_creator or is_admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to delete this video"
        )

    # Soft delete video
    await db.videos.update_one(
        {"_id": ObjectId(video_id)},
        {"$set": {"is_active": False, "updated_at": datetime.utcnow()}}
    )

    # Decrement creator’s video count ONLY if the creator deleted it
    # Or if admin deleted it, decrement the *actual owner's* count
    await db.users.update_one(
        {"_id": video["creator_id"]},
        {"$inc": {"videos_count": -1}}
    )

    return {"message": "Video deleted successfully"}  # Optional for 204

@router.post("/{video_id}/like", status_code=status.HTTP_200_OK)
async def like_video(
    video_id: str,
    current_user: str = Depends(get_current_active_user),
    background_tasks: BackgroundTasks = None
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

    video = await db.videos.find_one(
        {"_id": ObjectId(video_id)},
        {"description": 1, "urls.thumbnail": 1, "thumbnail": 1, "creator_id": 1},
    )
    
    # Increment like count in buffer
    await metrics_buffer.increment_like(video_id)
    
    # Get current buffered count for response
    buffered = await metrics_buffer.get_buffered_counts(video_id)

    if background_tasks is not None:
        display_name = user.get("display_name") or user.get("username") or "Someone"
        description = (video or {}).get("description") or ""
        description = description.strip()
        description_preview = description[:30] + ("..." if len(description) > 30 else "")
        thumbnail_url = None
        if video:
            thumbnail_url = (video.get("urls") or {}).get("thumbnail")
        recipient_tokens = []
        creator_id = (video or {}).get("creator_id")
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
                description_preview,
                {"video_id": video_id, "liker_id": str(current_user)},
                f"{display_name} liked your video",
                thumbnail_url,
            )
    
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
    current_user: str = Depends(get_current_active_user),
    background_tasks: BackgroundTasks = None,
):
    """Bookmark a video"""
    db = get_database()

    user = await db.users.find_one({"_id": ObjectId(current_user)})
    video = await db.videos.find_one(
        {"_id": ObjectId(video_id)},
        {"description": 1, "urls.thumbnail": 1, "creator_id": 1},
    )
    
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

    if background_tasks is not None and video:
        creator_id = video.get("creator_id")
        if creator_id and str(creator_id) != str(current_user):
            recipient = await db.users.find_one(
                {"_id": ObjectId(creator_id)},
                {"expo_push_tokens": 1},
            )
            recipient_tokens = (recipient or {}).get("expo_push_tokens") or []
            display_name = (user or {}).get("display_name") or (user or {}).get("username") or "Someone"
            description = (video.get("description") or "").strip()
            description_preview = description[:30] + ("..." if len(description) > 30 else "")
            thumbnail_url = (video.get("urls") or {}).get("thumbnail")
            for token in recipient_tokens:
                background_tasks.add_task(
                    send_push_message,
                    token,
                    description_preview,
                    {"video_id": video_id, "saver_id": str(current_user)},
                    f"{display_name} saved your video",
                    thumbnail_url,
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
