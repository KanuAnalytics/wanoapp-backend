"""
GraphQL subscriptions for real-time updates

app/graphql/subscriptions.py

"""
import strawberry
from typing import AsyncGenerator, Optional, List
import asyncio
from app.graphql.types import (
    VideoType, CommentType, UserType, VideoTypeEnum, VideoPrivacyEnum,
    parse_object_id, video_from_doc, comment_from_doc,
)
from app.graphql.auth import require_user_id, visible_video_filter
from datetime import datetime
from bson import ObjectId
from app.core.database import get_database
from app.services.metrics_service import metrics_buffer

# Define the return types for subscriptions
@strawberry.type
class VideoMetrics:
    views_count: int
    likes_count: int
    comments_count: int
    buffered_views: int
    buffered_likes: int
    buffered_comments: int
    total_views: int
    total_likes: int
    total_comments: int

@strawberry.type
class NotificationType:
    id: str
    user_id: str
    type: str
    title: str
    message: str
    from_user_id: Optional[str] = None
    video_id: Optional[str] = None
    is_read: bool = False
    created_at: datetime

@strawberry.type
class Subscription:
    @strawberry.subscription
    async def video_metrics(self, info, video_id: str) -> AsyncGenerator[VideoMetrics, None]:
        """Subscribe to video metrics updates (views, likes, comments)"""
        viewer = require_user_id(info)
        vq = visible_video_filter(viewer)
        vq["_id"] = parse_object_id(video_id)
        db = get_database()
        
        while True:
            # Get current database counts
            video = await db.videos.find_one(vq)
            if video:
                # Get buffered counts
                buffered = await metrics_buffer.get_buffered_counts(video_id)
                
                yield VideoMetrics(
                    views_count=video.get("views_count", 0),
                    likes_count=video.get("likes_count", 0),
                    comments_count=video.get("comments_count", 0),
                    buffered_views=buffered["views"],
                    buffered_likes=buffered["likes"],
                    buffered_comments=buffered["comments"],
                    total_views=video.get("views_count", 0) + buffered["views"],
                    total_likes=video.get("likes_count", 0) + buffered["likes"],
                    total_comments=video.get("comments_count", 0) + buffered["comments"]
                )
            
            await asyncio.sleep(5)  # Update every 5 seconds
    
    @strawberry.subscription
    async def new_comments(self, info, video_id: str) -> AsyncGenerator[CommentType, None]:
        """Subscribe to new comments on a video"""
        viewer = require_user_id(info)
        vid = parse_object_id(video_id)
        vq = visible_video_filter(viewer)
        vq["_id"] = vid
        db = get_database()
        if not await db.videos.find_one(vq, {"_id": 1}):
            raise Exception("Video not found")
        last_check = datetime.utcnow()
        
        while True:
            await asyncio.sleep(3)  # Check every 3 seconds
            
            # Find new comments since last check
            cursor = db.comments.find({
                "video_id": vid,
                "created_at": {"$gt": last_check},
                "is_active": True
            })
            
            async for comment in cursor:
                yield comment_from_doc(comment, viewer)
            
            last_check = datetime.utcnow()
    
    @strawberry.subscription
    async def follower_count(self, info, user_id: str) -> AsyncGenerator[int, None]:
        """Subscribe to follower count updates for a user"""
        require_user_id(info)
        uid = parse_object_id(user_id)
        db = get_database()
        
        while True:
            user = await db.users.find_one({"_id": uid})
            if user:
                yield user.get("followers_count", 0)
            
            await asyncio.sleep(10)  # Check every 10 seconds
    
    @strawberry.subscription
    async def trending_videos(self, info, country: Optional[str] = None) -> AsyncGenerator[List[VideoType], None]:
        """Subscribe to trending videos updates"""
        require_user_id(info)
        db = get_database()
        
        while True:
            # Build query
            query = {"is_active": True, "privacy": "public"}
            if country:
                query["country"] = country
            
            # Get top 10 trending videos by views in last 24 hours
            cursor = db.videos.find(query).sort([
                ("views_count", -1),
                ("likes_count", -1)
            ]).limit(10)
            
            videos = []
            async for video in cursor:
                video_id = str(video["_id"])
                buffered = await metrics_buffer.get_buffered_counts(video_id)
                
                videos.append(video_from_doc(video, buffered))
            
            yield videos
            
            await asyncio.sleep(60)  # Update every minute