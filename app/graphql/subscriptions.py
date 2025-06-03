"""
GraphQL subscriptions for real-time updates

app/graphql/subscriptions.py

"""
import strawberry
from typing import AsyncGenerator, Optional, List
import asyncio
from app.graphql.types import VideoType, CommentType, UserType, VideoTypeEnum, VideoPrivacyEnum
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
    async def video_metrics(self, video_id: str) -> AsyncGenerator[VideoMetrics, None]:
        """Subscribe to video metrics updates (views, likes, comments)"""
        db = get_database()
        
        while True:
            # Get current database counts
            video = await db.videos.find_one({"_id": ObjectId(video_id)})
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
    async def new_comments(self, video_id: str) -> AsyncGenerator[CommentType, None]:
        """Subscribe to new comments on a video"""
        db = get_database()
        last_check = datetime.utcnow()
        
        while True:
            await asyncio.sleep(3)  # Check every 3 seconds
            
            # Find new comments since last check
            cursor = db.comments.find({
                "video_id": ObjectId(video_id),
                "created_at": {"$gt": last_check},
                "is_active": True
            })
            
            async for comment in cursor:
                yield CommentType(
                    id=str(comment["_id"]),
                    video_id=str(comment["video_id"]),
                    user_id=str(comment["user_id"]),
                    content=comment["content"],
                    likes_count=comment.get("likes_count", 0),
                    replies_count=comment.get("replies_count", 0),
                    is_reply=comment.get("is_reply", False),
                    parent_comment_id=str(comment["parent_comment_id"]) if comment.get("parent_comment_id") else None,
                    created_at=comment["created_at"]
                )
            
            last_check = datetime.utcnow()
    
    @strawberry.subscription
    async def follower_count(self, user_id: str) -> AsyncGenerator[int, None]:
        """Subscribe to follower count updates for a user"""
        db = get_database()
        
        while True:
            user = await db.users.find_one({"_id": ObjectId(user_id)})
            if user:
                yield user.get("followers_count", 0)
            
            await asyncio.sleep(10)  # Check every 10 seconds
    
    @strawberry.subscription
    async def trending_videos(self, country: Optional[str] = None) -> AsyncGenerator[List[VideoType], None]:
        """Subscribe to trending videos updates"""
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
                
                videos.append(VideoType(
                    id=video_id,
                    creator_id=str(video["creator_id"]),
                    title=video.get("title"),
                    description=video.get("description"),
                    video_type=VideoTypeEnum(video["video_type"]),
                    privacy=VideoPrivacyEnum(video["privacy"]),
                    views_count=video.get("views_count", 0),
                    likes_count=video.get("likes_count", 0),
                    comments_count=video.get("comments_count", 0),
                    shares_count=video.get("shares_count", 0),
                    hashtags=video.get("hashtags", []),
                    categories=video.get("categories", []),
                    created_at=video["created_at"],
                    buffered_views=buffered["views"],
                    buffered_likes=buffered["likes"],
                    buffered_comments=buffered["comments"]
                ))
            
            yield videos
            
            await asyncio.sleep(60)  # Update every minute