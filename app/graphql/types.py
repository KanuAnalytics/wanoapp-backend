"""
GraphQL type definitions

app/graphql/types.py

"""
from bson import ObjectId
from bson.errors import InvalidId
import strawberry
from typing import Any, Dict, List, Optional
from datetime import datetime
from enum import Enum
from strawberry.types import Info


from app.core.database import get_database


def parse_object_id(value: Any) -> ObjectId:
    """Turn a client-supplied ID into an ObjectId, or raise a clean GraphQL error."""
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except (InvalidId, TypeError, ValueError):
        raise Exception("Invalid ID")


def _enum_or_none(enum_cls, value):
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return None


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
    country: Optional[str] = None
    languages: List[str] = strawberry.field(default_factory=list)
    tribes: Optional[List[str]] = None

@strawberry.type
class UserType:
    id: str
    username: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    bio: Optional[str] = None
    user_type: Optional[UserTypeEnum] = None
    localization: Optional[LocalizationType] = None
    followers_count: int = 0
    following_count: int = 0
    videos_count: int = 0
    created_at: Optional[datetime] = None
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    tags: List[str] = strawberry.field(default_factory=list)


def localization_from_doc(loc: Optional[Dict[str, Any]]) -> LocalizationType:
    loc = loc if isinstance(loc, dict) else {}
    languages = loc.get("languages") or []
    tribes = loc.get("tribes")
    return LocalizationType(
        country=loc.get("country"),
        languages=[str(x) for x in languages] if isinstance(languages, list) else [],
        tribes=[str(x) for x in tribes] if isinstance(tribes, list) else None,
    )


def user_from_doc(doc: Dict[str, Any], viewer_id: Optional[str] = None) -> UserType:
    """Build a UserType. Private fields (email, gender, date of birth) are only
    included when the viewer is that user."""
    uid = str(doc["_id"])
    is_self = viewer_id is not None and str(viewer_id) == uid
    tags = doc.get("tags") or []
    return UserType(
        id=uid,
        username=doc.get("username") or "",
        email=doc.get("email") if is_self else None,
        display_name=doc.get("display_name"),
        bio=doc.get("bio"),
        user_type=_enum_or_none(UserTypeEnum, doc.get("user_type")),
        localization=localization_from_doc(doc.get("localization")),
        followers_count=doc.get("followers_count", 0) or 0,
        following_count=doc.get("following_count", 0) or 0,
        videos_count=doc.get("videos_count", 0) or 0,
        created_at=doc.get("created_at"),
        gender=doc.get("gender") if is_self else None,
        date_of_birth=doc.get("date_of_birth") if is_self else None,
        tags=[str(t) for t in tags] if isinstance(tags, list) else [],
    )


@strawberry.type
class ResponseType:
    message: str
    status: str

@strawberry.type
class VideoType:
    id: str
    creator_id: str
    creator: Optional[UserType] = None
    title: Optional[str] = None
    description: Optional[str] = None
    video_type: Optional[VideoTypeEnum] = None
    privacy: Optional[VideoPrivacyEnum] = None
    views_count: int = 0
    likes_count: int = 0
    comments_count: int = 0
    shares_count: int = 0
    hashtags: List[str] = strawberry.field(default_factory=list)
    categories: List[str] = strawberry.field(default_factory=list)
    created_at: Optional[datetime] = None
    # Buffered counts
    buffered_views: int = 0
    buffered_likes: int = 0
    buffered_comments: int = 0

    FEid: Optional[str] = None
    start: float = 0.0
    end: float = 0.0
    remoteUrl: Optional[str] = None
    remoteUrl_CF: Optional[str] = None
    duration: float = 0.0
    type: Optional[str] = None


def _num(value, default=0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def video_from_doc(doc: Dict[str, Any], buffered: Optional[Dict[str, int]] = None) -> VideoType:
    """Build a VideoType from a Mongo video document, tolerating missing fields."""
    buffered = buffered or {}
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    duration = _num(doc.get("duration", metadata.get("duration")), 0.0)
    hashtags = doc.get("hashtags") or []
    categories = doc.get("categories") or []
    return VideoType(
        id=str(doc["_id"]),
        creator_id=str(doc.get("creator_id") or ""),
        title=doc.get("title"),
        description=doc.get("description"),
        video_type=_enum_or_none(VideoTypeEnum, doc.get("video_type")),
        privacy=_enum_or_none(VideoPrivacyEnum, doc.get("privacy")),
        views_count=doc.get("views_count", 0) or 0,
        likes_count=doc.get("likes_count", 0) or 0,
        comments_count=doc.get("comments_count", 0) or 0,
        shares_count=doc.get("shares_count", 0) or 0,
        hashtags=[str(h) for h in hashtags] if isinstance(hashtags, list) else [],
        categories=[str(c) for c in categories] if isinstance(categories, list) else [],
        created_at=doc.get("created_at"),
        buffered_views=buffered.get("views", 0),
        buffered_likes=buffered.get("likes", 0),
        buffered_comments=buffered.get("comments", 0),
        FEid=str(doc["FEid"]) if doc.get("FEid") is not None else None,
        start=_num(doc.get("start"), 0.0),
        end=_num(doc.get("end"), duration),
        remoteUrl=doc.get("remoteUrl"),
        remoteUrl_CF=doc.get("remoteUrl_CF"),
        duration=duration,
        type=doc.get("type") or doc.get("media_type"),
    )


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
    user_display_name: Optional[str] = None
    content: str = ""
    parent_id: Optional[str] = None
    likes_count: int = 0
    replies_count: int = 0
    is_liked: bool = False
    is_edited: bool = False
    edited_at: Optional[datetime] = None
    is_pinned: bool = False
    is_hearted: bool = False
    created_at: Optional[datetime] = None

    @strawberry.field
    async def user(self, info: Info) -> Optional[UserType]:
        """Get the user who made this comment"""
        db = get_database()
        try:
            oid = ObjectId(self.user_id)
        except (InvalidId, TypeError):
            return None
        user = await db.users.find_one({"_id": oid})
        if user:
            return user_from_doc(user, info.context.get("user_id"))
        return None

    @strawberry.field
    async def replies(self, info: Info, limit: int = 10) -> List["CommentType"]:
        """Get replies to this comment"""
        db = get_database()
        limit = max(1, min(limit, 50))
        cursor = db.comments.find({
            "parent_id": ObjectId(self.id),
            "is_active": True
        }).sort("created_at", 1).limit(limit)

        viewer = info.context.get("user_id")
        return [comment_from_doc(reply, viewer) async for reply in cursor]


def comment_from_doc(doc: Dict[str, Any], viewer_id: Optional[str] = None) -> CommentType:
    """Build a CommentType from a Mongo comment document (REST schema)."""
    is_liked = False
    if viewer_id:
        try:
            is_liked = ObjectId(viewer_id) in (doc.get("liked_by") or [])
        except (InvalidId, TypeError):
            is_liked = False
    parent = doc.get("parent_id")
    return CommentType(
        id=str(doc["_id"]),
        video_id=str(doc.get("video_id") or ""),
        user_id=str(doc.get("user_id") or ""),
        user_display_name=doc.get("user_display_name"),
        content=doc.get("content") or "",
        parent_id=str(parent) if parent else None,
        likes_count=doc.get("likes_count", 0) or 0,
        replies_count=doc.get("replies_count", 0) or 0,
        is_liked=is_liked,
        is_edited=bool(doc.get("is_edited", False)),
        edited_at=doc.get("edited_at"),
        is_pinned=bool(doc.get("is_pinned", False)),
        is_hearted=bool(doc.get("is_hearted", False)),
        created_at=doc.get("created_at"),
    )
