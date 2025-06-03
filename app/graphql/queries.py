"""
GraphQL queries

app/graphql/queries.py
"""
import strawberry
from typing import List, Optional, Dict, Any
from bson import ObjectId
from app.core.database import get_database
from app.services.metrics_service import metrics_buffer
from app.graphql.types import (
    UserType, VideoType, CommentType, MusicType, 
    FeedItemType, VideoConnection, PageInfo,
    UserTypeEnum, VideoTypeEnum, VideoPrivacyEnum,
    LocalizationType, MetricsStatusType
)
from datetime import datetime


@strawberry.type
class Query:
    @strawberry.field
    async def me(self, info) -> Optional[UserType]:
        """Get current authenticated user"""
        # Get user from context (set by authentication middleware)
        user_id = info.context.get("user_id")
        if not user_id:
            return None
            
        db = get_database()
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        
        if not user:
            return None
            
        return UserType(
            id=str(user["_id"]),
            username=user["username"],
            email=user["email"],
            display_name=user["display_name"],
            bio=user.get("bio"),
            user_type=UserTypeEnum(user["user_type"]),
            localization=LocalizationType(**user["localization"]),
            followers_count=user.get("followers_count", 0),
            following_count=user.get("following_count", 0),
            videos_count=user.get("videos_count", 0),
            created_at=user["created_at"]
        )
    
    @strawberry.type
    class MetricsStatus:
        total_videos_in_buffer: int
        total_updates_pending: int
        last_flush: str
        views_pending: int
        likes_pending: int
        comments_pending: int

    
    @strawberry.field
    async def user(self, user_id: str) -> Optional[UserType]:
        """Get a user by ID"""
        db = get_database()
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        
        if not user:
            return None
            
        return UserType(
            id=str(user["_id"]),
            username=user["username"],
            email=user["email"],
            display_name=user["display_name"],
            bio=user.get("bio"),
            user_type=UserTypeEnum(user["user_type"]),
            localization=LocalizationType(**user["localization"]),
            followers_count=user.get("followers_count", 0),
            following_count=user.get("following_count", 0),
            videos_count=user.get("videos_count", 0),
            created_at=user["created_at"]
        )
    
    @strawberry.field
    async def users(
        self,
        user_type: Optional[UserTypeEnum] = None,
        limit: int = 20,
        offset: int = 0
    ) -> List[UserType]:
        """Get list of users"""
        db = get_database()
        
        query = {"is_active": True}
        if user_type:
            query["user_type"] = user_type.value
            
        cursor = db.users.find(query).skip(offset).limit(limit)
        
        users = []
        async for user in cursor:
            users.append(UserType(
                id=str(user["_id"]),
                username=user["username"],
                email=user["email"],
                display_name=user["display_name"],
                bio=user.get("bio"),
                user_type=UserTypeEnum(user["user_type"]),
                localization=LocalizationType(**user["localization"]),
                followers_count=user.get("followers_count", 0),
                following_count=user.get("following_count", 0),
                videos_count=user.get("videos_count", 0),
                created_at=user["created_at"]
            ))
            
        return users
    
    @strawberry.field
    async def video(self, video_id: str) -> Optional[VideoType]:
        """Get a video by ID"""
        db = get_database()
        video = await db.videos.find_one({
            "_id": ObjectId(video_id),
            "is_active": True
        })
        
        if not video:
            return None
            
        # Increment view count in buffer
        await metrics_buffer.increment_view(video_id)
        
        # Get buffered counts
        buffered = await metrics_buffer.get_buffered_counts(video_id)
        
        return VideoType(
            id=str(video["_id"]),
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
        )
    
    @strawberry.field
    async def videos(
        self,
        video_type: Optional[VideoTypeEnum] = None,
        privacy: Optional[VideoPrivacyEnum] = None,
        creator_id: Optional[str] = None,
        hashtag: Optional[str] = None,
        first: int = 20,
        after: Optional[str] = None
    ) -> VideoConnection:
        """Get paginated list of videos"""
        db = get_database()
        
        query = {"is_active": True}
        if video_type:
            query["video_type"] = video_type.value
        if privacy:
            query["privacy"] = privacy.value
        if creator_id:
            query["creator_id"] = ObjectId(creator_id)
        if hashtag:
            query["hashtags"] = hashtag
            
        # Handle cursor-based pagination
        if after:
            query["_id"] = {"$gt": ObjectId(after)}
            
        cursor = db.videos.find(query).limit(first + 1).sort("_id", -1)
        
        videos = []
        async for video in cursor:
            if len(videos) < first:
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
        
        has_next_page = len(videos) > first
        if has_next_page:
            videos = videos[:-1]
            
        total_count = await db.videos.count_documents(query)
        
        return VideoConnection(
            edges=videos,
            page_info=PageInfo(
                has_next_page=has_next_page,
                has_previous_page=after is not None,
                start_cursor=videos[0].id if videos else None,
                end_cursor=videos[-1].id if videos else None
            ),
            total_count=total_count
        )
    
    @strawberry.field
    async def feed(
        self,
        info,
        limit: int = 20,
        offset: int = 0
    ) -> List[FeedItemType]:
        """Get personalized video feed"""
        user_id = info.context.get("user_id")
        if not user_id:
            raise Exception("Authentication required")
            
        db = get_database()
        
        # Get user preferences
        user = await db.users.find_one({"_id": ObjectId(user_id)})
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
        
        cursor = db.videos.find(query).skip(offset).limit(limit).sort("created_at", -1)
        
        feed_items = []
        position = offset
        
        async for video in cursor:
            video_id = str(video["_id"])
            buffered = await metrics_buffer.get_buffered_counts(video_id)
            
            video_type = VideoType(
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
            )
            
            feed_items.append(FeedItemType(
                video=video_type,
                score=0.8,  # Placeholder score
                reason="Trending in your country",
                is_ad=False,
                position=position
            ))
            position += 1
            
        return feed_items
    
    @strawberry.field
    async def video_comments(
        self,
        video_id: str,
        limit: int = 20,
        offset: int = 0
    ) -> List[CommentType]:
        """Get comments for a video"""
        db = get_database()
        
        cursor = db.comments.find({
            "video_id": ObjectId(video_id),
            "is_reply": False,
            "is_active": True,
            "is_hidden": False
        }).skip(offset).limit(limit).sort("created_at", -1)
        
        comments = []
        async for comment in cursor:
            comments.append(CommentType(
                id=str(comment["_id"]),
                video_id=str(comment["video_id"]),
                user_id=str(comment["user_id"]),
                content=comment["content"],
                likes_count=comment.get("likes_count", 0),
                replies_count=comment.get("replies_count", 0),
                is_reply=comment.get("is_reply", False),
                parent_comment_id=str(comment["parent_comment_id"]) if comment.get("parent_comment_id") else None,
                created_at=comment["created_at"]
            ))
            
        return comments
    
    @strawberry.field
    async def search_videos(
        self,
        query: str,
        limit: int = 20,
        offset: int = 0
    ) -> List[VideoType]:
        """Search videos by title, description, or hashtags"""
        db = get_database()
        
        search_query = {
            "is_active": True,
            "privacy": "public",
            "$or": [
                {"title": {"$regex": query, "$options": "i"}},
                {"description": {"$regex": query, "$options": "i"}},
                {"hashtags": {"$in": [query.lower()]}}
            ]
        }
        
        cursor = db.videos.find(search_query).skip(offset).limit(limit).sort("views_count", -1)
        
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
            
        return videos
    
    @strawberry.field
    async def metrics_status(self, info) -> MetricsStatus:
        """Get current metrics buffer status (admin only)"""
        user_id = info.context.get("user_id")
        if not user_id:
            raise Exception("Authentication required")
        
        # In production, check if user is admin
        
        total_videos = len(set(
            list(metrics_buffer.views_buffer.keys()) +
            list(metrics_buffer.likes_buffer.keys()) +
            list(metrics_buffer.comments_buffer.keys())
        ))
        
        return MetricsStatus(
            total_videos_in_buffer=total_videos,
            total_updates_pending=metrics_buffer.total_updates,
            last_flush=metrics_buffer.last_flush.isoformat(),
            views_pending=sum(metrics_buffer.views_buffer.values()),
            likes_pending=sum(metrics_buffer.likes_buffer.values()),
            comments_pending=sum(metrics_buffer.comments_buffer.values())
        )