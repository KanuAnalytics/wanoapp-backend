"""
GraphQL mutations

app/graphql/mutations.py

"""
import strawberry
from typing import Optional, List
from datetime import datetime
from app.core.database import get_database
from app.services.metrics_service import metrics_buffer
from app.graphql.types import (
    UserType, VideoType, CommentType,
    UserTypeEnum, VideoTypeEnum, VideoPrivacyEnum,
    LocalizationType,
    parse_object_id, user_from_doc, video_from_doc, comment_from_doc,
)
from app.graphql.auth import require_user_id, require_user_doc, require_admin
from app.graphql.mutations_folder.video_editor import VideoEditorMutation
from strawberry.types import Info

COMMENT_MAX_LEN = 500


@strawberry.input
class UpdateUserInput:
    display_name: Optional[str] = None
    bio: Optional[str] = None
    profile_picture: Optional[str] = None
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    tags: Optional[List[str]] = None

@strawberry.input
class CreateVideoInput:
    title: Optional[str] = None
    description: Optional[str] = None
    video_type: VideoTypeEnum = VideoTypeEnum.REGULAR
    privacy: VideoPrivacyEnum = VideoPrivacyEnum.PUBLIC
    hashtags: List[str] = strawberry.field(default_factory=list)
    categories: List[str] = strawberry.field(default_factory=list)
    remix_enabled: bool = True
    comments_enabled: bool = True

    video_url: str  # Required - the uploaded video URL
    thumbnail_url: Optional[str] = None  # Optional thumbnail
    duration: float = 0.0  # Video duration in seconds
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    file_size: int = 0

@strawberry.input
class UpdateVideoInput:
    title: Optional[str] = None
    description: Optional[str] = None
    privacy: Optional[VideoPrivacyEnum] = None
    hashtags: Optional[List[str]] = None
    categories: Optional[List[str]] = None
    remix_enabled: Optional[bool] = None
    comments_enabled: Optional[bool] = None

@strawberry.input
class CreateCommentInput:
    video_id: str
    content: str
    parent_id: Optional[str] = None


def _validate_comment_content(content: str) -> str:
    if content is None or not content.strip():
        raise Exception("Comment content is required")
    if len(content) > COMMENT_MAX_LEN:
        raise Exception(f"Comment must be at most {COMMENT_MAX_LEN} characters")
    return content


@strawberry.type
class CommentMutations:
    @strawberry.mutation
    async def create_comment(
        self,
        input: CreateCommentInput,
        info: Info
    ) -> CommentType:
        """Create a new comment or reply (same document shape as REST POST /comments)"""
        user_id = require_user_id(info)
        content = _validate_comment_content(input.content)
        video_oid = parse_object_id(input.video_id)
        parent_oid = parse_object_id(input.parent_id) if input.parent_id else None
        user_oid = parse_object_id(user_id)
        db = get_database()

        video = await db.videos.find_one({"_id": video_oid, "is_active": True})
        if not video or (video.get("privacy") == "private" and video.get("creator_id") != user_oid):
            raise Exception("Video not found")
        if video.get("comments_enabled") is False:
            raise Exception("Comments are disabled for this video")

        user = await db.users.find_one({"_id": user_oid})
        if not user:
            raise Exception("User not found")

        parent_comment = None
        if parent_oid:
            parent_comment = await db.comments.find_one({
                "_id": parent_oid,
                "video_id": video_oid,
                "is_active": True,
            })
            if not parent_comment:
                raise Exception("Parent comment not found")

        now = datetime.utcnow()
        comment_doc = {
            "video_id": video_oid,
            "user_id": user_oid,
            "user_display_name": user.get("display_name", "Anonymous"),
            "content": content,
            "parent_id": parent_oid,
            "likes_count": 0,
            "replies_count": 0,
            "liked_by": [],
            "is_edited": False,
            "edited_at": None,
            "is_pinned": False,
            "is_hearted": False,
            "created_at": now,
            "updated_at": now,
            "is_active": True
        }

        result = await db.comments.insert_one(comment_doc)
        comment_doc["_id"] = result.inserted_id

        if parent_oid:
            await db.comments.update_one({"_id": parent_oid}, {"$inc": {"replies_count": 1}})

        await db.videos.update_one({"_id": video_oid}, {"$inc": {"comments_count": 1}})

        if parent_comment:
            notif_recipient = parent_comment.get("user_id")
            notif_type = "comment_reply"
        else:
            notif_recipient = video.get("creator_id")
            notif_type = "comment"
        if notif_recipient and str(notif_recipient) != str(user_id):
            await db.notifications.insert_one({
                "recipient_id": parse_object_id(notif_recipient),
                "type": notif_type,
                "user_id": user_oid,
                "post_id": video_oid,
                "comment_id": result.inserted_id,
                "date": now,
            })

        return comment_from_doc(comment_doc, user_id)

    @strawberry.mutation
    async def update_comment(
        self,
        comment_id: str,
        content: str,
        info: Info
    ) -> CommentType:
        """Update a comment"""
        user_id = require_user_id(info)
        content = _validate_comment_content(content)
        cid = parse_object_id(comment_id)
        db = get_database()

        result = await db.comments.update_one(
            {"_id": cid, "user_id": parse_object_id(user_id), "is_active": True},
            {
                "$set": {
                    "content": content,
                    "is_edited": True,
                    "edited_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow()
                }
            }
        )
        if result.matched_count == 0:
            raise Exception("Comment not found or you don't have permission to edit it")

        comment = await db.comments.find_one({"_id": cid})
        return comment_from_doc(comment, user_id)


@strawberry.type
class Mutation(VideoEditorMutation, CommentMutations):
    # There are deliberately no GraphQL register or login mutations. Signup
    # and login go through the REST endpoints only (POST /api/v1/auth/register
    # and /api/v1/auth/login/json), which hash passwords, block disposable
    # emails and always create standard accounts. The old GraphQL versions
    # stored and accepted plain-text passwords, let the caller pick user_type
    # (including admin), and were never used by the app.

    @strawberry.mutation
    async def update_user(self, info, input: UpdateUserInput) -> UserType:
        """Update user profile"""
        user_id = require_user_id(info)
        uid = parse_object_id(user_id)
        db = get_database()

        allowed = ("display_name", "bio", "profile_picture", "gender", "date_of_birth", "tags")
        update_data = {k: getattr(input, k) for k in allowed if getattr(input, k) is not None}
        update_data["updated_at"] = datetime.utcnow()

        await db.users.update_one({"_id": uid}, {"$set": update_data})

        user_doc = await db.users.find_one({"_id": uid})
        if not user_doc:
            raise Exception("User not found")
        return user_from_doc(user_doc, user_id)

    @strawberry.mutation
    async def create_video(self, info, input: CreateVideoInput) -> VideoType:
        """Create a new video"""
        user = await require_user_doc(info)
        user_oid = user["_id"]
        db = get_database()

        # Validate video type against duration
        if input.video_type == VideoTypeEnum.BITS and input.duration > 15:
            raise Exception("Bits videos must be 15 seconds or less")
        elif input.video_type == VideoTypeEnum.REGULAR and (input.duration < 15 or input.duration > 120):
            raise Exception("Regular videos must be between 15 and 90 seconds")

        loc = user.get("localization") if isinstance(user.get("localization"), dict) else {}
        languages = loc.get("languages") or ["en"]

        video_doc = {
            "creator_id": user_oid,
            "title": input.title,
            "description": input.description,
            "video_type": input.video_type.value,
            "privacy": input.privacy.value,
            "hashtags": input.hashtags,
            "categories": input.categories,
            "remix_enabled": input.remix_enabled,
            "comments_enabled": input.comments_enabled,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "is_active": True,
            "views_count": 0,
            "likes_count": 0,
            "comments_count": 0,
            "shares_count": 0,
            "bookmarks_count": 0,
            "is_approved": True,
            "is_flagged": False,
            "report_count": 0,
            "is_remix": False,
            "remix_count": 0,
            "country": loc.get("country") or "NG",
            "language": languages[0],

            "urls": {
                "original": input.video_url,
                "hls_playlist": input.video_url,  # In production, generate HLS version
                "thumbnail": input.thumbnail_url or input.video_url,
                "download": input.video_url
            },

            "metadata": {
                "duration": input.duration,
                "width": input.width,
                "height": input.height,
                "fps": input.fps,
                "file_size": input.file_size
            },

            # Video editor compatibility
            "FEid": None,
            "start": 0.0,
            "end": input.duration,
            "remoteUrl": input.video_url
        }

        result = await db.videos.insert_one(video_doc)
        await db.users.update_one({"_id": user_oid}, {"$inc": {"videos_count": 1}})

        video_doc["_id"] = result.inserted_id
        return video_from_doc(video_doc)

    @strawberry.mutation
    async def update_video(self, info, video_id: str, input: UpdateVideoInput) -> VideoType:
        """Update a video"""
        user_id = require_user_id(info)
        vid = parse_object_id(video_id)
        db = get_database()

        video = await db.videos.find_one({"_id": vid, "is_active": True})
        if not video:
            raise Exception("Video not found")
        if str(video.get("creator_id")) != user_id:
            raise Exception("Can only update your own videos")

        update_data = {}
        if input.title is not None:
            update_data["title"] = input.title
        if input.description is not None:
            update_data["description"] = input.description
        if input.privacy is not None:
            from app.services import moderation
            if moderation.is_held(video):
                raise Exception("This post is under review; its privacy can't change until the review is done")
            update_data["privacy"] = input.privacy.value
        if input.hashtags is not None:
            update_data["hashtags"] = input.hashtags
        if input.categories is not None:
            update_data["categories"] = input.categories
        if input.remix_enabled is not None:
            update_data["remix_enabled"] = input.remix_enabled
        if input.comments_enabled is not None:
            update_data["comments_enabled"] = input.comments_enabled
        update_data["updated_at"] = datetime.utcnow()

        await db.videos.update_one({"_id": vid}, {"$set": update_data})

        video = await db.videos.find_one({"_id": vid})
        buffered = await metrics_buffer.get_buffered_counts(str(vid))
        return video_from_doc(video, buffered)

    @strawberry.mutation
    async def delete_video(self, info, video_id: str) -> bool:
        """Delete a video (soft delete)"""
        user_id = require_user_id(info)
        vid = parse_object_id(video_id)
        uid = parse_object_id(user_id)
        db = get_database()

        video = await db.videos.find_one({"_id": vid, "is_active": True})
        if not video:
            raise Exception("Video not found")
        if str(video.get("creator_id")) != user_id:
            raise Exception("Can only delete your own videos")

        result = await db.videos.update_one(
            {"_id": vid, "creator_id": uid, "is_active": True},
            {"$set": {"is_active": False, "updated_at": datetime.utcnow()}}
        )
        if result.modified_count:
            await db.users.update_one({"_id": uid}, {"$inc": {"videos_count": -1}})

        return True

    @strawberry.mutation
    async def like_video(self, info, video_id: str) -> bool:
        """Like a video"""
        user_id = require_user_id(info)
        vid = parse_object_id(video_id)
        db = get_database()

        if not await db.videos.find_one({"_id": vid, "is_active": True}, {"_id": 1}):
            raise Exception("Video not found")

        result = await db.users.update_one(
            {"_id": parse_object_id(user_id), "liked_videos": {"$ne": vid}},
            {"$addToSet": {"liked_videos": vid}}
        )
        if not result.modified_count:
            raise Exception("Video already liked")

        await metrics_buffer.increment_like(str(vid))
        return True

    @strawberry.mutation
    async def unlike_video(self, info, video_id: str) -> bool:
        """Unlike a video"""
        user_id = require_user_id(info)
        vid = parse_object_id(video_id)
        db = get_database()

        result = await db.users.update_one(
            {"_id": parse_object_id(user_id), "liked_videos": vid},
            {"$pull": {"liked_videos": vid}}
        )
        if result.modified_count:
            await metrics_buffer.decrement_like(str(vid))
        return True

    @strawberry.mutation
    async def follow_user(self, info, user_id_to_follow: str) -> bool:
        """Follow a user"""
        user_id = require_user_id(info)
        me = parse_object_id(user_id)
        target = parse_object_id(user_id_to_follow)
        if me == target:
            raise Exception("Cannot follow yourself")

        db = get_database()
        if not await db.users.find_one({"_id": target, "is_active": True}, {"_id": 1}):
            raise Exception("User not found")

        result = await db.users.update_one(
            {"_id": me, "following": {"$ne": target}},
            {"$addToSet": {"following": target}, "$inc": {"following_count": 1}}
        )
        if result.modified_count:
            await db.users.update_one(
                {"_id": target, "followers": {"$ne": me}},
                {"$addToSet": {"followers": me}, "$inc": {"followers_count": 1}}
            )
        return True

    @strawberry.mutation
    async def unfollow_user(self, info, user_id_to_unfollow: str) -> bool:
        """Unfollow a user"""
        user_id = require_user_id(info)
        me = parse_object_id(user_id)
        target = parse_object_id(user_id_to_unfollow)
        db = get_database()

        result = await db.users.update_one(
            {"_id": me, "following": target},
            {"$pull": {"following": target}, "$inc": {"following_count": -1}}
        )
        if result.modified_count:
            await db.users.update_one(
                {"_id": target, "followers": me},
                {"$pull": {"followers": me}, "$inc": {"followers_count": -1}}
            )
        return True

    @strawberry.mutation
    async def delete_comment(self, info, comment_id: str) -> bool:
        """Delete a comment (soft delete)"""
        user_id = require_user_id(info)
        cid = parse_object_id(comment_id)
        db = get_database()

        comment = await db.comments.find_one({"_id": cid, "is_active": True})
        if not comment:
            raise Exception("Comment not found")
        if str(comment.get("user_id")) != user_id:
            raise Exception("Can only delete your own comments")

        result = await db.comments.update_one(
            {"_id": cid, "is_active": True},
            {"$set": {"is_active": False, "updated_at": datetime.utcnow()}}
        )
        if result.modified_count:
            if comment.get("parent_id"):
                await db.comments.update_one(
                    {"_id": comment["parent_id"], "replies_count": {"$gt": 0}},
                    {"$inc": {"replies_count": -1}}
                )
            await db.videos.update_one(
                {"_id": comment["video_id"], "comments_count": {"$gt": 0}},
                {"$inc": {"comments_count": -1}}
            )
        return True

    @strawberry.mutation
    async def bookmark_video(self, info, video_id: str) -> bool:
        """Bookmark a video"""
        user_id = require_user_id(info)
        vid = parse_object_id(video_id)
        db = get_database()

        if not await db.videos.find_one({"_id": vid, "is_active": True}, {"_id": 1}):
            raise Exception("Video not found")

        result = await db.users.update_one(
            {"_id": parse_object_id(user_id), "bookmarked_videos": {"$ne": vid}},
            {"$addToSet": {"bookmarked_videos": vid}}
        )
        if result.modified_count:
            await db.videos.update_one({"_id": vid}, {"$inc": {"bookmarks_count": 1}})
        return True

    @strawberry.mutation
    async def unbookmark_video(self, info, video_id: str) -> bool:
        """Remove bookmark from a video"""
        user_id = require_user_id(info)
        vid = parse_object_id(video_id)
        db = get_database()

        result = await db.users.update_one(
            {"_id": parse_object_id(user_id), "bookmarked_videos": vid},
            {"$pull": {"bookmarked_videos": vid}}
        )
        if result.modified_count:
            await db.videos.update_one(
                {"_id": vid, "bookmarks_count": {"$gt": 0}},
                {"$inc": {"bookmarks_count": -1}}
            )
        return True

    @strawberry.mutation
    async def flush_metrics(self, info) -> bool:
        """Manually flush metrics buffer (admin only)"""
        await require_admin(info)
        await metrics_buffer.flush_all()
        return True
