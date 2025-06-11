"""
GraphQL type definitions

app/graphql/types.py

"""
from bson import ObjectId
import strawberry
from typing import List, Optional
from datetime import datetime
from enum import Enum

from app.core.database import get_database

@strawberry.enum
class UserTypeEnum(Enum):
    STANDARD = "standard"
    ARTIST = "artist"
    ADVERTISER = "advertiser"
    ADMIN = "admin"

@strawberry.enum
class VideoTypeEnum(Enum):
    REGULAR = "regular"
    BITS = "bits"

@strawberry.enum
class VideoPrivacyEnum(Enum):
    PUBLIC = "public"
    UNLISTED = "unlisted"
    PRIVATE = "private"

@strawberry.type
class LocalizationType:
    country: str
    languages: List[str]
    tribes: Optional[List[str]] = None

@strawberry.type
class UserType:
    id: str
    username: str
    email: str
    display_name: str
    bio: Optional[str] = None
    user_type: UserTypeEnum
    localization: LocalizationType
    followers_count: int = 0
    following_count: int = 0
    videos_count: int = 0
    created_at: datetime
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    tags: List[str] = strawberry.field(default_factory=list)
    
@strawberry.type
class VideoType:
    id: str
    creator_id: str
    creator: Optional[UserType] = None
    title: Optional[str] = None
    description: Optional[str] = None
    video_type: VideoTypeEnum
    privacy: VideoPrivacyEnum
    views_count: int = 0
    likes_count: int = 0
    comments_count: int = 0
    shares_count: int = 0
    hashtags: List[str] = strawberry.field(default_factory=list)
    categories: List[str] = strawberry.field(default_factory=list)
    created_at: datetime
    # Buffered counts
    buffered_views: int = 0
    buffered_likes: int = 0
    buffered_comments: int = 0

    FEid: Optional[str] = None
    start: float = 0.0
    end: float = 0.0
    remoteUrl: Optional[str] = None
    duration: float=0.0
    type: str
    
# @strawberry.type
# class CommentType:
#     id: str
#     video_id: str
#     user_id: str
#     user: Optional[UserType] = None
#     content: str
#     likes_count: int = 0
#     replies_count: int = 0
#     is_reply: bool = False
#     parent_comment_id: Optional[str] = None
#     created_at: datetime
    
@strawberry.type
class MusicType:
    id: str
    artist_id: str
    artist: Optional[UserType] = None
    title: str
    artist_name: str
    album: Optional[str] = None
    genre: List[str] = strawberry.field(default_factory=list)
    duration: float
    is_approved: bool = False
    usage_count: int = 0
    created_at: datetime

@strawberry.type
class FeedItemType:
    video: VideoType
    score: float
    reason: str
    is_ad: bool = False
    position: int

@strawberry.type
class PageInfo:
    has_next_page: bool
    has_previous_page: bool
    start_cursor: Optional[str] = None
    end_cursor: Optional[str] = None

@strawberry.type
class VideoConnection:
    edges: List[VideoType]
    page_info: PageInfo
    total_count: int

@strawberry.type
class AuthPayload:
    token: str
    user: UserType

@strawberry.type
class MetricsStatusType:
    total_videos_in_buffer: int
    total_updates_pending: int
    last_flush: str
    views_pending: int
    likes_pending: int
    comments_pending: int
    
@strawberry.type
class CommentType:
    id: str
    video_id: str
    user_id: str
    user_display_name: str  # New field
    content: str
    parent_id: Optional[str] = None
    likes_count: int = 0
    replies_count: int = 0
    is_liked: bool = False
    is_edited: bool = False
    edited_at: Optional[datetime] = None
    is_pinned: bool = False
    is_hearted: bool = False
    created_at: datetime
    
    @strawberry.field
    async def user(self) -> Optional[UserType]:
        """Get the user who made this comment"""
        db = get_database()
        user = await db.users.find_one({"_id": ObjectId(self.user_id)})
        if user:
            return UserType(
                id=str(user["_id"]),
                username=user["username"],
                display_name=user["display_name"],
                # ... other user fields
            )
        return None
    
    @strawberry.field
    async def replies(self, limit: int = 10) -> List["CommentType"]:
        """Get replies to this comment"""
        db = get_database()
        cursor = db.comments.find({
            "parent_id": ObjectId(self.id),
            "is_active": True
        }).sort("created_at", 1).limit(limit)
        
        replies = []
        async for reply in cursor:
            replies.append(CommentType(
                id=str(reply["_id"]),
                video_id=str(reply["video_id"]),
                user_id=str(reply["user_id"]),
                user_display_name=reply["user_display_name"],  # Use cached name
                content=reply["content"],
                parent_id=str(reply["parent_id"]),
                likes_count=reply.get("likes_count", 0),
                replies_count=reply.get("replies_count", 0),
                is_edited=reply.get("is_edited", False),
                edited_at=reply.get("edited_at"),
                is_pinned=reply.get("is_pinned", False),
                is_hearted=reply.get("is_hearted", False),
                created_at=reply["created_at"]
            ))
        
        return replies

@strawberry.field
async def comments(
    self, 
    info: strawberry.Info,
    limit: int = 20, 
    skip: int = 0
) -> List[CommentType]:
    """Get comments for this video with cached display names"""
    db = get_database()
    user_id = info.context.get("user_id")
    
    # Get top-level comments only (no parent_id)
    cursor = db.comments.find({
        "video_id": ObjectId(self.id),
        "parent_id": None,
        "is_active": True
    }).sort([
        ("is_pinned", -1),  # Pinned comments first
        ("created_at", -1)   # Then newest
    ]).skip(skip).limit(limit)
    
    comments = []
    async for comment in cursor:
        # Check if current user liked this comment
        is_liked = False
        if user_id:
            is_liked = ObjectId(user_id) in comment.get("liked_by", [])
        
        comments.append(CommentType(
            id=str(comment["_id"]),
            video_id=str(comment["video_id"]),
            user_id=str(comment["user_id"]),
            user_display_name=comment["user_display_name"],  # Use cached name
            content=comment["content"],
            parent_id=None,
            likes_count=comment.get("likes_count", 0),
            replies_count=comment.get("replies_count", 0),
            is_liked=is_liked,
            is_edited=comment.get("is_edited", False),
            edited_at=comment.get("edited_at"),
            is_pinned=comment.get("is_pinned", False),
            is_hearted=comment.get("is_hearted", False),
            created_at=comment["created_at"]
        ))
    
    return comments
