from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Response, status
from app.core.database import get_database
from app.api.deps import get_optional_active_user
from app.services.metrics_service import metrics_buffer
from app.services.recombee_service import recombee_send
from recombee_api_client.api_requests import RecommendItemsToUser, RecommendNextItems
from pydantic import BaseModel
from bson import ObjectId

router = APIRouter()
#deploy

# Fields the client may sort the v1 feed by; anything else falls back to created_at.
SORTABLE_FIELDS = {"created_at", "views_count", "likes_count"}
class FeedVideo(BaseModel):
    id: str
    creator_id: str
    title: Optional[str]
    description: Optional[str] = None
    thumbnail: Optional[str] = None
    remoteUrl: Optional[str] = None
    remoteUrl_CF: Optional[str] = None
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
    supports_landscape: bool = False
    created_at: Optional[datetime] = None


@router.get("/", response_model=List[FeedVideo])
async def get_feed(
    response: Response,
    current_user: Optional[str] = Depends(get_optional_active_user),
    skip: int = 0,
    limit: int = 20,
    user_id: Optional[str] = None,
    video_id: Optional[str] = None,
    saved: bool = False,
    exclude_following: bool = False,
    sorted_by: Optional[str] = None,
    exclude_watched: bool = False,
):
    """Get personalized video feed, videos from a specific user, or saved videos"""
    db = get_database()

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
        # For saved videos, start from users collection
        target_user_id = user_id if user_id else current_user
        pipeline = [
            {"$match": {"_id": ObjectId(target_user_id)}},
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
            "$or": [
                {"videos.isReadyToStream": True},
                {"videos.isReadyToStream": {"$exists": False}}
            ]
            }},
        ]
        docs = await db.users.aggregate(pipeline).to_list(length=limit)
        has_more = len(docs) == limit
    else:
        # For all other cases, use videos collection with dynamic match conditions
        match_conditions = {
            "is_active": True,
            "$and": [
                        {
            "$or": [
                {"isReadyToStream": True},
                {"isReadyToStream": {"$exists": False}}
                ]
            }
            ]
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
        pipeline = [
            {"$match": match_conditions},
            {"$sort": sort_stage},
            {"$skip": skip},
            {"$limit": limit},
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
                    "created_at": 1,
                    "thumbnail": "$urls.thumbnail",
                    "is_active": 1,
                    "supports_landscape": 1,
                    "user": {
                        "username": "$creator.username",
                        "display_name": "$creator.display_name",
                        "profile_picture": "$creator.profile_picture",
                        "is_active": "$creator.is_active"
                    },
                }
            },
        ]

        docs = await db.videos.aggregate(pipeline).to_list(length=limit)
        # Captured before watch-filtering below can shrink docs: has_more
        # reflects whether the raw skip/limit window was actually full, not
        # how many of those survived filtering. Those are different
        # questions once filtering happens post-fetch -- a full raw window
        # with 3 watched videos filtered out still means more content exists
        # at the next skip, even though this page comes back short.
        has_more = len(docs) == limit

        if apply_watch_filter and docs:
            # Bounded to this batch's own ids -- never the user's full watch
            # history -- so this stays cheap no matter how much they've
            # watched in total.
            candidate_ids = [ObjectId(d["_id"]) for d in docs]
            watched_cursor = db.watch_history.find(
                {"user_id": ObjectId(current_user), "video_id": {"$in": candidate_ids}},
                {"video_id": 1},
            )
            watched_ids = {str(w["video_id"]) async for w in watched_cursor}
            docs = [d for d in docs if d["_id"] not in watched_ids]

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
        user_info = video.get("user", {})
        videos.append(
            FeedVideo(
                id=feed_video_id,
                creator_id=str(video["creator_id"]),
                title=video.get("title"),
                description=video.get("description"),
                thumbnail=video.get("thumbnail"),
                views_count=video.get("views_count", 0),
                likes_count=video.get("likes_count", 0),
                comments_count=video.get("comments_count", 0),
                remoteUrl=video.get("remoteUrl"),
                remoteUrl_CF=video.get("remoteUrl_CF"),
                is_ad=False,
                buffered_views=buffered["views"],
                buffered_likes=buffered["likes"],
                user=user_info,
                has_liked=has_liked,
                is_bookmarked=is_bookmarked,
                is_following=is_following,
                supports_landscape=video.get("supports_landscape", False),
                created_at=video.get("created_at"),
            )
        )

    if video_id and skip == 0:
        if not ObjectId.is_valid(video_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid video_id format",
            )

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
                    FeedVideo(
                        id=featured_id,
                        creator_id=str(featured["creator_id"]),
                        title=featured.get("title"),
                        description=featured.get("description"),
                        thumbnail=featured.get("thumbnail"),
                        views_count=featured.get("views_count", 0),
                        likes_count=featured.get("likes_count", 0),
                        comments_count=featured.get("comments_count", 0),
                        remoteUrl=featured.get("remoteUrl"),
                        remoteUrl_CF=featured.get("remoteUrl_CF"),
                        is_ad=False,
                        buffered_views=buffered["views"],
                        buffered_likes=buffered["likes"],
                        user=featured.get("user", {}),
                        has_liked=featured_id in liked_video_ids,
                        is_bookmarked=featured_id in bookmarked_video_ids,
                        is_following=str(featured["creator_id"]) in following_ids,
                        supports_landscape=featured.get("supports_landscape", False),
                        created_at=featured.get("created_at"),
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

    return videos


@router.get("/v2", response_model=List[FeedVideo])
async def get_feed_v2(
    scenario: str,
    current_user: Optional[str] = Depends(get_optional_active_user),
    limit: int = 20,
    next_recomm_id: Optional[str] = None,
    video_id: Optional[str] = None,
):
    db = get_database()

    recombee_user_id = current_user or "temp-user"
    recombee_limit = limit - 1 if video_id else limit

    base_filter = "'is_active' == true AND 'privacy' == \"public\" AND 'is_ready_to_stream' == true"

    async def recombee_fallback(count: int):
        req = RecommendItemsToUser(
            recombee_user_id,
            count,
            scenario=scenario,
            cascade_create=True,
            rotation_rate=0.5,
            filter=base_filter,
        )
        req.timeout = 5000
        return await recombee_send(req)

    if next_recomm_id:
        req = RecommendNextItems(next_recomm_id, recombee_limit)
        req.timeout = 5000
        result = await recombee_send(req)
    else:
        recent_filter = base_filter + " AND 'created_at' > now() - (10 * 24 * 60 * 60)"
        req = RecommendItemsToUser(
            recombee_user_id,
            recombee_limit,
            scenario=scenario,
            cascade_create=True,
            rotation_rate=0.5,
            filter=recent_filter,
        )
        req.timeout = 5000
        result = await recombee_send(req)

    recomms = result.get("recomms", [])

    if len(recomms) < recombee_limit:
        # Batch ran short (recency pool thin, or paginated batch exhausted) - top up
        # from the unfiltered pool rather than treating "fewer than asked" as "list ended".
        fallback_result = await recombee_fallback(recombee_limit - len(recomms))
        seen_ids = {r["id"] for r in recomms}
        extra_recomms = [r for r in fallback_result.get("recomms", []) if r["id"] not in seen_ids]
        result = fallback_result
        result["recomms"] = recomms + extra_recomms

    recomm_id = result.get("recommId")

    recommended_ids = [ObjectId(r["id"]) for r in result.get("recomms", [])]

    if not recommended_ids:
        return []

    user_doc = await db.users.find_one(
        {"_id": ObjectId(current_user)},
        {"liked_videos": 1, "bookmarked_videos": 1, "following": 1},
    ) or {}

    liked_video_ids = set(str(v) for v in user_doc.get("liked_videos", []))
    bookmarked_video_ids = set(str(v) for v in user_doc.get("bookmarked_videos", []))
    following_ids = set(str(v) for v in user_doc.get("following", []))

    pipeline = [
        {"$match": {"_id": {"$in": recommended_ids}}},
        {
            "$lookup": {
                "from": "users",
                "localField": "creator_id",
                "foreignField": "_id",
                "as": "creator",
            }
        },
        {"$unwind": {"path": "$creator", "preserveNullAndEmptyArrays": True}},
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

    docs = await db.videos.aggregate(pipeline).to_list(length=limit)

    # preserve Recombee order
    doc_map = {doc["_id"]: doc for doc in docs}
    ordered = [doc_map[str(oid)] for oid in recommended_ids if str(oid) in doc_map]

    videos = []
    for video in ordered:
        feed_video_id = video["_id"]
        buffered = await metrics_buffer.get_buffered_counts(feed_video_id)
        videos.append(
            FeedVideo(
                id=feed_video_id,
                creator_id=video["creator_id"],
                title=video.get("title"),
                description=video.get("description"),
                thumbnail=video.get("thumbnail"),
                views_count=video.get("views_count", 0),
                likes_count=video.get("likes_count", 0),
                comments_count=video.get("comments_count", 0),
                remoteUrl=video.get("remoteUrl"),
                remoteUrl_CF=video.get("remoteUrl_CF"),
                buffered_views=buffered["views"],
                buffered_likes=buffered["likes"],
                user=video.get("user", {}),
                has_liked=feed_video_id in liked_video_ids,
                is_bookmarked=feed_video_id in bookmarked_video_ids,
                is_following=str(video["creator_id"]) in following_ids,
                recomm_id=recomm_id,
                supports_landscape=video.get("supports_landscape", False),
                created_at=video.get("created_at"),
            )
        )

    if video_id and ObjectId.is_valid(video_id):
        featured_pipeline = [
            {"$match": {"_id": ObjectId(video_id), "is_active": True}},
            {"$lookup": {"from": "users", "localField": "creator_id", "foreignField": "_id", "as": "creator"}},
            {"$unwind": {"path": "$creator", "preserveNullAndEmptyArrays": True}},
            {"$project": {
                "_id": {"$toString": "$_id"},
                "creator_id": {"$toString": "$creator_id"},
                "title": 1, "description": 1, "remoteUrl": 1, "remoteUrl_CF": 1,
                "views_count": 1, "likes_count": 1, "comments_count": 1,
                "thumbnail": "$urls.thumbnail", "privacy": 1, "supports_landscape": 1,
                "created_at": 1,
                "user": {"username": "$creator.username", "display_name": "$creator.display_name",
                         "profile_picture": "$creator.profile_picture", "is_active": "$creator.is_active"},
            }},
        ]
        featured_doc = await db.videos.aggregate(featured_pipeline).to_list(length=1)
        if featured_doc:
            f = featured_doc[0]
            if f.get("privacy") == "public" or str(f.get("creator_id")) == current_user:
                fid = f["_id"]
                videos = [v for v in videos if v.id != fid]
                videos.insert(0, FeedVideo(
                    id=fid,
                    creator_id=f["creator_id"],
                    title=f.get("title"),
                    description=f.get("description"),
                    thumbnail=f.get("thumbnail"),
                    views_count=f.get("views_count", 0),
                    likes_count=f.get("likes_count", 0),
                    comments_count=f.get("comments_count", 0),
                    remoteUrl=f.get("remoteUrl"),
                    remoteUrl_CF=f.get("remoteUrl_CF"),
                    buffered_views=0,
                    buffered_likes=0,
                    user=f.get("user", {}),
                    has_liked=fid in liked_video_ids,
                    is_bookmarked=fid in bookmarked_video_ids,
                    is_following=str(f["creator_id"]) in following_ids,
                    recomm_id=recomm_id,
                    supports_landscape=f.get("supports_landscape", False),
                    created_at=f.get("created_at"),
                ))
                videos = videos[:limit]

    return videos
