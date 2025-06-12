"""
GraphQL mutations

app/graphql/mutations.py

"""
import strawberry
from typing import Optional, List
from bson import ObjectId
from datetime import datetime, timedelta
from app.core.database import get_database
from app.services.metrics_service import metrics_buffer
from app.graphql.types import (
    UserType, VideoType, CommentType, AuthPayload,
    UserTypeEnum, VideoTypeEnum, VideoPrivacyEnum,
    LocalizationType
)
from app.core.security import verify_password, get_password_hash, create_access_token
from app.graphql.mutations_folder.video_editor import VideoEditorMutation
from strawberry.types import Info



@strawberry.input
class RegisterInput:
    username: str
    email: str
    password: str
    display_name: str
    country: str
    languages: List[str] = strawberry.field(default_factory=lambda: ["en"])
    user_type: UserTypeEnum = UserTypeEnum.STANDARD
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    tags: Optional[List[str]] = None

@strawberry.input
class LoginInput:
    username: str
    password: str

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
    
    # Add these new fields:
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

@strawberry.type
class CommentMutations:
    @strawberry.mutation
    async def create_comment(
        self, 
        input: CreateCommentInput,
        info: Info
    ) -> CommentType:
        """Create a new comment"""
        db = get_database()
        user_id = info.context["user_id"]
        
        # Get user's display name
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            raise Exception("User not found")
        
        # Verify video exists
        video = await db.videos.find_one({"_id": ObjectId(input.video_id)})
        if not video:
            raise Exception("Video not found")
        
        # If it's a reply, verify parent comment exists
        if input.parent_id:
            parent = await db.comments.find_one({"_id": ObjectId(input.parent_id)})
            if not parent:
                raise Exception("Parent comment not found")
        
        # Create comment with cached display name
        comment_doc = {
            "video_id": ObjectId(input.video_id),
            "user_id": ObjectId(user_id),
            "user_display_name": user["display_name"],  # Cache display name
            "content": input.content,
            "parent_id": ObjectId(input.parent_id) if input.parent_id else None,
            "likes_count": 0,
            "replies_count": 0,
            "liked_by": [],
            "is_edited": False,
            "edited_at": None,
            "is_pinned": False,
            "is_hearted": False,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "is_active": True
        }
        
        result = await db.comments.insert_one(comment_doc)
        
        # Update counts
        if input.parent_id:
            await db.comments.update_one(
                {"_id": ObjectId(input.parent_id)},
                {"$inc": {"replies_count": 1}}
            )
        
        await db.videos.update_one(
            {"_id": ObjectId(input.video_id)},
            {"$inc": {"comments_count": 1}}
        )
        
        return CommentType(
            id=str(result.inserted_id),
            video_id=input.video_id,
            user_id=str(user_id),
            user_display_name=user["display_name"],
            content=input.content,
            parent_id=input.parent_id,
            created_at=comment_doc["created_at"]
        )
    
    @strawberry.mutation
    async def update_comment(
        self,
        comment_id: str,
        content: str,
        info: Info
    ) -> CommentType:
        """Update a comment"""
        db = get_database()
        user_id = info.context["user_id"]
        
        # Get comment
        comment = await db.comments.find_one({
            "_id": ObjectId(comment_id),
            "user_id": ObjectId(user_id),
            "is_active": True
        })
        
        if not comment:
            raise Exception("Comment not found or you don't have permission to edit it")
        
        # Update comment
        await db.comments.update_one(
            {"_id": ObjectId(comment_id)},
            {
                "$set": {
                    "content": content,
                    "is_edited": True,
                    "edited_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow()
                }
            }
        )
        
        # Return updated comment
        comment = await db.comments.find_one({"_id": ObjectId(comment_id)})
        
        return CommentType(
            id=str(comment["_id"]),
            video_id=str(comment["video_id"]),
            user_id=str(comment["user_id"]),
            user_display_name=comment["user_display_name"],
            content=comment["content"],
            parent_id=str(comment["parent_id"]) if comment.get("parent_id") else None,
            likes_count=comment.get("likes_count", 0),
            replies_count=comment.get("replies_count", 0),
            is_edited=True,
            edited_at=comment["edited_at"],
            created_at=comment["created_at"]
        )


@strawberry.type
class Mutation(VideoEditorMutation, CommentMutations):
    @strawberry.mutation
    async def register(self, input: RegisterInput) -> AuthPayload:
        """Register a new user"""
        db = get_database()
        
        # Check if username or email already exists
        existing_user = await db.users.find_one({
            "$or": [
                {"username": input.username},
                {"email": input.email}
            ]
        })
        
        if existing_user:
            raise Exception("Username or email already registered")
        
        # Create user document
        user_doc = {
            "username": input.username,
            "email": input.email,
            "password_hash": f"hashed_{input.password}",  # Use get_password_hash() in production
            "display_name": input.display_name,
            "user_type": input.user_type.value,
            "localization": {
                "country": input.country,
                "languages": input.languages,
                "tribes": []
            },
            "gender": input.gender,
            "date_of_birth": input.date_of_birth,
            "tags": input.tags if input.tags else [],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "is_active": True,
            "followers_count": 0,
            "following_count": 0,
            "videos_count": 0,
            "likes_count": 0,
            "bookmarked_videos": [],
            "liked_videos": [],
            "following": [],
            "followers": []
        }
        
        result = await db.users.insert_one(user_doc)
        user_doc["_id"] = result.inserted_id
        
        # Create access token
        access_token = create_access_token(data={"sub": str(result.inserted_id)})
        
        user = UserType(
            id=str(user_doc["_id"]),
            username=user_doc["username"],
            email=user_doc["email"],
            display_name=user_doc["display_name"],
            user_type=UserTypeEnum(user_doc["user_type"]),
            localization=LocalizationType(**user_doc["localization"]),
            gender=user_doc.get("gender"),
            date_of_birth=user_doc.get("date_of_birth"),
            tags=user_doc.get("tags", []),
            followers_count=0,
            following_count=0,
            videos_count=0,
            created_at=user_doc["created_at"]
        )
        
        return AuthPayload(token=access_token, user=user)
    
    @strawberry.mutation
    async def login(self, input: LoginInput) -> AuthPayload:
        """Login user"""
        db = get_database()
        
        user_doc = await db.users.find_one({"username": input.username})
        
        if not user_doc:
            raise Exception("Invalid username or password")
        
        # Check password
        password_hash = user_doc.get("password_hash", "")
        if password_hash.startswith("hashed_"):
            # MVP simple hash
            if f"hashed_{input.password}" != password_hash:
                raise Exception("Invalid username or password")
        else:
            # Real hash
            if not verify_password(input.password, password_hash):
                raise Exception("Invalid username or password")
        
        # Create access token
        access_token = create_access_token(data={"sub": str(user_doc["_id"])})
        
        user = UserType(
            id=str(user_doc["_id"]),
            username=user_doc["username"],
            email=user_doc["email"],
            display_name=user_doc["display_name"],
            user_type=UserTypeEnum(user_doc["user_type"]),
            localization=LocalizationType(**user_doc["localization"]),
            followers_count=user_doc.get("followers_count", 0),
            following_count=user_doc.get("following_count", 0),
            videos_count=user_doc.get("videos_count", 0),
            created_at=user_doc["created_at"]
        )
        
        return AuthPayload(token=access_token, user=user)
    
    @strawberry.mutation
    async def update_user(self, info, input: UpdateUserInput) -> UserType:
        """Update user profile"""
        user_id = info.context.get("user_id")
        if not user_id:
            raise Exception("Authentication required")
            
        db = get_database()
        
        update_data = {k: v for k, v in input.__dict__.items() if v is not None}
        update_data["updated_at"] = datetime.utcnow()
        
        await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_data}
        )
        
        user_doc = await db.users.find_one({"_id": ObjectId(user_id)})
        
        return UserType(
            id=str(user_doc["_id"]),
            username=user_doc["username"],
            email=user_doc["email"],
            display_name=user_doc["display_name"],
            bio=user_doc.get("bio"),
            user_type=UserTypeEnum(user_doc["user_type"]),
            localization=LocalizationType(**user_doc["localization"]),
            followers_count=user_doc.get("followers_count", 0),
            following_count=user_doc.get("following_count", 0),
            videos_count=user_doc.get("videos_count", 0),
            created_at=user_doc["created_at"]
        )
    
    # Modify create_video mutation
    @strawberry.mutation
    async def create_video(self, info, input: CreateVideoInput) -> VideoType:
        """Create a new video"""
        user_id = info.context.get("user_id")
        if not user_id:
            raise Exception("Authentication required")
            
        db = get_database()
        
        # Get user info and check verification
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        
        if not user.get("is_verified", False):
            raise Exception("Please verify your email before uploading videos")
        
        # Validate video type against duration
        if input.video_type == VideoTypeEnum.BITS and input.duration > 15:
            raise Exception("Bits videos must be 15 seconds or less")
        elif input.video_type == VideoTypeEnum.REGULAR and (input.duration < 15 or input.duration > 90):
            raise Exception("Regular videos must be between 15 and 90 seconds")
        
        video_doc = {
            "creator_id": ObjectId(user_id),
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
            "country": user.get("localization", {}).get("country", "NG"),
            "language": user.get("localization", {}).get("languages", ["en"])[0],
            
            # Use actual URLs from input
            "urls": {
                "original": input.video_url,
                "hls_playlist": input.video_url,  # In production, generate HLS version
                "thumbnail": input.thumbnail_url or input.video_url,  # Use video URL as fallback
                "download": input.video_url
            },
            
            # Use actual metadata from input
            "metadata": {
                "duration": input.duration,
                "width": input.width,
                "height": input.height,
                "fps": input.fps,
                "file_size": input.file_size
            },
            
            # Add the new fields for video editor compatibility
            "FEid": None,
            "start": 0.0,
            "end": input.duration,
            "remoteUrl": input.video_url
        }
        
        result = await db.videos.insert_one(video_doc)
        
        # Update user's video count
        await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$inc": {"videos_count": 1}}
        )
        
        video_doc["_id"] = result.inserted_id
        
        return VideoType(
            id=str(video_doc["_id"]),
            creator_id=str(video_doc["creator_id"]),
            title=video_doc.get("title"),
            description=video_doc.get("description"),
            video_type=VideoTypeEnum(video_doc["video_type"]),
            privacy=VideoPrivacyEnum(video_doc["privacy"]),
            views_count=0,
            likes_count=0,
            comments_count=0,
            shares_count=0,
            hashtags=video_doc.get("hashtags", []),
            categories=video_doc.get("categories", []),
            created_at=video_doc["created_at"],
            buffered_views=0,
            buffered_likes=0,
            buffered_comments=0,
            FEid=video_doc.get("FEid"),
            start=video_doc.get("start", 0.0),
            end=video_doc.get("end", input.duration),
            remoteUrl=video_doc.get("remoteUrl"),
            type=video_doc.get('type')
        )
        
    @strawberry.mutation
    async def update_video(self, info, video_id: str, input: UpdateVideoInput) -> VideoType:
        """Update a video"""
        user_id = info.context.get("user_id")
        if not user_id:
            raise Exception("Authentication required")
            
        db = get_database()
        
        # Verify ownership
        video = await db.videos.find_one({"_id": ObjectId(video_id)})
        if not video:
            raise Exception("Video not found")
            
        if str(video["creator_id"]) != user_id:
            raise Exception("Can only update your own videos")
        
        update_data = {}
        if input.title is not None:
            update_data["title"] = input.title
        if input.description is not None:
            update_data["description"] = input.description
        if input.privacy is not None:
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
        
        await db.videos.update_one(
            {"_id": ObjectId(video_id)},
            {"$set": update_data}
        )
        
        video = await db.videos.find_one({"_id": ObjectId(video_id)})
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
    
    @strawberry.mutation
    async def delete_video(self, info, video_id: str) -> bool:
        """Delete a video (soft delete)"""
        user_id = info.context.get("user_id")
        if not user_id:
            raise Exception("Authentication required")
            
        db = get_database()
        
        # Verify ownership
        video = await db.videos.find_one({"_id": ObjectId(video_id)})
        if not video:
            raise Exception("Video not found")
            
        if str(video["creator_id"]) != user_id:
            raise Exception("Can only delete your own videos")
        
        await db.videos.update_one(
            {"_id": ObjectId(video_id)},
            {"$set": {"is_active": False, "updated_at": datetime.utcnow()}}
        )
        
        # Update user's video count
        await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$inc": {"videos_count": -1}}
        )
        
        return True
    
    @strawberry.mutation
    async def like_video(self, info, video_id: str) -> bool:
        """Like a video"""
        user_id = info.context.get("user_id")
        if not user_id:
            raise Exception("Authentication required")
            
        db = get_database()
        
        # Check if already liked
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        if ObjectId(video_id) in user.get("liked_videos", []):
            raise Exception("Video already liked")
        
        # Add to user's liked videos
        await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$addToSet": {"liked_videos": ObjectId(video_id)}}
        )
        
        # Increment like count in buffer
        await metrics_buffer.increment_like(video_id)
        
        return True
    
    @strawberry.mutation
    async def unlike_video(self, info, video_id: str) -> bool:
        """Unlike a video"""
        user_id = info.context.get("user_id")
        if not user_id:
            raise Exception("Authentication required")
            
        db = get_database()
        
        # Remove from user's liked videos
        await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$pull": {"liked_videos": ObjectId(video_id)}}
        )
        
        # Decrement like count in buffer
        await metrics_buffer.decrement_like(video_id)
        
        return True
    
    @strawberry.mutation
    async def follow_user(self, info, user_id_to_follow: str) -> bool:
        """Follow a user"""
        user_id = info.context.get("user_id")
        if not user_id:
            raise Exception("Authentication required")
            
        if user_id == user_id_to_follow:
            raise Exception("Cannot follow yourself")
            
        db = get_database()
        
        # Add to following list
        await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {
                "$addToSet": {"following": ObjectId(user_id_to_follow)},
                "$inc": {"following_count": 1}
            }
        )
        
        # Add to followers list
        await db.users.update_one(
            {"_id": ObjectId(user_id_to_follow)},
            {
                "$addToSet": {"followers": ObjectId(user_id)},
                "$inc": {"followers_count": 1}
            }
        )
        
        return True
    
    @strawberry.mutation
    async def unfollow_user(self, info, user_id_to_unfollow: str) -> bool:
        """Unfollow a user"""
        user_id = info.context.get("user_id")
        if not user_id:
            raise Exception("Authentication required")
            
        db = get_database()
        
        # Remove from following list
        await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {
                "$pull": {"following": ObjectId(user_id_to_unfollow)},
                "$inc": {"following_count": -1}
            }
        )
        
        # Remove from followers list
        await db.users.update_one(
            {"_id": ObjectId(user_id_to_unfollow)},
            {
                "$pull": {"followers": ObjectId(user_id)},
                "$inc": {"followers_count": -1}
            }
        )
        
        return True
    
    @strawberry.mutation
    async def create_comment(self, info, video_id: str, input: CreateCommentInput) -> CommentType:
        """Create a comment on a video"""
        user_id = info.context.get("user_id")
        if not user_id:
            raise Exception("Authentication required")
            
        db = get_database()
        
        # Verify video exists
        video = await db.videos.find_one({"_id": ObjectId(video_id)})
        if not video:
            raise Exception("Video not found")
        
        if not video.get("comments_enabled", True):
            raise Exception("Comments are disabled for this video")
        
        comment_doc = {
            "video_id": ObjectId(video_id),
            "user_id": ObjectId(user_id),
            "content": input.content,
            "likes_count": 0,
            "replies_count": 0,
            "is_reply": bool(input.parent_comment_id),
            "parent_comment_id": ObjectId(input.parent_comment_id) if input.parent_comment_id else None,
            "is_flagged": False,
            "is_hidden": False,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "is_active": True
        }
        
        result = await db.comments.insert_one(comment_doc)
        
        # Increment comment count in buffer
        await metrics_buffer.increment_comment(video_id)
        
        # If it's a reply, update parent comment's reply count
        if input.parent_comment_id:
            await db.comments.update_one(
                {"_id": ObjectId(input.parent_comment_id)},
                {"$inc": {"replies_count": 1}}
            )
        
        comment_doc["_id"] = result.inserted_id
        
        return CommentType(
            id=str(comment_doc["_id"]),
            video_id=str(comment_doc["video_id"]),
            user_id=str(comment_doc["user_id"]),
            content=comment_doc["content"],
            likes_count=0,
            replies_count=0,
            is_reply=comment_doc["is_reply"],
            parent_comment_id=str(comment_doc["parent_comment_id"]) if comment_doc["parent_comment_id"] else None,
            created_at=comment_doc["created_at"]
        )
    
    @strawberry.mutation
    async def delete_comment(self, info, comment_id: str) -> bool:
        """Delete a comment (soft delete)"""
        user_id = info.context.get("user_id")
        if not user_id:
            raise Exception("Authentication required")
            
        db = get_database()
        
        comment = await db.comments.find_one({"_id": ObjectId(comment_id)})
        if not comment:
            raise Exception("Comment not found")
        
        if str(comment["user_id"]) != user_id:
            raise Exception("Can only delete your own comments")
        
        await db.comments.update_one(
            {"_id": ObjectId(comment_id)},
            {"$set": {"is_active": False, "updated_at": datetime.utcnow()}}
        )
        
        # Decrement video comment count directly
        await db.videos.update_one(
            {"_id": comment["video_id"]},
            {"$inc": {"comments_count": -1}}
        )
        
        return True
    
    @strawberry.mutation
    async def bookmark_video(self, info, video_id: str) -> bool:
        """Bookmark a video"""
        user_id = info.context.get("user_id")
        if not user_id:
            raise Exception("Authentication required")
            
        db = get_database()
        
        # Add to user's bookmarked videos
        await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$addToSet": {"bookmarked_videos": ObjectId(video_id)}}
        )
        
        # Increment video's bookmark count
        await db.videos.update_one(
            {"_id": ObjectId(video_id)},
            {"$inc": {"bookmarks_count": 1}}
        )
        
        return True
    
    @strawberry.mutation
    async def unbookmark_video(self, info, video_id: str) -> bool:
        """Remove bookmark from a video"""
        user_id = info.context.get("user_id")
        if not user_id:
            raise Exception("Authentication required")
            
        db = get_database()
        
        # Remove from user's bookmarked videos
        await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$pull": {"bookmarked_videos": ObjectId(video_id)}}
        )
        
        # Decrement video's bookmark count
        await db.videos.update_one(
            {"_id": ObjectId(video_id)},
            {"$inc": {"bookmarks_count": -1}}
        )
        
        return True
    
    @strawberry.mutation
    async def flush_metrics(self, info) -> bool:
        """Manually flush metrics buffer (admin only)"""
        user_id = info.context.get("user_id")
        if not user_id:
            raise Exception("Authentication required")
            
        # In production, check if user is admin
        await metrics_buffer.flush_all()
        return True