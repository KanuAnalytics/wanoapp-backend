"""
GraphQL queries

app/graphql/queries.py
"""
import re
import strawberry
from typing import List, Optional
from app.core.database import get_database
from app.services.metrics_service import metrics_buffer
from app.services import trending
from app.graphql.types import (
    UserType, VideoType, CommentType, MusicType,
    FeedItemType, VideoConnection, PageInfo,
    UserTypeEnum, VideoTypeEnum, VideoPrivacyEnum,
    LocalizationType, MetricsStatusType,
    parse_object_id, user_from_doc, video_from_doc, comment_from_doc,
)
from app.graphql.auth import (
    linkable_video_filter,
    current_user_id, require_user_id, require_admin, visible_video_filter,
)

MAX_PAGE = 50
MAX_SEARCH_LEN = 100


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(value, hi))


async def _video_with_buffer(video) -> VideoType:
    buffered = await metrics_buffer.get_buffered_counts(str(video["_id"]))
    return video_from_doc(video, buffered)


@strawberry.type
class Query:
    @strawberry.field
    async def me(self, info) -> Optional[UserType]:
        """Get current authenticated user"""
        user_id = current_user_id(info)
        if not user_id:
            return None

        db = get_database()
        user = await db.users.find_one({"_id": parse_object_id(user_id)})
        if not user:
            return None
        return user_from_doc(user, user_id)

    @strawberry.field
    async def user(self, info, user_id: str) -> Optional[UserType]:
        """Get a user by ID"""
        db = get_database()
        user = await db.users.find_one({"_id": parse_object_id(user_id), "is_active": True})
        if not user:
            return None
        return user_from_doc(user, current_user_id(info))

    @strawberry.field
    async def users(
        self,
        info,
        user_type: Optional[UserTypeEnum] = None,
        limit: int = 20,
        offset: int = 0
    ) -> List[UserType]:
        """Get list of users"""
        viewer = require_user_id(info)
        db = get_database()

        query = {"is_active": True}
        if user_type:
            query["user_type"] = user_type.value

        limit = _clamp(limit, 1, MAX_PAGE)
        offset = max(offset, 0)
        cursor = db.users.find(query).skip(offset).limit(limit)
        return [user_from_doc(u, viewer) async for u in cursor]

    @strawberry.field
    async def video(self, info, video_id: str) -> Optional[VideoType]:
        """Get a video by ID"""
        db = get_database()
        query = linkable_video_filter(current_user_id(info))
        query["_id"] = parse_object_id(video_id)
        video = await db.videos.find_one(query)
        if not video:
            return None

        # Increment view count in buffer
        await metrics_buffer.increment_view(video_id)
        return await _video_with_buffer(video)

    @strawberry.field
    async def videos(
        self,
        info,
        video_type: Optional[VideoTypeEnum] = None,
        privacy: Optional[VideoPrivacyEnum] = None,
        creator_id: Optional[str] = None,
        hashtag: Optional[str] = None,
        first: int = 20,
        after: Optional[str] = None
    ) -> VideoConnection:
        """Get paginated list of videos (newest first)"""
        db = get_database()

        query = visible_video_filter(current_user_id(info))
        if video_type:
            query["video_type"] = video_type.value
        if privacy:
            query["privacy"] = privacy.value
        if creator_id:
            query["creator_id"] = parse_object_id(creator_id)
        if hashtag:
            query["hashtags"] = hashtag

        count_query = dict(query)

        # Cursor-based pagination: sort is _id descending, so "after" means older.
        if after:
            query["_id"] = {"$lt": parse_object_id(after)}

        first = _clamp(first, 1, MAX_PAGE)
        cursor = db.videos.find(query).sort("_id", -1).limit(first + 1)
        docs = [v async for v in cursor]

        has_next_page = len(docs) > first
        docs = docs[:first]
        videos = [await _video_with_buffer(v) for v in docs]

        total_count = await db.videos.count_documents(count_query)

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
        user_id = require_user_id(info)
        db = get_database()

        user = await db.users.find_one({"_id": parse_object_id(user_id)})
        if not user:
            raise Exception("User not found")
        loc = user.get("localization") if isinstance(user.get("localization"), dict) else {}
        user_country = loc.get("country") or "NG"

        limit = _clamp(limit, 1, MAX_PAGE)
        offset = max(offset, 0)
        # Really trending now (app/services/trending.py), the user's country first.
        top = await trending.top(db, country=user_country, limit=offset + limit)
        page = top[offset:offset + limit]
        full = {d["_id"]: d async for d in db.videos.find({"_id": {"$in": [t["_id"] for t in page]}})}
        best = max((t.get("trending_score") or 0 for t in top), default=0) or 1

        feed_items = []
        position = offset
        for t in page:
            video = full.get(t["_id"])
            if not video:
                continue
            local = (video.get("country") or "").upper() == (user_country or "").upper()
            feed_items.append(FeedItemType(
                video=await _video_with_buffer(video),
                score=round((t.get("trending_score") or 0) / best, 4),
                reason="Trending in your country" if local else "Trending on Wano",
                is_ad=False,
                position=position
            ))
            position += 1

        return feed_items

    @strawberry.field
    async def video_comments(
        self,
        info,
        video_id: str,
        limit: int = 20,
        offset: int = 0
    ) -> List[CommentType]:
        """Get top-level comments for a video"""
        db = get_database()
        viewer = current_user_id(info)
        vid = parse_object_id(video_id)

        vq = visible_video_filter(viewer)
        vq["_id"] = vid
        if not await db.videos.find_one(vq, {"_id": 1}):
            return []

        limit = _clamp(limit, 1, MAX_PAGE)
        offset = max(offset, 0)
        cursor = db.comments.find({
            "video_id": vid,
            "parent_id": None,
            "is_active": True,
        }).sort("created_at", -1).skip(offset).limit(limit)

        return [comment_from_doc(c, viewer) async for c in cursor]

    @strawberry.field
    async def search_videos(
        self,
        info,
        query: str,
        limit: int = 20,
        offset: int = 0
    ) -> List[VideoType]:
        """Search public videos by title, description, or hashtags"""
        db = get_database()

        term = (query or "").strip()[:MAX_SEARCH_LEN]
        if not term:
            return []
        pattern = re.escape(term)

        from app.services import search
        limit = _clamp(limit, 1, MAX_PAGE)
        offset = max(offset, 0)
        docs = await search.find_videos(db, term, offset, limit)
        return [await _video_with_buffer(v) for v in docs]

    @strawberry.field
    async def metrics_status(self, info) -> MetricsStatusType:
        """Get current metrics buffer status (admin only)"""
        await require_admin(info)

        total_videos = len(set(
            list(metrics_buffer.views_buffer.keys()) +
            list(metrics_buffer.likes_buffer.keys()) +
            list(metrics_buffer.comments_buffer.keys())
        ))

        return MetricsStatusType(
            total_videos_in_buffer=total_videos,
            total_updates_pending=metrics_buffer.total_updates,
            last_flush=metrics_buffer.last_flush.isoformat(),
            views_pending=sum(metrics_buffer.views_buffer.values()),
            likes_pending=sum(metrics_buffer.likes_buffer.values()),
            comments_pending=sum(metrics_buffer.comments_buffer.values())
        )
