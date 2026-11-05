from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from app.core.database import get_database
from app.api.deps import get_current_user, get_optional_active_user, oauth2_scheme_optional
from app.services.metrics_service import metrics_buffer
from app.services import for_you, new_feed, trending
from app.services.recommendations import viewer_id
from pydantic import BaseModel
from bson import ObjectId

router = APIRouter()
#deploy

# Fields the client may sort the v1 feed by; anything else falls back to created_at.
SORTABLE_FIELDS = {"created_at", "views_count", "likes_count"}

class FeedSound(BaseModel):
    id: Optional[str] = None
    title: Optional[str] = None
    uri: Optional[str] = None
    artist: Optional[str] = None

class FeedVideo(BaseModel):
    id: str
    creator_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    thumbnail: Optional[str] = None
    remoteUrl: Optional[str] = None
    remoteUrl_CF: Optional[str] = None
    media_type: str = "video"
    images: List[str] = []
    views_count: int
    likes_count: int
    comments_count: int = 0
    is_ad: bool = False
    # Include buffered counts
    buffered_views: int = 0
    buffered_likes: int = 0
    user: dict = {}
    has_liked: bool = False
    is_bookmarked: bool = False
    is_following: bool = False
    recomm_id: Optional[str] = None
    # For You only: which source picked it and the A/B variant (for measurement).
    source: Optional[str] = None
    variant: Optional[str] = None
    supports_landscape: bool = False
    created_at: Optional[datetime] = None
    # The song on the post, if one was picked. Older posts have none.
    sound: Optional[FeedSound] = None


def _count(value) -> int:
    """A counter from a document that may hold junk (None, strings, negatives)."""
    try:
        n = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(n, 0)


def _text(value) -> Optional[str]:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _feed_video(video: dict, **fields) -> FeedVideo:
    """
    Build a FeedVideo from a projected (or raw) video document. Older documents
    can be missing fields or hold null in them, so every value falls back to a
    safe default instead of failing validation for the whole page.
    """
    sound = video.get("sound")
    thumbnail = video.get("thumbnail")
    if thumbnail is None and isinstance(video.get("urls"), dict):
        thumbnail = video["urls"].get("thumbnail")
    images = video.get("images")
    return FeedVideo(
        creator_id=str(video.get("creator_id")),
        title=_text(video.get("title")),
        description=_text(video.get("description")),
        thumbnail=thumbnail if isinstance(thumbnail, str) else None,
        views_count=_count(video.get("views_count")),
        likes_count=_count(video.get("likes_count")),
        comments_count=_count(video.get("comments_count")),
        remoteUrl=video.get("remoteUrl") if isinstance(video.get("remoteUrl"), str) else None,
        remoteUrl_CF=video.get("remoteUrl_CF") if isinstance(video.get("remoteUrl_CF"), str) else None,
        media_type=video.get("media_type") if isinstance(video.get("media_type"), str) else "video",
        images=[i for i in images if isinstance(i, str)] if isinstance(images, list) else [],
        user=video.get("user") if isinstance(video.get("user"), dict) else {},
        supports_landscape=bool(video.get("supports_landscape")),
        created_at=video.get("created_at"),
        sound=sound if isinstance(sound, dict) and sound.get("title") else None,
        **fields,
    )


@router.get("/", response_model=List[FeedVideo])
async def get_feed(
    response: Response,
    current_user: Optional[str] = Depends(get_optional_active_user),
    skip: int = Query(0, ge=0, le=100_000),
    limit: int = Query(20, ge=1, le=50),
    cursor: Optional[str] = Query(None, max_length=512),
    user_id: Optional[str] = None,
    video_id: Optional[str] = None,
    saved: bool = False,
    exclude_following: bool = False,
    sorted_by: Optional[str] = None,
    exclude_watched: bool = False,
):
    """Get personalized video feed, videos from a specific user, or saved videos.

    The New tab: send cursor=start, then the X-Next-Cursor header from each
    response. Without a cursor it pages by skip, as older app builds do."""
    db = get_database()
    if cursor is not None and (saved or (sorted_by or "created_at") != "created_at"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="cursor works only with the newest-first feed")
    next_cursor = None

    if user_id is not None and not ObjectId.is_valid(user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format",
        )
    if video_id is not None and not ObjectId.is_valid(video_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid video_id format",
        )

    user_doc = None
    liked_video_ids = set()
    bookmarked_video_ids = set()
    blocked_users = []
    blocked_by = []
    following_ids = set()

    if current_user:
        user_doc = await db.users.find_one(
            {"_id": ObjectId(current_user)},
            {
                "liked_videos": 1,
                "bookmarked_videos": 1,
                "blocked_users": 1,
                "blocked_by": 1,
                "following": 1,
            },
        ) or {}

        liked_video_ids = set(str(v) for v in user_doc.get("liked_videos", []))
        bookmarked_video_ids = set(str(v) for v in user_doc.get("bookmarked_videos", []))
        blocked_users = user_doc.get("blocked_users", [])
        blocked_by = user_doc.get("blocked_by", [])
        following_ids = set(str(v) for v in user_doc.get("following", []))

    # Single unified pipeline that handles all scenarios
    if saved:
        if not current_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required to access saved videos",
            )
        # Saved videos are private to their owner: always the caller's own
        # list, whatever user_id says.
        viewer_oid = ObjectId(current_user)
        raw_saved = (user_doc or {}).get("bookmarked_videos") or []
        # has_more comes from the raw slice, before inactive/private videos
        # are filtered out, so a page with hidden entries still pages on.
        has_more = len(raw_saved[skip:skip + limit]) == limit
        pipeline = [
            {"$match": {"_id": viewer_oid}},
            {
            "$project": {
                "bookmarked_videos": {
                "$slice": ["$bookmarked_videos", skip, limit]
                }
            }
            },
            {
            "$lookup": {
                "from": "videos",
                "localField": "bookmarked_videos",
                "foreignField": "_id",
                "as": "videos",
            }
            },
            {"$unwind": "$videos"},
            {"$match": {
            "videos.is_active": True,
            "$and": [
                {"$or": [
                    {"videos.isReadyToStream": True},
                    {"videos.isReadyToStream": {"$exists": False}}
                ]},
                # A post saved while public and since made private stays
                # visible only to its creator.
                {"$or": [
                    {"videos.privacy": "public"},
                    {"videos.creator_id": viewer_oid},
                ]},
            ],
            }},
            # The creator, so saved posts show who posted them like the other feeds do.
            {
                "$lookup": {
                    "from": "users",
                    "localField": "videos.creator_id",
                    "foreignField": "_id",
                    "as": "creator",
                }
            },
            {"$unwind": {"path": "$creator", "preserveNullAndEmptyArrays": True}},
            {
                "$addFields": {
                    "videos.user": {
                        "username": "$creator.username",
                        "display_name": "$creator.display_name",
                        "profile_picture": "$creator.profile_picture",
                        "is_active": "$creator.is_active",
                    }
                }
            },
            {"$project": {"videos": 1}},
        ]
        docs = await db.users.aggregate(pipeline).to_list(length=limit)
    else:
        # For all other cases, use videos collection with dynamic match conditions
        match_conditions = {
            "is_active": True,
            "$and": [new_feed.ready_filter()],
        }

        if user_id:
            # Get videos from specific user
            match_conditions.update(
                {
                    "creator_id": ObjectId(user_id),
                    "privacy": "public",
                }
            )
        elif current_user:
            # Get personalized feed
            # Exclude blocked users, the current user's own videos, and (optionally) followed creators
            exclude_creator_ids = set(blocked_users + blocked_by)
            exclude_creator_ids.add(ObjectId(current_user))
            if exclude_following:
                user_following_ids = user_doc.get("following", []) if user_doc else []
                exclude_creator_ids.update(ObjectId(uid) for uid in user_following_ids)

            match_conditions.update({"privacy": "public"})
            if exclude_creator_ids:
                match_conditions["creator_id"] = {"$nin": list(exclude_creator_ids)}
        else:
            # Anonymous feed: show public, active videos only
            match_conditions.update(
                {
                    "privacy": "public",
                }
            )

        # sorted_by lands straight in $sort, so an unrecognised field name means an
        # unindexed in-memory sort (capped at 32MB). Fall back to created_at instead.
        # The _id tiebreaker keeps $skip/$limit paging stable: without it, docs sharing
        # a sort value have no defined order between requests, so a page boundary can
        # repeat or drop videos.
        sort_field = sorted_by if sorted_by in SORTABLE_FIELDS else "created_at"
        sort_stage = {sort_field: -1, "_id": -1}

        # Exclusion only makes sense for the generic discovery feed -- a
        # profile grid (user_id) should show all of a creator's videos, and
        # saved videos are handled in the branch above entirely. Anonymous
        # requests have no stable identity to key watch_history on.
        #
        # exclude_watched is an explicit client opt-in, not just "has a
        # current_user": an app build that predates this feature sends
        # neither this flag, and keeps computing skip locally exactly as
        # before. Gating on it means an unupdated client gets byte-for-byte
        # its old behavior until it ships the exclusion-aware code.
        apply_watch_filter = bool(current_user) and not user_id and exclude_watched

        # skip/limit stay exactly as-is regardless of filtering: this pipeline
        # doesn't backfill past `limit` to compensate for filtered-out
        # videos, so the raw window a page covers is always [skip, skip+limit)
        # -- unaffected by how many of those turn out to be watched. That
        # keeps skip stable across pages at the cost of pages sometimes
        # coming back shorter than `limit` (or empty) when several of the
        # raw candidates happen to already be watched.
        project = {
                    "_id": {"$toString": "$_id"},
                    "creator_id": {"$toString": "$creator_id"},
                    "title": 1,
                    "description": 1,
                    "remoteUrl": 1,
                    "remoteUrl_CF": 1,
                    "views_count": 1,
                    "likes_count": 1,
                    "comments_count": 1,
                    "created_at": 1,
                    "thumbnail": "$urls.thumbnail",
                    "media_type": {"$ifNull": ["$media_type", "video"]},
                    "images": {"$ifNull": ["$images", []]},
                    "sound": 1,
                    "is_active": 1,
                    "supports_landscape": 1,
                    "user": {
                        "username": "$creator.username",
                        "display_name": "$creator.display_name",
                        "profile_picture": "$creator.profile_picture",
                        "is_active": "$creator.is_active"
                    },
        }

        if cursor is not None:
            docs, next_cursor = await new_feed.cursor_page(
                db, match_conditions, limit=limit, cursor=cursor, viewer=None if user_id else current_user,
                exclude_watched=apply_watch_filter, cap_creators=not user_id, project=project,
            )
            has_more = next_cursor is not None
            apply_watch_filter = False   # already applied while paging
        else:
            skip_match = match_conditions if user_id else await new_feed.skip_exclusions(
                db, match_conditions, current_user)
            pipeline = [
                {"$match": skip_match},
                {"$sort": sort_stage},
                {"$skip": skip},
                {"$limit": limit},
                {"$lookup": {"from": "users", "localField": "creator_id",
                             "foreignField": "_id", "as": "creator"}},
                {"$unwind": {"path": "$creator", "preserveNullAndEmptyArrays": True}},
                {"$project": project},
            ]
            docs = await db.videos.aggregate(pipeline).to_list(length=limit)
        # Captured before watch-filtering below can shrink docs: has_more
        # reflects whether the raw skip/limit window was actually full, not
        # how many of those survived filtering. Those are different
        # questions once filtering happens post-fetch -- a full raw window
        # with 3 watched videos filtered out still means more content exists
        # at the next skip, even though this page comes back short.
            has_more = len(docs) == limit

            # Bounded to this batch's own ids -- never the user's full watch
            # history -- so this stays cheap no matter how much they've
            # watched in total. "Not interested" always applies; watched
            # posts only when the client opted in.
            if current_user and docs and not user_id:
                candidate_ids = [ObjectId(d["_id"]) for d in docs]
                q = {"user_id": ObjectId(current_user), "video_id": {"$in": candidate_ids}}
                if not apply_watch_filter:
                    q["not_interested"] = True
                hidden_ids = {str(w["video_id"]) async for w in db.watch_history.find(q, {"video_id": 1})}
                docs = [d for d in docs if d["_id"] not in hidden_ids]
            # Posts of accounts that were deactivated before their posts were.
            if not user_id:
                docs = [d for d in docs if (d.get("user") or {}).get("is_active") is not False]

    videos = []
    for doc in docs:
        # For saved videos, video data is in doc["videos"], otherwise it's directly in doc
        video = doc.get("videos", doc) if saved else doc
        feed_video_id = str(video["_id"])
        has_liked = feed_video_id in liked_video_ids
        is_bookmarked = feed_video_id in bookmarked_video_ids
        is_following = str(video["creator_id"]) in following_ids
        # Get buffered counts
        buffered = await metrics_buffer.get_buffered_counts(feed_video_id)
        videos.append(
            _feed_video(
                video,
                id=feed_video_id,
                is_ad=False,
                buffered_views=buffered["views"],
                buffered_likes=buffered["likes"],
                has_liked=has_liked,
                is_bookmarked=is_bookmarked,
                is_following=is_following,
            )
        )

    if video_id and skip == 0 and cursor in (None, new_feed.START):
        featured_pipeline = [
            {
                "$match": {
                    "_id": ObjectId(video_id),
                    "is_active": True,
                    "$or": [
                        {"isReadyToStream": True},
                        {"isReadyToStream": {"$exists": False}},
                    ],
                }
            },
            {
                "$lookup": {
                    "from": "users",
                    "localField": "creator_id",
                    "foreignField": "_id",
                    "as": "creator",
                }
            },
            {
                "$unwind": {
                    "path": "$creator",
                    "preserveNullAndEmptyArrays": True,
                }
            },
            {
                "$project": {
                    "_id": {"$toString": "$_id"},
                    "creator_id": {"$toString": "$creator_id"},
                    "title": 1,
                    "description": 1,
                    "remoteUrl": 1,
                    "remoteUrl_CF": 1,
                    "views_count": 1,
                    "likes_count": 1,
                    "comments_count": 1,
                    "thumbnail": "$urls.thumbnail",
                    "media_type": {"$ifNull": ["$media_type", "video"]},
                    "images": {"$ifNull": ["$images", []]},
                    "sound": 1,
                    "privacy": 1,
                    "supports_landscape": 1,
                    "created_at": 1,
                    "user": {
                        "username": "$creator.username",
                        "display_name": "$creator.display_name",
                        "profile_picture": "$creator.profile_picture",
                        "is_active": "$creator.is_active",
                    },
                }
            },
        ]

        featured_doc = await db.videos.aggregate(featured_pipeline).to_list(length=1)
        if featured_doc:
            featured = featured_doc[0]
            if featured.get("privacy") == "public" or (
                current_user and str(featured.get("creator_id")) == current_user
            ):
                featured_id = str(featured["_id"])
                buffered = await metrics_buffer.get_buffered_counts(featured_id)

                videos = [v for v in videos if v.id != featured_id]
                videos.insert(
                    0,
                    _feed_video(
                        featured,
                        id=featured_id,
                        is_ad=False,
                        buffered_views=buffered["views"],
                        buffered_likes=buffered["likes"],
                        has_liked=featured_id in liked_video_ids,
                        is_bookmarked=featured_id in bookmarked_video_ids,
                        is_following=str(featured["creator_id"]) in following_ids,
                    ),
                )
                if len(videos) > limit:
                    videos = videos[:limit]

    # Signals whether the raw skip/limit window was full, independent of the
    # returned list's length -- see the has_more comment above. v2 has no
    # equivalent header: Recombee already tops up short batches internally
    # (see get_feed_v2's fallback_result logic) and reports true exhaustion
    # as a genuinely empty response, so the client's existing empty-page
    # check is already a reliable signal there.
    response.headers["X-Has-More"] = "true" if has_more else "false"
    if next_cursor:
        response.headers["X-Next-Cursor"] = next_cursor

    return videos


@router.get("/v2", response_model=List[FeedVideo])
async def get_feed_v2(
    scenario: str,
    current_user: Optional[str] = Depends(get_optional_active_user),
    limit: int = Query(20, ge=1, le=50),
    next_recomm_id: Optional[str] = None,
    video_id: Optional[str] = None,
    x_device_id: Optional[str] = Header(None, alias="X-Device-Id"),
):
    """For You feed. See app/services/for_you.py for how a page is put together."""
    db = get_database()
    viewer = viewer_id(current_user, x_device_id)

    featured = None
    if video_id and ObjectId.is_valid(video_id):
        f = await db.videos.find_one({"_id": ObjectId(video_id), "is_active": True},
                                     {"creator_id": 1, "privacy": 1})
        if f and (f.get("privacy") in ("public", "unlisted")
                  or (current_user and str(f.get("creator_id")) == current_user)):
            featured = f

    page = await for_you.build_page(
        db, viewer=viewer, current_user=current_user, scenario=scenario, limit=limit,
        next_recomm_id=next_recomm_id, featured=featured,
    )
    items = ([{"id": str(featured["_id"]), "source": "featured"}] if featured else []) + page.items
    if not items:
        return []

    user_doc = (await db.users.find_one(
        {"_id": ObjectId(current_user)},
        {"liked_videos": 1, "bookmarked_videos": 1, "following": 1},
    ) or {}) if current_user else {}
    liked_video_ids = set(str(v) for v in user_doc.get("liked_videos", []))
    bookmarked_video_ids = set(str(v) for v in user_doc.get("bookmarked_videos", []))
    following_ids = set(str(v) for v in user_doc.get("following", []))

    oids = [ObjectId(it["id"]) for it in items]
    pipeline = [
        {"$match": {"_id": {"$in": oids}, "is_active": True}},
        {"$lookup": {"from": "users", "localField": "creator_id", "foreignField": "_id", "as": "creator"}},
        {"$unwind": {"path": "$creator", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "_id": {"$toString": "$_id"},
            "creator_id": {"$toString": "$creator_id"},
            "title": 1, "description": 1, "remoteUrl": 1, "remoteUrl_CF": 1,
            "views_count": 1, "likes_count": 1, "comments_count": 1,
            "thumbnail": "$urls.thumbnail",
            "media_type": {"$ifNull": ["$media_type", "video"]},
            "images": {"$ifNull": ["$images", []]},
            "sound": 1, "supports_landscape": 1, "created_at": 1,
            "user": {
                "username": "$creator.username",
                "display_name": "$creator.display_name",
                "profile_picture": "$creator.profile_picture",
                "is_active": "$creator.is_active",
            },
        }},
    ]
    docs = await db.videos.aggregate(pipeline).to_list(length=len(oids))
    doc_map = {d["_id"]: d for d in docs}

    videos, served = [], []
    for it in items:
        video = doc_map.get(it["id"])
        if not video:
            continue
        buffered = await metrics_buffer.get_buffered_counts(it["id"])
        videos.append(_feed_video(
            video,
            id=it["id"],
            buffered_views=buffered["views"],
            buffered_likes=buffered["likes"],
            has_liked=it["id"] in liked_video_ids,
            is_bookmarked=it["id"] in bookmarked_video_ids,
            is_following=str(video["creator_id"]) in following_ids,
            recomm_id=page.recomm_id,
            source=it.get("source"),
            variant=page.variant,
        ))
        served.append(it)
        if len(videos) >= limit:
            break

    await for_you.record_impressions(
        db, viewer=viewer, current_user=current_user, variant=page.variant,
        recomm_id=page.recomm_id, items=served,
    )
    return videos


async def _viewer_if_signed_in(token: Optional[str] = Depends(oauth2_scheme_optional)) -> Optional[str]:
    if not token:
        return None
    try:
        return await get_current_user(token)
    except HTTPException:
        return None


@router.get("/trending", response_model=List[FeedVideo])
async def get_trending(
    country: Optional[str] = Query(None, min_length=2, max_length=8),
    limit: int = Query(20, ge=1, le=100),
    current_user: Optional[str] = Depends(_viewer_if_signed_in),
):
    """What is taking off on Wano right now: the viewer's country first (or
    `country`), then the rest. See app/services/trending.py."""
    db = get_database()
    user_doc = {}
    if current_user and ObjectId.is_valid(current_user):
        user_doc = await db.users.find_one(
            {"_id": ObjectId(current_user)},
            {"liked_videos": 1, "bookmarked_videos": 1, "following": 1, "localization": 1, "is_active": 1},
        ) or {}
        if user_doc.get("is_active") is False:
            user_doc, current_user = {}, None
    if not country:
        country = ((user_doc.get("localization") or {}).get("country")) or None

    docs = await trending.top(db, country=country, limit=limit)
    if not docs:
        return []
    oids = [d["_id"] for d in docs]
    pipeline = [
        {"$match": {"_id": {"$in": oids}}},
        {"$lookup": {"from": "users", "localField": "creator_id", "foreignField": "_id", "as": "creator"}},
        {"$unwind": {"path": "$creator", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "_id": {"$toString": "$_id"}, "creator_id": {"$toString": "$creator_id"},
            "title": 1, "description": 1, "remoteUrl": 1, "remoteUrl_CF": 1,
            "views_count": 1, "likes_count": 1, "comments_count": 1,
            "thumbnail": "$urls.thumbnail", "media_type": {"$ifNull": ["$media_type", "video"]},
            "images": {"$ifNull": ["$images", []]}, "sound": 1, "supports_landscape": 1, "created_at": 1,
            "user": {"username": "$creator.username", "display_name": "$creator.display_name",
                     "profile_picture": "$creator.profile_picture", "is_active": "$creator.is_active"},
        }},
    ]
    by_id = {d["_id"]: d for d in await db.videos.aggregate(pipeline).to_list(length=len(oids))}
    liked = {str(v) for v in user_doc.get("liked_videos", [])}
    saved = {str(v) for v in user_doc.get("bookmarked_videos", [])}
    following = {str(v) for v in user_doc.get("following", [])}
    out = []
    for d in docs:
        vid = str(d["_id"])
        video = by_id.get(vid)
        if not video:
            continue
        buffered = await metrics_buffer.get_buffered_counts(vid)
        out.append(_feed_video(
            video, id=vid, buffered_views=buffered["views"], buffered_likes=buffered["likes"],
            has_liked=vid in liked, is_bookmarked=vid in saved,
            is_following=video["creator_id"] in following, source="trending",
        ))
    return out
