"""
Video CRUD operations with buffered metrics

app/api/v1/vidoes.py

"""
from typing import List, Literal, Optional
from fastapi import APIRouter, HTTPException, Depends, Header, Request, status, UploadFile, File, Form, Query, BackgroundTasks
from bson import ObjectId
from datetime import datetime, timezone as _utc_tz
from app.models.video import Video, VideoType, VideoPrivacy
from app.core.database import get_database
from app.core.config import settings
from app.api.deps import get_current_active_user, get_optional_active_user, get_verified_user
from app.services.expo import send_push_message
from app.services.metrics_service import metrics_buffer
from pydantic import BaseModel,HttpUrl, Field, model_validator
import re
import json
import logging
from bson.json_util import dumps
from app.models.user import UserType
from recombee_api_client.api_requests import SetViewPortion, AddRating, DeleteRating, AddBookmark, DeleteBookmark, SetItemValues, Batch, DeleteItem
from app.services.recombee_service import recombee_send
from app.services import creators, for_you, moderation, recommendations, search, trending
from app.services.recommendations import anon_id, viewer_id
from app.services.video_deletion import soft_delete_video
from app.services.stitch_jobs import MAX_CLIPS, StitchValidationError, create_stitch_job
from app.services.video_posts import DEFAULT_THUMBNAIL, create_video_post, post_stream_uid, stream_uid_reference_query
from app.services.songs import sound_for_post
from app.services.scheduled_publisher import normalize_schedule


# Fraction of a video (0-1) that counts as "watched" for feed-exclusion
# purposes. Below this, a view is still reported to Recombee (every view is a
# useful ranking signal) but isn't persisted to watch_history.
WATCHED_PORTION_THRESHOLD = 0.25

TITLE_MAX_LENGTH = 100

router = APIRouter()
logger = logging.getLogger(__name__)


def _video_oid(video_id: str) -> ObjectId:
    if not ObjectId.is_valid(video_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid video ID"
        )
    return ObjectId(video_id)


def _client_ip(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    # X-Forwarded-For is "client-claimed, ..., proxy-seen"; anyone can write the
    # left part, so trust only the entries our own proxies appended (rightmost).
    hops = max(0, int(getattr(settings, "TRUSTED_PROXY_HOPS", 1)))
    fwd = [p.strip() for p in (request.headers.get("x-forwarded-for") or "").split(",") if p.strip()]
    if hops and len(fwd) >= hops:
        return fwd[-hops][:64]
    return request.client.host if request.client else None


def _visible_to(video: dict, current_user: Optional[str]) -> bool:
    """Opening a post by its ID (a share link): public and unlisted posts work
    for anyone who has the link; private ones only for the creator. Lists,
    feeds and search still show public posts only."""
    if video.get("privacy", "public") in ("public", "unlisted"):
        return True
    return bool(current_user) and str(video.get("creator_id")) == str(current_user)


async def _active_video_or_404(db, video_oid: ObjectId, projection: Optional[dict] = None) -> dict:
    video = await db.videos.find_one({"_id": video_oid, "is_active": True}, projection)
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found"
        )
    return video


def clean_title(title: Optional[str]) -> Optional[str]:
    """Titles stay optional here because older app versions never send one; the app makes them required."""
    if title is None:
        return None
    title = title.strip()[:TITLE_MAX_LENGTH]
    return title or None

class PostSound(BaseModel):
    id: Optional[str] = None
    title: Optional[str] = None
    uri: Optional[str] = None
    artist: Optional[str] = None

class VideoPost(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    video_type: Optional[VideoType] = VideoType.REGULAR
    privacy: VideoPrivacy = VideoPrivacy.PUBLIC
    media_type: Literal["video", "photo"] = "video"
    remoteUrl: Optional[str] = None
    remoteUrl_CF: Optional[str] = None
    images: Optional[List[str]] = None
    thumbnail: Optional[str] = DEFAULT_THUMBNAIL
    duration: Optional[float] = 0.0
    start: Optional[float] = 0.0
    end: Optional[float] = None
    remix_enabled: Optional[bool] = True
    comments_enabled: bool = True
    categoryId: Optional[str] = None
    subcategoryId: Optional[str] = None
    isReadyToStream: Optional[bool] = False
    width: Optional[int] = None
    height: Optional[int] = None
    supports_landscape: Optional[bool] = None
    # The song played with a photo post (an id from GET /music/songs).
    sound_id: Optional[str] = None
    # When set, the post stays hidden and goes live at this time. The zone is
    # only remembered so the app can show the time the way it was picked.
    scheduled_at: Optional[datetime] = None
    timezone: Optional[str] = None

class StitchClip(BaseModel):
    url: str
    start: float = Field(0.0, ge=0)
    end: Optional[float] = None

    @model_validator(mode="after")
    def end_after_start(self):
        if self.end is not None and self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self

class StitchAudio(BaseModel):
    uri: str
    # The song's id from GET /music/songs, so the post can show its name.
    id: Optional[str] = None

class VideoStitch(BaseModel):
    videoUrls: List[StitchClip] = Field(..., min_length=1, max_length=MAX_CLIPS)
    audio: Optional[StitchAudio] = None
    keepOriginalSound: bool = False
    # Volume multipliers: 1 leaves the level alone, 0.5 halves it, 0 mutes.
    videoVolume: float = Field(1.0, ge=0, le=2)
    musicVolume: float = Field(1.0, ge=0, le=2)
    title: Optional[str] = None
    description: Optional[str] = None
    privacy: VideoPrivacy = VideoPrivacy.PUBLIC
    thumbnail: Optional[str] = None
    comments_enabled: bool = True
    categoryId: Optional[str] = None
    subcategoryId: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    timezone: Optional[str] = None

class VideoCreate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    video_type: VideoType
    privacy: VideoPrivacy = VideoPrivacy.PUBLIC
    hashtags: List[str] = []
    categories: List[str] = []
    remix_enabled: bool = True
    comments_enabled: bool = True

class TrackWatchBody(BaseModel):
    portion: float = Field(..., ge=0, le=1, allow_inf_nan=False)
    time_spent: Optional[float] = Field(None, ge=0, le=86400, allow_inf_nan=False)
    recomm_id: Optional[str] = None
    auto_presented: bool = False

class VideoUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    privacy: Optional[VideoPrivacy] = None
    hashtags: Optional[List[str]] = None
    categories: Optional[List[str]] = None
    remix_enabled: Optional[bool] = None
    comments_enabled: Optional[bool] = None

class VideoResponse(BaseModel):
    id: str = Field(alias="_id")
    creator_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    video_type: VideoType
    privacy: VideoPrivacy
    views_count: int
    likes_count: int
    comments_count: int
    created_at: datetime
    
    buffered_views: int = 0
    buffered_likes: int = 0
    buffered_comments: int = 0

    FEid: Optional[str] = None
    start: float = 0.0
    end: Optional[float] = None
    remoteUrl: Optional[str] = None
    remoteUrl_CF: Optional[str] = None
    media_type: str = "video"
    images: List[str] = []
    sound: Optional[PostSound] = None

    is_following: Optional[bool] = None
    is_liked: Optional[bool] = None
    urls: Optional[dict] = None
    user: Optional[dict] = None
    supports_landscape: bool = False

    class Config:
        populate_by_name = True

    @model_validator(mode="before")
    @classmethod
    def replace_stored_nulls(cls, data):
        # Older documents can hold null where these expect a value.
        if isinstance(data, dict):
            data = dict(data)
            for key, default in (("images", []), ("media_type", "video"), ("supports_landscape", False), ("start", 0.0)):
                if key in data and data[key] is None:
                    data[key] = default
        return data

@router.post("/post", status_code=status.HTTP_201_CREATED)
async def post_video(
    input: VideoPost,
    current_user: str = Depends(get_verified_user)
):
    try: 
        """Endpoint to handle video posting logic"""
        is_photo_post = input.media_type == "photo"

        if is_photo_post:
            if not input.images:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="images is required for photo posts",
                )
        else:
            if not input.remoteUrl or not input.remoteUrl_CF:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="remoteUrl and remoteUrl_CF are required for video posts",
                )

            # remoteUrl_CF comes from the client: refuse a Cloudflare stream that
            # already belongs to someone else's post, or deleting this post
            # (or theirs) would take down the other one's video.
            stream_uid = post_stream_uid(input.remoteUrl_CF, input.remoteUrl)
            if stream_uid:
                db = get_database()
                taken = await db.videos.find_one(
                    {**stream_uid_reference_query(stream_uid), "creator_id": {"$ne": ObjectId(current_user)}},
                    {"_id": 1},
                )
                if taken:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="This video is already used by another post",
                    )

        # Only photo posts play a separate song; a video's song is mixed in by /stitch.
        sound = await sound_for_post(input.sound_id) if is_photo_post and input.sound_id else None
        scheduled_at, scheduled_timezone = normalize_schedule(
            input.scheduled_at, input.timezone, require_lead_time=False
        )

        video_id = await create_video_post(
            current_user,
            media_type=input.media_type,
            images=input.images,
            remote_url=input.remoteUrl,
            remote_url_cf=input.remoteUrl_CF,
            title=clean_title(input.title),
            description=input.description,
            privacy=input.privacy,
            thumbnail=input.thumbnail,
            duration=input.duration,
            end=input.end,
            is_ready_to_stream=input.isReadyToStream,
            width=input.width,
            height=input.height,
            comments_enabled=input.comments_enabled,
            category_id=input.categoryId,
            subcategory_id=input.subcategoryId,
            supports_landscape=input.supports_landscape,
            sound=sound,
            scheduled_at=scheduled_at,
            scheduled_timezone=scheduled_timezone,
        )

        message = "Photo posted successfully" if is_photo_post else "Video posted successfully"
        if scheduled_at:
            message = "Post scheduled successfully"
        return {"message": message, "video_id": video_id}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Posting a video failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create the post"
        )
@router.post("/stitch", status_code=status.HTTP_202_ACCEPTED)
async def stitch_video(
    input: VideoStitch,
    current_user: str = Depends(get_verified_user)
):
    """
    Queue a multi-clip post. The clips are stitched once Cloudflare Stream has
    processed all of them, and the post appears when the result is ready.
    """
    sound = await sound_for_post(input.audio.id, input.audio.uri) if input.audio else None
    scheduled_at, scheduled_timezone = normalize_schedule(
        input.scheduled_at, input.timezone, require_lead_time=False
    )
    try:
        job_id = await _create_stitch_job(input, current_user, sound, scheduled_at, scheduled_timezone)
    except StitchValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except HTTPException:
        raise
    except Exception:
        logger.exception("Creating a stitch job failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not start processing this video"
        )
    return {"message": "Video submitted successfully. Processing in background.", "job_id": job_id}


async def _create_stitch_job(input: VideoStitch, current_user: str, sound, scheduled_at, scheduled_timezone) -> str:
    return await create_stitch_job(
        user_id=current_user,
        clips=[clip.model_dump() for clip in input.videoUrls],
        audio_url=input.audio.uri if input.audio else None,
        keep_original_sound=input.keepOriginalSound,
        video_volume=input.videoVolume,
        music_volume=input.musicVolume,
        post={
            "title": clean_title(input.title),
            "description": input.description,
            "sound": sound,
            "privacy": input.privacy.value,
            "thumbnail": input.thumbnail,
            "categoryId": input.categoryId,
            "subcategoryId": input.subcategoryId,
            "comments_enabled": input.comments_enabled,
            "scheduled_at": scheduled_at,
            "scheduled_timezone": scheduled_timezone,
        },
    )

# Modify create_video function
# Update the create_video endpoint to use get_verified_user
@router.post("/", response_model=VideoResponse, status_code=status.HTTP_201_CREATED)
async def create_video(
    video_data: VideoCreate,
    current_user: str = Depends(get_verified_user)  # This will now work
):
    """Create a new video post - requires verified email"""
    db = get_database()
    
    # Get user info
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    
    video_doc = {
        **video_data.dict(),
        "creator_id": ObjectId(current_user),
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
        # Placeholder URLs - in production, these would come from storage service
        "urls": {
            "original": "https://storage.wano.com/video.mp4",
            "hls_playlist": "https://storage.wano.com/video.m3u8",
            "thumbnail": "https://storage.wano.com/thumb.jpg"
        },
        "metadata": {
            "duration": 30.0,
            "width": 1080,
            "height": 1920,
            "fps": 30.0,
            "file_size": 10000000
        },
        "country": user.get("localization", {}).get("country", "NG"),
        "language": user.get("localization", {}).get("languages", ["en"])[0]
    }
    
    result = await db.videos.insert_one(video_doc)
    
    # Update user's video count
    await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {"$inc": {"videos_count": 1}}
    )
    
    video_doc["_id"] = str(result.inserted_id)
    video_doc["creator_id"] = str(video_doc["creator_id"])
    
    return VideoResponse(**video_doc)

@router.get("/", response_model=List[VideoResponse])
async def get_videos(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    video_type: Optional[VideoType] = None,
    privacy: Optional[VideoPrivacy] = None
):
    """Get all public videos with pagination"""
    db = get_database()

    # This listing spans every creator, so it only ever shows public posts;
    # asking for private/unlisted ones returns nothing rather than leaking them.
    if privacy and privacy != VideoPrivacy.PUBLIC:
        return []
    query = {"is_active": True, "privacy": "public"}
    if video_type:
        query["video_type"] = video_type
    
    cursor = db.videos.find(query).skip(skip).limit(limit).sort("created_at", -1)
    videos = []
    async for video in cursor:
        video["_id"] = str(video["_id"])
        video["creator_id"] = str(video["creator_id"])
        
        # Get buffered counts
        video_id = video["_id"]
        buffered = await metrics_buffer.get_buffered_counts(video_id)
        
        response = VideoResponse(**video)
        response.buffered_views = buffered["views"]
        response.buffered_likes = buffered["likes"]
        response.buffered_comments = buffered["comments"]
        
        videos.append(response)
    
    return videos

# --- Search endpoint ---
@router.get("/search")
async def search_videos(
    q: str = Query(..., description="Search query", max_length=200),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
):
    """
    Search public videos: words in the title, description or hashtags
    (Atlas Search when enabled; see app/services/search.py).
    """
    db = get_database()
    docs = await search.find_videos(db, q, skip, limit)
    ids = list({d.get("creator_id") for d in docs if isinstance(d.get("creator_id"), ObjectId)})
    users = {u["_id"]: u async for u in db.users.find(
        {"_id": {"$in": ids}}, {"username": 1, "display_name": 1, "profile_picture": 1})} if ids else {}
    out = []
    for d in docs:
        row = {"_id": str(d["_id"]), "creator_id": str(d.get("creator_id")) if d.get("creator_id") else None}
        if "description" in d:
            row["description"] = d["description"]
        at = d.get("created_at")
        row["created_at"] = (at.astimezone(_utc_tz.utc) if at.tzinfo else at).strftime("%Y-%m-%dT%H:%M:%S.") + \
            f"{at.microsecond // 1000:03d}Z" if isinstance(at, datetime) else None
        if isinstance(d.get("urls"), dict) and "thumbnail" in d["urls"]:
            row["thumbnail"] = d["urls"]["thumbnail"]
        u = users.get(d.get("creator_id")) or {}
        row["user"] = {k: u[k] for k in ("username", "display_name", "profile_picture") if k in u}
        out.append(row)
    return out


async def _send_milestone_push(db, video: dict, video_id: str, background_tasks: BackgroundTasks, title: str):
    recipient = await db.users.find_one(
        {"_id": video["creator_id"]},
        {"expo_push_tokens": 1},
    )
    recipient_tokens = (recipient or {}).get("expo_push_tokens") or []
    thumbnail_url = (video.get("urls") or {}).get("thumbnail")
    description = (video.get("description") or "").strip()
    description_preview = description[:30] + ("..." if len(description) > 30 else "")
    for token in recipient_tokens:
        background_tasks.add_task(
            send_push_message,
            token,
            description_preview,
            {"video_id": video_id},
            title,
            thumbnail_url,
        )


async def notify_view_milestones(
    db,
    video: dict,
    video_id: str,
    total_views: int,
    background_tasks: Optional[BackgroundTasks],
):
    """Push-notifies a video's creator when its view count crosses a
    milestone. Shared by GET /{video_id} and POST /{video_id}/watch so both
    view-counting paths trigger the same creator notifications."""
    if background_tasks is None:
        return

    if total_views == 25:
        await _send_milestone_push(
            db, video, video_id, background_tasks, "Your post is getting attention 🔥"
        )

    milestone_labels = {50: "50", 100: "100", 1000: "1k", 5000: "5k"}
    if total_views in milestone_labels:
        await _send_milestone_push(
            db,
            video,
            video_id,
            background_tasks,
            f"Your video just hit {milestone_labels[total_views]} views",
        )


@router.get("/{video_id}", response_model=VideoResponse)
async def get_video(
    video_id: str,
    current_user: Optional[str] = Depends(get_optional_active_user),
    background_tasks: BackgroundTasks = None,
    recomm_id: Optional[str] = None,
    portion: Optional[float] = None,
    time_spent: Optional[float] = None,
    auto_presented: bool = False,
    x_device_id: Optional[str] = Header(None, alias="X-Device-Id"),
    request: Request = None,
):
    """Get a specific video by ID, and count the view."""
    db = get_database()

    video_oid = _video_oid(video_id)
    video = await db.videos.find_one({
        "_id": video_oid,
        "is_active": True
    })

    # A private post looks exactly like a missing one to everyone but its creator.
    if not video or not _visible_to(video, current_user):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found"
        )

    # Views are counted here as well as in POST /{video_id}/watch. Builds
    # already in the stores count a view by opening the video and never call
    # /watch, so counting there alone would stop their views dead.
    # count_view_once allows one view per viewer per video every 6 hours, so
    # a build that calls both still counts a single view.
    viewer = viewer_id(current_user, x_device_id)
    if await trending.count_view_once(db, viewer=viewer, video_id=video_id, ip=_client_ip(request)):
        await metrics_buffer.increment_view(video_id)
        await trending.record_watch(
            db, video_id, logged_in=bool(current_user),
            portion=portion if portion is not None else 0.0,
            viewer=current_user, creator=video.get("creator_id"),
        )
        counts = await metrics_buffer.get_buffered_counts(video_id)
        await notify_view_milestones(
            db, video, video_id,
            video.get("views_count", 0) + counts["views"], background_tasks,
        )

    if current_user and portion is not None:
        try:
            kwargs = {"auto_presented": auto_presented, "cascade_create": True}
            if recomm_id:
                kwargs["recomm_id"] = recomm_id
            if time_spent is not None:
                kwargs["time_spent"] = time_spent
            req = SetViewPortion(current_user, video_id, portion, **kwargs)
            req.timeout = 5000
            await recombee_send(req)
        except Exception:
            pass
    
    # Get buffered counts for immediate display
    buffered = await metrics_buffer.get_buffered_counts(video_id)

    is_liked = False
    is_following = False
    creator_doc = await db.users.find_one({"_id": video["creator_id"]}) or {}

    if current_user:
        user_doc = await db.users.find_one({"_id": ObjectId(current_user)}) or {}
        is_liked = ObjectId(video_id) in (user_doc.get("liked_videos") or [])
        current_user_oid = ObjectId(current_user)
        is_following = current_user_oid in (creator_doc.get("followers") or [])

    user_info = {
        "username": creator_doc.get("username"),
        "display_name": creator_doc.get("display_name"),
        "profile_picture": creator_doc.get("profile_picture"),
        "is_active": creator_doc.get("is_active"),
    }

    video["_id"] = str(video["_id"])
    video["creator_id"] = str(video["creator_id"])

    response = VideoResponse(**video)
    response.buffered_views = buffered["views"]
    response.buffered_likes = buffered["likes"]
    response.buffered_comments = buffered["comments"]
    response.is_following = is_following
    response.is_liked = is_liked
    response.user = user_info

    return response

@router.post("/{video_id}/watch", status_code=status.HTTP_200_OK)
async def track_video_watch(
    video_id: str,
    body: TrackWatchBody,
    current_user: Optional[str] = Depends(get_optional_active_user),
    background_tasks: BackgroundTasks = None,
    x_device_id: Optional[str] = Header(None, alias="X-Device-Id"),
    request: Request = None,
):
    """
    Called once per video, when the feed scrolls off it — replaces the old
    tracking call that rode along on GET /{video_id}. Sends the same
    Recombee view-portion signal that call sent, triggers the same
    view-milestone creator notifications, and additionally persists a
    watch_history row once `portion` crosses WATCHED_PORTION_THRESHOLD, for
    the feed to later filter out. The feed-exclusion filtering itself is not
    implemented yet — this endpoint only records the data it will need.
    """
    _video_oid(video_id)

    db = get_database()

    video = await db.videos.find_one(
        {"_id": ObjectId(video_id), "is_active": True},
        {"description": 1, "urls.thumbnail": 1, "creator_id": 1, "views_count": 1, "privacy": 1},
    )
    if not video or not _visible_to(video, current_user):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found",
        )

    viewer = viewer_id(current_user, x_device_id)

    # The only place a view is counted (GET /{video_id} no longer does), and
    # only once per viewer per video every 6 hours, so replaying /watch can't
    # inflate views or push a video into trending.
    counted = await trending.count_view_once(db, viewer=viewer, video_id=video_id, ip=_client_ip(request))
    if counted:
        await metrics_buffer.increment_view(video_id)
        await trending.record_watch(db, video_id, logged_in=bool(current_user), portion=body.portion,
                                    viewer=current_user, creator=video.get("creator_id"))
        await creators.record_view(db, video, viewer=current_user, portion=body.portion)
        buffered = await metrics_buffer.get_buffered_counts(video_id)
        total_views = video.get("views_count", 0) + buffered["views"]
        await notify_view_milestones(db, video, video_id, total_views, background_tasks)

    watched = False

    # Logged-out devices report too, under their own anon-<device> profile.
    if current_user or anon_id(x_device_id):
        try:
            kwargs = {"auto_presented": body.auto_presented, "cascade_create": True}
            if body.recomm_id:
                kwargs["recomm_id"] = body.recomm_id
            if body.time_spent is not None:
                kwargs["time_spent"] = body.time_spent
            req = SetViewPortion(viewer, video_id, body.portion, **kwargs)
            req.timeout = 5000
            await recombee_send(req)
        except Exception:
            pass
    try:
        await for_you.record_watch(db, viewer=viewer, video_id=video_id, portion=body.portion)
    except Exception:
        pass

    if current_user:

        if body.portion >= WATCHED_PORTION_THRESHOLD:
            now = datetime.utcnow()
            await db.watch_history.update_one(
                {
                    "user_id": ObjectId(current_user),
                    "video_id": ObjectId(video_id),
                },
                {
                    "$set": {"portion": body.portion, "watched_at": now},
                    "$setOnInsert": {"first_watched_at": now},
                },
                upsert=True,
            )
            watched = True

    return {"watched": watched}

class FeedbackBody(BaseModel):
    recomm_id: Optional[str] = None


async def _visible_video_or_404(db, video_id: str, current_user: Optional[str]) -> dict:
    video = await db.videos.find_one({"_id": _video_oid(video_id), "is_active": True},
                                     {"creator_id": 1, "privacy": 1})
    if not video or not _visible_to(video, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    return video


@router.post("/{video_id}/share", status_code=status.HTTP_200_OK)
async def share_video(
    video_id: str,
    body: Optional[FeedbackBody] = None,
    current_user: str = Depends(get_current_active_user),
):
    """The app calls this when someone shares a post; a share is one of the
    strongest signals that they liked it."""
    db = get_database()
    video = await _visible_video_or_404(db, video_id, current_user)
    await db.videos.update_one({"_id": ObjectId(video_id)}, {"$inc": {"shares_count": 1}})
    await trending.record_once(db, video_id, actor=current_user, creator=video.get("creator_id"),
                               kind="share", shares=1)
    await recommendations.report_share(current_user, video_id, (body or FeedbackBody()).recomm_id)
    return {"shared": True}


@router.post("/{video_id}/not-interested", status_code=status.HTTP_200_OK)
async def not_interested(
    video_id: str,
    body: Optional[FeedbackBody] = None,
    current_user: str = Depends(get_current_active_user),
):
    """"Not interested": tells Recombee, and the post never comes back in For You."""
    db = get_database()
    await _visible_video_or_404(db, video_id, current_user)
    now = datetime.utcnow()
    await db.watch_history.update_one(
        {"user_id": ObjectId(current_user), "video_id": ObjectId(video_id)},
        {"$set": {"watched_at": now, "not_interested": True}, "$setOnInsert": {"first_watched_at": now}},
        upsert=True,
    )
    await recommendations.report_not_interested(current_user, video_id, (body or FeedbackBody()).recomm_id)
    return {"not_interested": True}


@router.put("/{video_id}", response_model=VideoResponse)
async def update_video(
    video_id: str,
    video_update: VideoUpdate,
    current_user: str = Depends(get_current_active_user)
):
    """Update a video (only creator can update)"""
    db = get_database()

    video_oid = _video_oid(video_id)
    # Verify ownership
    video = await db.videos.find_one({"_id": video_oid})
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found"
        )
    
    if str(video["creator_id"]) != current_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only update your own videos"
        )
    
    update_data = {
        k: v for k, v in video_update.dict(exclude_unset=True).items()
    }
    if "privacy" in update_data and update_data["privacy"] is None:
        del update_data["privacy"]
    if "privacy" in update_data and moderation.is_held(video):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This post is under review; its privacy can't change until the review is done",
        )
    old_privacy = video.get("privacy")
    update_data["updated_at"] = datetime.utcnow()
    # Videos made before stream_uid existed get it on their next edit.
    if not video.get("stream_uid") and video.get("media_type") != "photo":
        uid = post_stream_uid(video.get("remoteUrl_CF"), video.get("remoteUrl"))
        if uid:
            update_data["stream_uid"] = uid

    await db.videos.update_one(
        {"_id": video_oid},
        {"$set": update_data}
    )

    new_privacy = update_data.get("privacy")
    if new_privacy is not None:
        new_privacy = getattr(new_privacy, "value", new_privacy)
    if new_privacy is not None and new_privacy != old_privacy and video.get("is_active") is not False:
        # The v2 feed filters on Recombee's copy of privacy; keep it in step.
        try:
            req = SetItemValues(video_id, {"privacy": new_privacy}, cascade_create=False)
            req.timeout = 5000
            await recombee_send(req)
        except Exception:
            logger.exception("Could not sync privacy of video %s to Recombee", video_id)

    video = await db.videos.find_one({"_id": video_oid})
    video["_id"] = str(video["_id"])
    video["creator_id"] = str(video["creator_id"])
    
    # Get buffered counts
    buffered = await metrics_buffer.get_buffered_counts(video_id)
    
    response = VideoResponse(**video)
    response.buffered_views = buffered["views"]
    response.buffered_likes = buffered["likes"]
    response.buffered_comments = buffered["comments"]
    
    return response

@router.delete("/{video_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_video(
    video_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Soft delete a video. Only creator or admin can delete.

    Deleting an already-deleted video is a no-op. Without that, a repeat
    delete read the placeholder's stream id back off the record and deleted
    the shared placeholder from Cloudflare, and lowered the creator's video
    count a second time.
    """
    db = get_database()

    if not ObjectId.is_valid(video_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid video ID"
        )

    # Get current user document
    user_doc = await db.users.find_one({"_id": ObjectId(current_user)})
    if not user_doc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )

    # Verify video exists
    video = await db.videos.find_one({"_id": ObjectId(video_id)})
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found"
        )

    # Check permissions (creator or admin)
    is_creator = str(video["creator_id"]) == current_user
    is_admin = user_doc.get("user_type") == UserType.ADMIN  # adjust if your field is different

    if not (is_creator or is_admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to delete this video"
        )

    # Already-deleted videos are a no-op inside soft_delete_video. A scheduled
    # post is cancelled there too, so the publisher never makes it live.
    await soft_delete_video(db, video)

    return {"message": "Video deleted successfully"}  # Optional for 204

@router.post("/{video_id}/like", status_code=status.HTTP_200_OK)
async def like_video(
    video_id: str,
    current_user: str = Depends(get_current_active_user),
    background_tasks: BackgroundTasks = None,
    recomm_id: Optional[str] = None,
):
    """Like a video (buffered)"""
    db = get_database()
    video_oid = _video_oid(video_id)

    video = await _active_video_or_404(
        db, video_oid,
        {"description": 1, "urls.thumbnail": 1, "thumbnail": 1, "creator_id": 1, "privacy": 1},
    )
    if not _visible_to(video, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    # Conditional add: only the request that actually adds the like counts it,
    # so repeated or concurrent likes can't inflate the count.
    result = await db.users.update_one(
        {"_id": ObjectId(current_user), "liked_videos": {"$ne": video_oid}},
        {"$addToSet": {"liked_videos": video_oid}}
    )
    if result.modified_count != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video already liked"
        )

    user = await db.users.find_one(
        {"_id": ObjectId(current_user)}, {"display_name": 1, "username": 1}
    ) or {}

    # A like means the user has seen the video, so record it in watch_history
    # the same as a >=25% watch -- the feed's exclude_watched filter then
    # keeps it out of their feed even if they liked it within the first 25%.
    # `portion` is deliberately left alone: it's only ever written by the
    # /watch endpoint from real playback, so a like never overwrites (or
    # invents) how much was actually watched. The row is kept on unlike;
    # they've still seen the video.
    now = datetime.utcnow()
    await db.watch_history.update_one(
        {
            "user_id": ObjectId(current_user),
            "video_id": video_oid,
        },
        {
            "$set": {"watched_at": now, "liked_at": now},
            "$setOnInsert": {"first_watched_at": now},
        },
        upsert=True,
    )

    # Increment like count in buffer
    await metrics_buffer.increment_like(video_id)
    await trending.record_once(db, video_id, actor=current_user, creator=video.get("creator_id"),
                               kind="like", likes=1)

    # Get current buffered count for response
    buffered = await metrics_buffer.get_buffered_counts(video_id)

    creator_id = video.get("creator_id")
    if background_tasks is not None:
        display_name = user.get("display_name") or user.get("username") or "Someone"
        description = (video.get("description") or "").strip()
        description_preview = description[:30] + ("..." if len(description) > 30 else "")
        thumbnail_url = (video.get("urls") or {}).get("thumbnail")
        recipient_tokens = []
        if creator_id and str(creator_id) != str(current_user):
            recipient = await db.users.find_one(
                {"_id": ObjectId(creator_id)},
                {"expo_push_tokens": 1},
            )
            recipient_tokens = (recipient or {}).get("expo_push_tokens") or []

        for token in recipient_tokens:
            background_tasks.add_task(
                send_push_message,
                token,
                description_preview,
                {"video_id": video_id, "liker_id": str(current_user)},
                f"{display_name} liked your video",
                thumbnail_url,
            )

    if creator_id and str(creator_id) != str(current_user):
        await db.notifications.insert_one({
            "recipient_id": ObjectId(creator_id),
            "type": "like",
            "user_id": ObjectId(current_user),
            "post_id": video_oid,
            "date": datetime.utcnow(),
        })

    try:
        req = AddRating(current_user, video_id, rating=1.0, cascade_create=True, recomm_id=recomm_id)
        req.timeout = 5000
        await recombee_send(req)
    except Exception:
        pass

    return {
        "message": "Video liked successfully",
        "current_likes": buffered["likes"]
    }

@router.delete("/{video_id}/like", status_code=status.HTTP_200_OK)
async def unlike_video(
    video_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Unlike a video (buffered)"""
    db = get_database()
    video_oid = _video_oid(video_id)
    await _active_video_or_404(db, video_oid, {"_id": 1})

    # Conditional pull: only a like that existed is taken off the count.
    result = await db.users.update_one(
        {"_id": ObjectId(current_user), "liked_videos": video_oid},
        {"$pull": {"liked_videos": video_oid}}
    )

    if result.modified_count == 1:
        await metrics_buffer.decrement_like(video_id)

        await db.notifications.delete_one({
            "type": "like",
            "user_id": ObjectId(current_user),
            "post_id": video_oid,
        })

        try:
            req = DeleteRating(current_user, video_id)
            req.timeout = 5000
            await recombee_send(req)
        except Exception:
            pass

    # Get current buffered count for response
    buffered = await metrics_buffer.get_buffered_counts(video_id)

    return {
        "message": "Video unliked successfully",
        "current_likes": buffered["likes"]
    }

@router.post("/{video_id}/bookmark", status_code=status.HTTP_200_OK)
async def bookmark_video(
    video_id: str,
    current_user: str = Depends(get_current_active_user),
    background_tasks: BackgroundTasks = None,
    recomm_id: Optional[str] = None,
):
    """Bookmark a video"""
    db = get_database()
    video_oid = _video_oid(video_id)

    video = await _active_video_or_404(
        db, video_oid, {"description": 1, "urls.thumbnail": 1, "creator_id": 1, "privacy": 1},
    )
    if not _visible_to(video, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    # Conditional add: a repeat bookmark is a no-op, not a second count.
    result = await db.users.update_one(
        {"_id": ObjectId(current_user), "bookmarked_videos": {"$ne": video_oid}},
        {"$addToSet": {"bookmarked_videos": video_oid}}
    )
    if result.modified_count != 1:
        return {"message": "Video bookmarked successfully"}

    # Increment bookmark count directly (not buffered for MVP)
    await db.videos.update_one(
        {"_id": video_oid},
        {"$inc": {"bookmarks_count": 1}}
    )
    recommendations.mark_stats_dirty([video_id])

    if background_tasks is not None:
        creator_id = video.get("creator_id")
        if creator_id and str(creator_id) != str(current_user):
            user = await db.users.find_one(
                {"_id": ObjectId(current_user)}, {"display_name": 1, "username": 1}
            ) or {}
            recipient = await db.users.find_one(
                {"_id": ObjectId(creator_id)},
                {"expo_push_tokens": 1},
            )
            recipient_tokens = (recipient or {}).get("expo_push_tokens") or []
            display_name = user.get("display_name") or user.get("username") or "Someone"
            description = (video.get("description") or "").strip()
            description_preview = description[:30] + ("..." if len(description) > 30 else "")
            thumbnail_url = (video.get("urls") or {}).get("thumbnail")
            for token in recipient_tokens:
                background_tasks.add_task(
                    send_push_message,
                    token,
                    description_preview,
                    {"video_id": video_id, "saver_id": str(current_user)},
                    f"{display_name} saved your video",
                    thumbnail_url,
                )

    try:
        req = AddBookmark(current_user, video_id, cascade_create=True, recomm_id=recomm_id)
        req.timeout = 5000
        await recombee_send(req)
    except Exception:
        pass

    return {"message": "Video bookmarked successfully"}

@router.delete("/{video_id}/bookmark", status_code=status.HTTP_200_OK)
async def unbookmark_video(
    video_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Remove bookmark from a video"""
    db = get_database()
    video_oid = _video_oid(video_id)
    await _active_video_or_404(db, video_oid, {"_id": 1})

    # Conditional pull: only a bookmark that existed lowers the count.
    result = await db.users.update_one(
        {"_id": ObjectId(current_user), "bookmarked_videos": video_oid},
        {"$pull": {"bookmarked_videos": video_oid}}
    )

    if result.modified_count == 1:
        await db.videos.update_one(
            {"_id": video_oid, "bookmarks_count": {"$gt": 0}},
            {"$inc": {"bookmarks_count": -1}}
        )
        recommendations.mark_stats_dirty([video_id])

        try:
            req = DeleteBookmark(current_user, video_id)
            req.timeout = 5000
            await recombee_send(req)
        except Exception:
            pass

    return {"message": "Bookmark removed successfully"}
