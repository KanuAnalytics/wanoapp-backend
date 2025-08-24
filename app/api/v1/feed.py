"""
Feed algorithm endpoint

app/api/v1/feed.py

"""
from typing import List, Optional
from fastapi import APIRouter, Depends
from app.core.database import get_database
from app.api.deps import get_current_active_user
from app.services.metrics_service import metrics_buffer
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
    limit: int = 20,
    user_id: Optional[str] = None,
    saved: bool = False
):
    """Get personalized video feed, videos from a specific user, or saved videos"""
    db = get_database()
    
    # Single unified pipeline that handles all scenarios
    if saved:
        # For saved videos, start from users collection
        target_user_id = user_id if user_id else current_user
        pipeline = [
            {"$match": {"_id": ObjectId(target_user_id)}},
            {"$project": {"bookmarked_videos": {"$slice": ["$bookmarked_videos", skip, limit]}}},
            {"$lookup": {
                "from": "videos",
                "localField": "bookmarked_videos",
                "foreignField": "_id",
                "as": "videos"
            }},
            {"$unwind": "$videos"},
            {"$match": {"videos.is_active": True}}
        ]
        cursor = db.users.aggregate(pipeline)
    else:
        # For all other cases, use videos collection with dynamic match conditions
        match_conditions = {"is_active": True}
        
        if user_id:
            # Get videos from specific user
            match_conditions.update({
                "creator_id": ObjectId(user_id),
                "privacy": "public"
            })
        else:
            # Get personalized feed
            user = await db.users.find_one({"_id": ObjectId(current_user)})
            user_country = user.get("localization", {}).get("country", "NG")
            user_languages = user.get("localization", {}).get("languages", ["en"])
            
            match_conditions.update({
                "privacy": "public",
                "$or": [
                    {"country": user_country},
                    {"language": {"$in": user_languages}}
                ]
            })
        
        pipeline = [
            {"$match": match_conditions},
            {"$sort": {"created_at": -1}},
            {"$skip": skip},
            {"$limit": limit}
        ]
        cursor = db.videos.aggregate(pipeline)
    
    videos = []
    async for doc in cursor:
        # For saved videos, video data is in doc["videos"], otherwise it's directly in doc
        video = doc.get("videos", doc) if saved else doc
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
    
    # Insert ads (1:20 ratio) - only for personalized feed, not for specific user videos or saved videos
    if not user_id and not saved and len(videos) >= 20:
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