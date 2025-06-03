"""
Feed algorithm endpoint

app/api/v1/feed.py

"""
from typing import List, Optional
from fastapi import APIRouter, Depends
from app.core.database import get_database
from app.api.deps import get_current_active_user
from app.services.metrics_service import metrics_buffer
from app.models.base import PyObjectId
from pydantic import BaseModel
from bson import ObjectId
import random

router = APIRouter()

class FeedVideo(BaseModel):
    id: str
    creator_id: str
    title: Optional[str]
    views_count: int
    likes_count: int
    is_ad: bool = False
    # Include buffered counts
    buffered_views: int = 0
    buffered_likes: int = 0

@router.get("/", response_model=List[FeedVideo])
async def get_feed(
    current_user: str = Depends(get_current_active_user),
    skip: int = 0,
    limit: int = 20
):
    """Get personalized video feed"""
    db = get_database()
    
    # Get user preferences
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    user_country = user.get("localization", {}).get("country", "NG")
    user_languages = user.get("localization", {}).get("languages", ["en"])
    
    # Build query based on user preferences
    query = {
        "is_active": True,
        "privacy": "public",
        "$or": [
            {"country": user_country},
            {"language": {"$in": user_languages}}
        ]
    }
    
    # Get videos
    cursor = db.videos.find(query).skip(skip).limit(limit).sort("created_at", -1)
    
    videos = []
    async for video in cursor:
        video_id = str(video["_id"])
        
        # Get buffered counts
        buffered = await metrics_buffer.get_buffered_counts(video_id)
        
        videos.append(FeedVideo(
            id=video_id,
            creator_id=str(video["creator_id"]),
            title=video.get("title"),
            views_count=video.get("views_count", 0),
            likes_count=video.get("likes_count", 0),
            is_ad=False,
            buffered_views=buffered["views"],
            buffered_likes=buffered["likes"]
        ))
    
    # Insert ads (1:20 ratio)
    if len(videos) >= 20:
        # Insert an ad at a random position
        ad_position = random.randint(5, 15)
        # In production, fetch actual ad from campaigns
        ad = FeedVideo(
            id="ad_placeholder",
            creator_id="advertiser_id",
            title="Sponsored Content",
            views_count=0,
            likes_count=0,
            is_ad=True
        )
        videos.insert(ad_position, ad)
    
    return videos