"""
User CRUD operations

app/api/v1/users.py

"""
import asyncio
import re
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Body, HTTPException, Depends, status, Query
from enum import Enum
from bson import ObjectId
from datetime import datetime
from app.models.base import PyObjectId
from app.models.user import StandardUser, ArtistUser, AdvertiserUser, AdminUser, UserType
from app.core.database import get_database
from app.core.security import get_password_hash, create_verification_token
from app.api.deps import get_current_active_user
from pydantic import BaseModel, Field, EmailStr, HttpUrl, validator
from app.models.video import VideoUrls
from app.services.email_service import email_service
import logging
from app.services.metrics_service import metrics_buffer

logger = logging.getLogger(__name__)

router = APIRouter()

class RelationshipType(str, Enum):
    followers = "followers"
    following = "following"

class UserCreate(BaseModel):
    username: str
    email: EmailStr
    password: str
    display_name: str
    user_type: UserType = UserType.STANDARD
    localization: dict
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    tags: Optional[List[str]] = Field(default_factory=list)


 #Video detail model for responses
class VideoDetail(BaseModel):
    """Video detail with id and description"""
    id: str
    description: Optional[str] = None
    title: Optional[str] = None
    thumbnail_url: Optional[str] = None
    created_at: Optional[datetime] = None
    url: Optional[VideoUrls] = None

class CompleteUserResponse(BaseModel):
    """Complete user response model with ALL fields"""
    # Basic fields
    id: str = Field(alias="_id")
    username: str
    email: EmailStr
    display_name: str
    bio: Optional[str] = None
    profile_picture: Optional[HttpUrl] = None
    cover_picture: Optional[HttpUrl] = None
    
    # New demographic fields
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    tags: List[str] = Field(default_factory=list)
    
    # User type and status
    user_type: str
    is_active: bool = True
    is_verified: bool = False
    verification_token: Optional[str] = None
    verification_token_expires: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    
    # Localization and customization
    localization: Dict[str, Any]
    theme: Optional[Dict[str, Any]] = None
    features: Dict[str, Any] = Field(default_factory=dict)
    
    # Video preferences
    video_upload_limit: int = 90
    can_upload_music: bool = False
    can_create_ads: bool = False
    
    # Statistics
    followers_count: int = 0
    following_count: int = 0
    videos_count: int = 0
    likes_count: int = 0
    
    # Arrays of IDs (complete lists)
    bookmarked_videos: List[VideoDetail] = Field(default_factory=list)
    liked_videos: List[VideoDetail] = Field(default_factory=list)
    following: List[str] = Field(default_factory=list)
    followers: List[str] = Field(default_factory=list)
    
    # Timestamps
    created_at: datetime
    updated_at: datetime
    class Config:
        populate_by_name = True
        json_encoders = {
            ObjectId: str,
            datetime: lambda v: v.isoformat() if v else None
        }



class UserWithDetailsResponse(BaseModel):
    """User response with additional computed fields"""
    user: CompleteUserResponse
    # Additional computed fields
    is_following: bool = False  # If current user follows this user
    is_followed_by: bool = False  # If this user follows current user
    mutual_followers_count: int = 0  # Number of mutual followers
    recent_videos: List[VideoDetail] = Field(default_factory=list)  # Last 5 videos with details
    
    class Config:
        json_encoders = {
            ObjectId: str,
            datetime: lambda v: v.isoformat() if v else None
        }

class UserUpdate(BaseModel):
    username: Optional[str] = None
    display_name: Optional[str] = None
    bio: Optional[str] = None
    profile_picture: Optional[str] = None
    cover_picture: Optional[str] = None
    localization: Optional[dict] = None
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    tags: Optional[List[str]] = None

class UserResponse(BaseModel):
    id: str = Field(alias="_id")
    username: str
    email: str
    display_name: str
    user_type: UserType
    is_verified: bool = False
    created_at: datetime
    followers_count: int = 0
    following_count: int = 0
    videos_count: int = 0
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    tags: List[str] = Field(default_factory=list)
    profile_picture: Optional[str] = None
    cover_picture: Optional[str] = None
    
    class Config:
        populate_by_name = True

class TagsUpdate(BaseModel):
    tags: List[str] = Field(..., description="List of user interest tags")

class FollowerResponse(BaseModel):
    """Response model for follower details"""
    id: str = Field(alias="_id")
    name: str = Field(alias="display_name")
    username: str
    picture: Optional[str] = Field(alias="profile_picture")
    
    class Config:
        populate_by_name = True
    
# Patch request model with all optional fields
class UserPatchRequest(BaseModel):
    """Patch request for updating user details - all fields optional"""
    display_name: Optional[str] = Field(None, min_length=1, max_length=100)
    bio: Optional[str] = Field(None, max_length=500)
    profile_picture: Optional[HttpUrl] = None
    cover_picture: Optional[HttpUrl] = None
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    tags: Optional[List[str]] = None
    
    # Localization updates
    localization: Optional[Dict[str, Any]] = None
    
    # Theme customization
    theme: Optional[Dict[str, Any]] = None
    
    # Feature flags (admin only or specific features)
    features: Optional[Dict[str, Any]] = None
    
    @validator('gender')
    def validate_gender(cls, v):
        if v is not None:
            valid_genders = ['male', 'female', 'other', 'prefer_not_to_say']
            if v.lower() not in valid_genders:
                raise ValueError(f'Gender must be one of: {", ".join(valid_genders)}')
            return v.lower()
        return v
    
    @validator('tags')
    def validate_tags(cls, v):
        if v is not None:
            # Normalize tags: lowercase, strip whitespace, remove duplicates
            normalized_tags = []
            for tag in v:
                normalized_tag = tag.strip().lower()
                if normalized_tag and normalized_tag not in normalized_tags:
                    normalized_tags.append(normalized_tag)
            return normalized_tags
        return v
    
    @validator('display_name')
    def validate_display_name(cls, v):
        if v is not None:
            # Remove extra whitespace
            return ' '.join(v.split())
        return v
    
    class Config:
        # Allow only the fields defined in the model
        extra = 'forbid'

# Enhanced response model with all user fields
class UserPatchResponse(BaseModel):
    """Response model for PATCH user endpoint"""
    id: str = Field(alias="_id")
    username: str
    email: EmailStr
    display_name: str
    bio: Optional[str] = None
    profile_picture: Optional[str] = None
    cover_picture: Optional[str] = None
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    tags: List[str] = Field(default_factory=list)
    localization: Dict[str, Any]
    theme: Optional[Dict[str, Any]] = None
    features: Dict[str, Any] = Field(default_factory=dict)
    is_verified: bool
    verified_at: Optional[datetime] = None
    followers_count: int = 0
    following_count: int = 0
    videos_count: int = 0
    likes_count: int = 0
    created_at: datetime
    updated_at: datetime
    
    class Config:
        populate_by_name = True

# Helper function to get user relationships (followers/following)
async def get_user_relationships(db, user_ids: List[ObjectId]) -> List[FollowerResponse]:
    """Get user details for a list of user IDs (for followers/following lists)"""
    if not user_ids:
        return []
    
    # Get users from database
    users_cursor = db.users.find(
        {"_id": {"$in": user_ids}, "is_active": True},
        {"_id": 1, "display_name": 1, "username": 1, "profile_picture": 1}
    )
    
    users = []
    async for user in users_cursor:
        users.append(FollowerResponse(
            _id=str(user["_id"]),
            display_name=user.get("display_name", ""),
            username=user["username"],
            profile_picture=user.get("profile_picture")
        ))
    
    return users

# Helper function to get video details
async def get_video_details(db, video_ids: List[ObjectId]) -> List[VideoDetail]:
    """Get video details for a list of video IDs"""
    if not video_ids:
        return []
    
    # Get videos from database
    cursor = db.videos.find({
        "_id": {"$in": video_ids},
        "is_active": True
    })
    
    # Create a mapping to preserve order
    video_map = {}
    async for video in cursor:
        video_map[video["_id"]] = VideoDetail(
            id=str(video["_id"]),
            description=video.get("description"),
            title=video.get("title"),
            thumbnail_url=video.get("urls", {}).get("thumbnail") if video.get("urls") else None,
            created_at=video.get("created_at"),
            url=video.get("urls")
        )
    
    # Return in the same order as video_ids
    return [video_map.get(vid) for vid in video_ids if vid in video_map]


@router.patch("/me", response_model=UserPatchResponse)
async def patch_user_profile(
    updates: UserPatchRequest = Body(..., example={
        "display_name": "John Doe",
        "bio": "Content creator and tech enthusiast",
        "gender": "male",
        "tags": ["technology", "gaming", "music"]
    }),
    current_user: str = Depends(get_current_active_user)
):
    """
    Partially update current user's profile
    
    - Only provided fields will be updated
    - Omitted fields remain unchanged
    - Returns the complete updated user profile
    """
    db = get_database()
    
    # Get current user
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Build update document from provided fields only
    update_doc = {}
    display_name_changed = False
    
    # Use dict to only process fields that were actually provided
    updates_dict = updates.dict(exclude_unset=True)
    
    # Process each field if provided
    if "display_name" in updates_dict:
        update_doc["display_name"] = updates.display_name
        if user["display_name"] != updates.display_name:
            display_name_changed = True
    
    if "bio" in updates_dict:
        update_doc["bio"] = updates.bio
    
    if "profile_picture" in updates_dict:
        update_doc["profile_picture"] = str(updates.profile_picture) if updates.profile_picture else None
    
    if "cover_picture" in updates_dict:
        update_doc["cover_picture"] = str(updates.cover_picture) if updates.cover_picture else None
    
    if "gender" in updates_dict:
        update_doc["gender"] = updates.gender
    
    if "date_of_birth" in updates_dict:
        update_doc["date_of_birth"] = updates.date_of_birth
    
    if "tags" in updates_dict:
        update_doc["tags"] = updates.tags
    
    # Handle nested objects
    if "localization" in updates_dict:
        # Merge with existing localization settings
        current_localization = user.get("localization", {})
        updated_localization = {**current_localization, **updates.localization}
        update_doc["localization"] = updated_localization
    
    if "theme" in updates_dict:
        update_doc["theme"] = updates.theme
    
    if "features" in updates_dict:
        # Merge with existing features
        current_features = user.get("features", {})
        updated_features = {**current_features, **updates.features}
        update_doc["features"] = updated_features
    
    # Check if any updates were provided
    if not update_doc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid fields provided for update"
        )
    
    # Add metadata
    update_doc["updated_at"] = datetime.utcnow()
    
    # Perform the update
    result = await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {"$set": update_doc}
    )
    
    if result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Update failed"
        )
    
    # If display name changed, update all user's comments in the background
    if display_name_changed:
        asyncio.create_task(
            update_user_comments_display_name(
                db,
                current_user,
                updates.display_name
            )
        )
        logger.info(f"Initiated display name update for user {current_user}'s comments")
    
    # Get and return updated user
    updated_user = await db.users.find_one({"_id": ObjectId(current_user)})
    updated_user["_id"] = str(updated_user["_id"])
    
    return UserPatchResponse(**updated_user)

@router.get("/{user_id}/complete", response_model=UserWithDetailsResponse)
async def get_user_complete(
    user_id: str,
    include_videos: bool = Query(False, description="Include user's recent videos"),
    current_user: str = Depends(get_current_active_user)
):
    """
    Get complete user data with ALL fields
    
    - Returns all user fields including video details (not just IDs)
    - Includes relationship status with current user
    - Optionally includes recent videos
    - Sensitive fields like password_hash are excluded
    """
    db = get_database()
    
    # Validate user_id
    if not ObjectId.is_valid(user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user ID format"
        )
    
    # Get the requested user
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Get video details for bookmarked and liked videos
    bookmarked_video_details = await get_video_details(
        db, 
        user.get("bookmarked_videos", [])
    )
    liked_video_details = await get_video_details(
        db, 
        user.get("liked_videos", [])
    )
    
    # Convert ObjectId fields to strings
    user_data = {
        **user,
        "_id": str(user["_id"]),
        "bookmarked_videos": bookmarked_video_details,
        "liked_videos": liked_video_details,
        "following": [str(uid) for uid in user.get("following", [])],
        "followers": [str(uid) for uid in user.get("followers", [])]
    }
    
    # Remove sensitive fields
    user_data.pop("password_hash", None)
    user_data.pop("verification_token", None)
    user_data.pop("verification_token_expires", None)
    
    # Create base response
    complete_user = CompleteUserResponse(**user_data)
    
    # Calculate relationship status
    current_user_oid = ObjectId(current_user)
    is_following = current_user_oid in user.get("followers", [])
    is_followed_by = current_user_oid in user.get("following", [])
    
    # Calculate mutual followers
    if current_user != user_id:
        current_user_data = await db.users.find_one(
            {"_id": current_user_oid},
            {"followers": 1}
        )
        if current_user_data:
            user_followers_set = set(user.get("followers", []))
            current_followers_set = set(current_user_data.get("followers", []))
            mutual_followers_count = len(user_followers_set.intersection(current_followers_set))
        else:
            mutual_followers_count = 0
    else:
        mutual_followers_count = 0
    
    # Prepare response
    response_data = {
        "user": complete_user,
        "is_following": is_following,
        "is_followed_by": is_followed_by,
        "mutual_followers_count": mutual_followers_count,
        "recent_videos": []
    }
    
    # Include recent videos if requested
    if include_videos:
        videos_cursor = db.videos.find(
            {
                "creator_id": ObjectId(user_id),
                "is_active": True,
                "privacy": "public"
            }
        ).sort("created_at", -1)
        
        recent_videos = []
        async for video in videos_cursor:
            recent_videos.append(VideoDetail(
                id=str(video["_id"]),
                description=video.get("description"),
                title=video.get("title"),
                thumbnail_url=video.get("urls", {}).get("thumbnail") if video.get("urls") else None,
                created_at=video.get("created_at")
            ))
        
        response_data["recent_videos"] = recent_videos
    
    return UserWithDetailsResponse(**response_data)

@router.get("/{user_id}/raw", response_model=Dict[str, Any])
async def get_user_raw(
    user_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """
    Get raw user data (admin endpoint or self only)
    
    - Returns absolutely ALL fields except password_hash
    - Includes internal fields
    - Restricted to user themselves or admins
    """
    db = get_database()
    
    # Check permissions - only allow user to view their own complete data
    # or implement admin check here
    if current_user != user_id:
        # Check if current user is admin
        requesting_user = await db.users.find_one({"_id": ObjectId(current_user)})
        if not requesting_user or requesting_user.get("user_type") != "ADMIN":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only view your own complete data"
            )
    
    # Get user with all fields
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Convert all ObjectIds to strings
    def convert_objectids(obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        elif isinstance(obj, list):
            return [convert_objectids(item) for item in obj]
        elif isinstance(obj, dict):
            return {key: convert_objectids(value) for key, value in obj.items()}
        elif isinstance(obj, datetime):
            return obj.isoformat()
        return obj
    
    user_dict = convert_objectids(dict(user))
    
    # Remove only the password hash for security
    user_dict.pop("password_hash", None)
    
    return user_dict

@router.get("/{user_id}/stats/detailed")
async def get_user_detailed_stats(
    user_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """
    Get detailed statistics about a user
    
    Includes:
    - Total video views
    - Total likes received
    - Engagement rate
    - Growth metrics
    - Content categories
    """
    db = get_database()
    
    # Verify user exists
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Aggregate video statistics
    pipeline = [
        {"$match": {"creator_id": ObjectId(user_id), "is_active": True}},
        {
            "$group": {
                "_id": None,
                "total_videos": {"$sum": 1},
                "total_views": {"$sum": "$views_count"},
                "total_likes": {"$sum": "$likes_count"},
                "total_comments": {"$sum": "$comments_count"},
                "total_shares": {"$sum": "$shares_count"},
                "avg_duration": {"$avg": "$duration"},
                "categories": {"$addToSet": "$category"},
                "hashtags": {"$push": "$hashtags"}
            }
        }
    ]
    
    stats_result = await db.videos.aggregate(pipeline).to_list(1)
    video_stats = stats_result[0] if stats_result else {
        "total_videos": 0,
        "total_views": 0,
        "total_likes": 0,
        "total_comments": 0,
        "total_shares": 0,
        "avg_duration": 0,
        "categories": [],
        "hashtags": []
    }
    
    # Add buffered counts to the totals
    user_videos = await db.videos.find(
        {"creator_id": ObjectId(user_id), "is_active": True}, 
        {"_id": 1}
    ).to_list(None)
    
    total_buffered_views = 0
    total_buffered_likes = 0  
    total_buffered_comments = 0
    
    for video in user_videos:
        video_id = str(video["_id"])
        # Get only the buffered portion (not total), since DB counts are already included
        async with metrics_buffer._lock:
            total_buffered_views += metrics_buffer.views_buffer.get(video_id, 0)
            total_buffered_likes += metrics_buffer.likes_buffer.get(video_id, 0)
            total_buffered_comments += metrics_buffer.comments_buffer.get(video_id, 0)
    
    # Add buffered counts to database totals
    video_stats["total_views"] += total_buffered_views
    video_stats["total_likes"] += total_buffered_likes
    video_stats["total_comments"] += total_buffered_comments
    
    # Flatten and count hashtags
    all_hashtags = []
    for hashtag_list in video_stats.get("hashtags", []):
        if isinstance(hashtag_list, list):
            all_hashtags.extend(hashtag_list)
    
    hashtag_counts = {}
    for tag in all_hashtags:
        hashtag_counts[tag] = hashtag_counts.get(tag, 0) + 1
    
    # Sort hashtags by frequency
    top_hashtags = sorted(hashtag_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    
    # Calculate engagement rate
    total_engagements = (
        video_stats["total_likes"] + 
        video_stats["total_comments"] + 
        video_stats["total_shares"]
    )
    engagement_rate = (
        (total_engagements / video_stats["total_views"] * 100) 
        if video_stats["total_views"] > 0 else 0
    )
    
    return {
        "user_id": user_id,
        "username": user["username"],
        "account_stats": {
            "followers_count": user.get("followers_count", 0),
            "following_count": user.get("following_count", 0),
            "videos_count": user.get("videos_count", 0),
            "liked_videos_count": len(user.get("liked_videos", [])),
            "bookmarked_videos_count": len(user.get("bookmarked_videos", []))
        },
        "content_stats": {
            "total_videos": video_stats["total_videos"],
            "total_views": video_stats["total_views"],
            "total_likes": video_stats["total_likes"],
            "total_comments": video_stats["total_comments"],
            "total_shares": video_stats["total_shares"],
            "average_video_duration": round(video_stats["avg_duration"], 2) if video_stats["avg_duration"] else 0,
            "engagement_rate": round(engagement_rate, 2),
            "categories": video_stats["categories"],
            "top_hashtags": [{"tag": tag, "count": count} for tag, count in top_hashtags]
        },
        "profile_info": {
            "is_verified": user.get("is_verified", False),
            "created_at": user["created_at"].isoformat(),
            "tags": user.get("tags", []),
            "localization": user.get("localization", {})
        }
    }

@router.get("/me/liked-videos", response_model=List[VideoDetail])
async def get_my_liked_videos(
    skip: int = 0,
    limit: int = 20,
    current_user: str = Depends(get_current_active_user)
):
    """Get current user's liked videos with details"""
    db = get_database()
    
    # Get user
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Get liked video IDs with pagination
    liked_video_ids = user.get("liked_videos", [])[skip:skip + limit]
    
    # Get video details
    video_details = await get_video_details(db, liked_video_ids)
    
    return video_details

@router.get("/me/bookmarked-videos", response_model=List[VideoDetail])
async def get_my_bookmarked_videos(
    skip: int = 0,
    limit: int = 20,
    current_user: str = Depends(get_current_active_user)
):
    """Get current user's bookmarked videos with details"""
    db = get_database()
    
    # Get user
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Get bookmarked video IDs with pagination
    bookmarked_video_ids = user.get("bookmarked_videos", [])[skip:skip + limit]
    
    # Get video details
    video_details = await get_video_details(db, bookmarked_video_ids)
    
    return video_details

@router.patch("/me/localization", response_model=UserPatchResponse)
async def patch_user_localization(
    country: Optional[str] = None,
    languages: Optional[List[str]] = None,
    tribes: Optional[List[str]] = None,
    current_user: str = Depends(get_current_active_user)
):
    """Update user's localization preferences"""
    db = get_database()
    
    # Build update for specific localization fields
    localization_update = {}
    if country is not None:
        localization_update["localization.country"] = country
    if languages is not None:
        localization_update["localization.languages"] = languages
    if tribes is not None:
        localization_update["localization.tribes"] = tribes
    
    if not localization_update:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No localization fields provided"
        )
    
    localization_update["updated_at"] = datetime.utcnow()
    
    # Update user
    result = await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {"$set": localization_update}
    )
    
    if result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found or update failed"
        )
    
    # Get and return updated user
    updated_user = await db.users.find_one({"_id": ObjectId(current_user)})
    updated_user["_id"] = str(updated_user["_id"])
    
    return UserPatchResponse(**updated_user)

@router.patch("/me/theme", response_model=UserPatchResponse)
async def patch_user_theme(
    primary_color: Optional[str] = None,
    accent_color: Optional[str] = None,
    font_family: Optional[str] = None,
    dark_mode: Optional[bool] = None,
    current_user: str = Depends(get_current_active_user)
):
    """Update user's theme customization"""
    db = get_database()
    
    # Get current theme
    user = await db.users.find_one(
        {"_id": ObjectId(current_user)},
        {"theme": 1}
    )
    
    current_theme = user.get("theme", {})
    
    # Merge updates with current theme
    if primary_color is not None:
        current_theme["primary_color"] = primary_color
    if accent_color is not None:
        current_theme["accent_color"] = accent_color
    if font_family is not None:
        current_theme["font_family"] = font_family
    if dark_mode is not None:
        current_theme["dark_mode"] = dark_mode
    
    # Update user
    result = await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {
            "$set": {
                "theme": current_theme,
                "updated_at": datetime.utcnow()
            }
        }
    )
    
    if result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found or update failed"
        )
    
    # Get and return updated user
    updated_user = await db.users.find_one({"_id": ObjectId(current_user)})
    updated_user["_id"] = str(updated_user["_id"])
    
    return UserPatchResponse(**updated_user)

# Helper function (should already exist from previous updates)
async def update_user_comments_display_name(db, user_id: str, new_display_name: str):
    """Update display name in all user's comments"""
    try:
        result = await db.comments.update_many(
            {"user_id": ObjectId(user_id)},
            {
                "$set": {
                    "user_display_name": new_display_name,
                    "updated_at": datetime.utcnow()
                }
            }
        )
        logger.info(f"Updated {result.modified_count} comments with new display name for user {user_id}")
    except Exception as e:
        logger.error(f"Failed to update comments display name: {e}")

@router.post("/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(user: UserCreate):
    """Create a new user"""
    db = get_database()
    
    # Check if username or email already exists
    existing_user = await db.users.find_one({
        "$or": [
            {"username": user.username},
            {"email": user.email}
        ]
    })
    
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already registered"
        )
    
    # Generate verification token
    verification_data = create_verification_token(user.email)
    
    # Create user document
    user_doc = {
        **user.dict(exclude={"password"}),
        "password_hash": get_password_hash(user.password),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "is_active": True,
        "is_verified": False,  # Not verified by default
        "verification_token": verification_data["token"],
        "verification_token_expires": verification_data["expires"],
        "verified_at": None,
        "followers_count": 0,
        "following_count": 0,
        "videos_count": 0,
        "likes_count": 0,
        "gender": user.gender,
        "date_of_birth": user.date_of_birth,
        "tags": user.tags if user.tags else []
    }
    
    # Add type-specific fields
    if user.user_type == UserType.STANDARD:
        user_doc.update({
            "bookmarked_videos": [],
            "liked_videos": [],
            "following": [],
            "followers": []
        })
    
    result = await db.users.insert_one(user_doc)
    user_doc["_id"] = str(result.inserted_id)
    
    # Send verification email (non-blocking)
    try:
        email_sent = await email_service.send_verification_email(
            user.email,
            user.username,
            verification_data["token"]
        )
        
        if not email_sent:
            logger.warning(f"Failed to send verification email to {user.email}, but user was created successfully")
    except Exception as e:
        logger.error(f"Error sending verification email: {e}")
        # Don't fail registration if email fails
    
    return UserResponse(**user_doc)

@router.get("/", response_model=List[UserResponse])
async def get_users(
    skip: int = 0,
    limit: int = 20,
    user_type: Optional[UserType] = None
):
    """Get all users with pagination"""
    db = get_database()
    
    query = {"is_active": True}
    if user_type:
        query["user_type"] = user_type
    
    cursor = db.users.find(query).skip(skip).limit(limit)
    users = []
    async for user in cursor:
        user["_id"] = str(user["_id"])
        users.append(UserResponse(**user))
    
    return users

@router.get("/{user_id}", response_model=UserResponse)
async def get_user(user_id: str):
    """Get a specific user by ID"""
    db = get_database()
    
    if not ObjectId.is_valid(user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user ID"
        )
    
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    user["_id"] = str(user["_id"])
    return UserResponse(**user)

@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    user_update: UserUpdate,
    current_user: str = Depends(get_current_active_user)
):
    """Update a user (only the user themselves can update)"""
    try:
        db = get_database()
        
    except Exception as e:
            raise e
    
    # Verify user is updating their own profile
    if user_id != current_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only update your own profile"
        )
    
    # Check for username uniqueness if username is being updated
    if user_update.username is not None:
        # Validate username format
        username = user_update.username.strip()
        if len(username) < 3:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username must be at least 3 characters long"
            )
        if len(username) > 30:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username must be at most 30 characters long"
            )
        if not re.match("^[a-zA-Z0-9_.-]+$", username):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username can only contain letters, numbers, underscores, hyphens, and periods"
            )
        
        # Keep original case for username
        user_update.username = username
        
        # Get current user to check if username is actually changing
        current_user_doc = await db.users.find_one({"_id": ObjectId(user_id)})
        if current_user_doc and current_user_doc["username"] != user_update.username:
            # Check if the new username is already taken
            existing_user = await db.users.find_one({
                "username": user_update.username,
                "_id": {"$ne": ObjectId(user_id)}  # Exclude current user
            })
            
            if existing_user:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Username is already taken"
                )
    
    update_data = {
        k: v for k, v in user_update.dict(exclude_unset=True).items()
    }
    update_data["updated_at"] = datetime.utcnow()
    
    result = await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": update_data}
    )
    
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    user["_id"] = str(user["_id"])
    return UserResponse(**user)

@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Soft delete a user"""
    db = get_database()
    
    # Only allow users to delete their own account (or admins)
    if user_id != current_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only delete your own account"
        )
    
    result = await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"is_active": False, "updated_at": datetime.utcnow()}}
    )
    
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

@router.post("/{user_id}/follow", status_code=status.HTTP_200_OK)
async def follow_user(
    user_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Follow a user"""
    db = get_database()
    
    if user_id == current_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot follow yourself"
        )
    
    # Check if target user exists
    target_user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Check if already following
    current_user_doc = await db.users.find_one({"_id": ObjectId(current_user)})
    if ObjectId(user_id) in current_user_doc.get("following", []):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Already following this user"
        )
    
    # Add to following list
    await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {
            "$addToSet": {"following": ObjectId(user_id)},
            "$inc": {"following_count": 1}
        }
    )
    
    # Add to followers list
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$addToSet": {"followers": ObjectId(current_user)},
            "$inc": {"followers_count": 1}
        }
    )
    
    return {"message": "Successfully followed user"}

@router.delete("/{user_id}/follow", status_code=status.HTTP_200_OK)
async def unfollow_user(
    user_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Unfollow a user"""
    db = get_database()
    
    # Check if actually following
    current_user_doc = await db.users.find_one({"_id": ObjectId(current_user)})
    if ObjectId(user_id) not in current_user_doc.get("following", []):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not following this user"
        )
    
    # Remove from following list
    await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {
            "$pull": {"following": ObjectId(user_id)},
            "$inc": {"following_count": -1}
        }
    )
    
    # Remove from followers list
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$pull": {"followers": ObjectId(current_user)},
            "$inc": {"followers_count": -1}
        }
    )
    
    return {"message": "Successfully unfollowed user"}

@router.get("/me/profile", response_model=UserResponse)
async def get_my_profile(current_user: str = Depends(get_current_active_user)):
    """Get current user's profile"""
    db = get_database()
    
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    user["_id"] = str(user["_id"])
    return UserResponse(**user)

@router.put("/me/tags", response_model=UserResponse)
async def update_user_tags(
    tags_update: TagsUpdate,
    current_user: str = Depends(get_current_active_user)
):
    """Update user's interest tags (replaces all existing tags)"""
    db = get_database()
    
    # Normalize tags
    normalized_tags = []
    for tag in tags_update.tags:
        normalized_tag = tag.strip().lower()
        if normalized_tag and normalized_tag not in normalized_tags:
            normalized_tags.append(normalized_tag)
    
    # Update user tags
    result = await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {
            "$set": {
                "tags": normalized_tags,
                "updated_at": datetime.utcnow()
            }
        }
    )
    
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    user["_id"] = str(user["_id"])
    return UserResponse(**user)

@router.post("/me/tags/add", response_model=UserResponse)
async def add_user_tags(
    tags_update: TagsUpdate,
    current_user: str = Depends(get_current_active_user)
):
    """Add tags to user's interests without replacing existing ones"""
    db = get_database()
    
    # Normalize new tags
    new_tags = [tag.strip().lower() for tag in tags_update.tags if tag.strip()]
    
    # Add tags using $addToSet to avoid duplicates
    result = await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {
            "$addToSet": {"tags": {"$each": new_tags}},
            "$set": {"updated_at": datetime.utcnow()}
        }
    )
    
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    user["_id"] = str(user["_id"])
    return UserResponse(**user)

@router.delete("/me/tags", response_model=UserResponse)
async def remove_user_tags(
    tags_update: TagsUpdate,
    current_user: str = Depends(get_current_active_user)
):
    """Remove specific tags from user's interests"""
    db = get_database()
    
    # Normalize tags to remove
    tags_to_remove = [tag.strip().lower() for tag in tags_update.tags if tag.strip()]
    
    # Remove tags using $pull
    result = await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {
            "$pull": {"tags": {"$in": tags_to_remove}},
            "$set": {"updated_at": datetime.utcnow()}
        }
    )
    
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    user["_id"] = str(user["_id"])
    return UserResponse(**user)

@router.put("/me", response_model=UserResponse)
async def update_me(
    user_update: UserUpdate,
    current_user: str = Depends(get_current_active_user)
):
    """Update current user's profile"""
    db = get_database()
    
    # Get current user data
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Build update document
    update_doc = {}
    display_name_changed = False
    
    if user_update.display_name is not None:
        update_doc["display_name"] = user_update.display_name
        # Check if display name is changing
        if user["display_name"] != user_update.display_name:
            display_name_changed = True
            
    if user_update.bio is not None:
        update_doc["bio"] = user_update.bio
    if user_update.profile_picture is not None:
        update_doc["profile_picture"] = user_update.profile_picture
    if user_update.gender is not None:
        update_doc["gender"] = user_update.gender
    if user_update.date_of_birth is not None:
        update_doc["date_of_birth"] = user_update.date_of_birth
    if user_update.tags is not None:
        update_doc["tags"] = user_update.tags
    
    if not update_doc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update"
        )
    
    update_doc["updated_at"] = datetime.utcnow()
    
    # Update user
    result = await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {"$set": update_doc}
    )
    
    if result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User update failed"
        )
    
    # If display name changed, update all user's comments
    if display_name_changed:
        # Run this in the background to not block the response
        asyncio.create_task(
            update_user_comments_display_name(
                db, 
                current_user, 
                user_update.display_name
            )
        )
    
    # Get updated user
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    user["_id"] = str(user["_id"])
    return UserResponse(**user)

@router.get("/{user_id}/relationships", response_model=List[FollowerResponse])
async def get_user_relationships_endpoint(
    user_id: str,
    relationship_type: RelationshipType,
    skip: Optional[int] = None,
    limit: Optional[int] = None,
):
    """Get list of user relationships (followers or following). If skip and limit are not provided, returns all."""
    db = get_database()
    
    # Validate user_id format
    if not ObjectId.is_valid(user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user ID format"
        )
    
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Get the appropriate relationship list based on type
    relationship_ids = user.get(relationship_type.value, [])
    
    if not relationship_ids:
        return []
    
    # Apply pagination only if skip and limit are provided
    if skip is not None and limit is not None:
        relationship_ids = relationship_ids[skip:skip + limit]
    
    # Get relationship details using the reusable function
    relationships = await get_user_relationships(db, relationship_ids)
    
    return relationships
