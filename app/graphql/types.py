"""
GraphQL type definitions

app/graphql/types.py

"""
import strawberry
from typing import List, Optional
from datetime import datetime
from enum import Enum

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
    
@strawberry.type
class CommentType:
    id: str
    video_id: str
    user_id: str
    user: Optional[UserType] = None
    content: str
    likes_count: int = 0
    replies_count: int = 0
    is_reply: bool = False
    parent_comment_id: Optional[str] = None
    created_at: datetime
    
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