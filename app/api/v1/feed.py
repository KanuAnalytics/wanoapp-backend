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
    description: Optional[str] = None
    thumbnail: Optional[str] = None
    views_count: int
    likes_count: int
    is_ad: bool = False
    # Include buffered counts
    buffered_views: int = 0
    buffered_likes: int = 0
    user: dict = {}
    has_liked: bool = False


@router.get("/", response_model=List[FeedVideo])
async def get_feed(
    current_user: str = Depends(get_current_active_user),
    skip: int = 0,
    limit: int = 20,
    user_id: Optional[str] = None,
    saved: bool = False,
    exclude_following: bool = False,
    sorted_by: Optional[str] = None,
):
    """Get personalized video feed, videos from a specific user, or saved videos"""
    db = get_database()

    user_doc = await db.users.find_one(
        {"_id": ObjectId(current_user)},
        {
            "liked_videos": 1,
            "blocked_users": 1,
            "blocked_by": 1,
            "following": 1,
            "localization": 1,
        },
    )

    liked_video_ids = set(str(v) for v in user_doc.get("liked_videos", []))
    blocked_users = user_doc.get("blocked_users", [])
    blocked_by = user_doc.get("blocked_by", [])

    # Single unified pipeline that handles all scenarios
    if saved:
        # For saved videos, start from users collection
        target_user_id = user_id if user_id else current_user
        pipeline = [
            {"$match": {"_id": ObjectId(target_user_id)}},
            {
                "$project": {
                    "bookmarked_videos": {
                        "$slice": ["$bookmarked_videos", skip, limit]
                    }
                }
            },
            {
                "$lookup": {
                    "from": "videos",
                    "localField": "bookmarked_videos",
                    "foreignField": "_id",
                    "as": "videos",
                }
            },
            {"$unwind": "$videos"},
            {"$match": {"videos.is_active": True}},
        ]
        cursor = db.users.aggregate(pipeline)
    else:
        # For all other cases, use videos collection with dynamic match conditions
        match_conditions = {"is_active": True}

        if user_id:
            # Get videos from specific user
            match_conditions.update(
                {
                    "creator_id": ObjectId(user_id),
                    "privacy": "public",
                }
            )
        else:
            # Get personalized feed
            user = await db.users.find_one({"_id": ObjectId(current_user)})
            exclude_creator_ids = []
            if exclude_following:
                following_ids = user.get("following", [])
                if following_ids:
                    exclude_creator_ids = [ObjectId(uid) for uid in following_ids]
            user_country = user.get("localization", {}).get("country", "NG")
            user_languages = user.get("localization", {}).get("languages", ["en"])

            match_conditions.update(
                {
                    "privacy": "public",
                    "$or": [
                        {"country": user_country},
                        {"language": {"$in": user_languages}},
                    ],
                }
            )
            if exclude_creator_ids:
                match_conditions["creator_id"] = {"$nin": exclude_creator_ids}

            # Exclude videos from blocked users
            exclude_ids = set(blocked_users + blocked_by)
            if exclude_ids:
                match_conditions["creator_id"] = {
                    **match_conditions.get("creator_id", {}),
                    "$nin": list(exclude_ids),
                }

        # 🔥 Direct sort: use the string from `sorted_by` as the field
        # Default: newest first (created_at descending)
        sort_stage = {sorted_by: -1} if sorted_by else {"created_at": -1}

        pipeline = [
            {"$match": match_conditions},
            {"$sort": sort_stage},
            {"$skip": skip},
            {"$limit": limit},
            {
                "$lookup": {
                    "from": "users",
                    "localField": "creator_id",
                    "foreignField": "_id",
                    "as": "creator",
                }
            },
            {
                "$unwind": {
                    "path": "$creator",
                    "preserveNullAndEmptyArrays": True,
                }
            },
            {
                "$project": {
                    "_id": {"$toString": "$_id"},
                    "creator_id": {"$toString": "$creator_id"},
                    "title": 1,
                    "description": 1,
                    "views_count": 1,
                    "likes_count": 1,
                    "created_at": {
                        "$dateToString": {
                            "format": "%Y-%m-%dT%H:%M:%S.%LZ",
                            "date": "$created_at",
                        }
                    },
                    "thumbnail": "$urls.thumbnail",
                    "is_active": 1,
                    "user": {
                        "username": "$creator.username",
                        "display_name": "$creator.display_name",
                        "profile_picture": "$creator.profile_picture",
                    },
                }
            },
        ]
        cursor = db.videos.aggregate(pipeline)

    videos = []
    async for doc in cursor:
        # For saved videos, video data is in doc["videos"], otherwise it's directly in doc
        video = doc.get("videos", doc) if saved else doc
        video_id = str(video["_id"])
        has_liked = video_id in liked_video_ids
        # Get buffered counts
        buffered = await metrics_buffer.get_buffered_counts(video_id)
        user_info = video.get("user", {})
        videos.append(
            FeedVideo(
                id=video_id,
                creator_id=str(video["creator_id"]),
                title=video.get("title"),
                description=video.get("description"),
                thumbnail=video.get("thumbnail"),
                views_count=video.get("views_count", 0),
                likes_count=video.get("likes_count", 0),
                is_ad=False,
                buffered_views=buffered["views"],
                buffered_likes=buffered["likes"],
                user=user_info,
                has_liked=has_liked,
            )
        )

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
            is_ad=True,
        )
        videos.insert(ad_position, ad)

    return videos