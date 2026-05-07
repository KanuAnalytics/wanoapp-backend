from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from app.core.database import get_database
from app.api.deps import get_optional_active_user
from app.services.metrics_service import metrics_buffer
from app.services.recombee_service import recombee_client
from recombee_api_client.api_requests import RecommendItemsToUser, RecommendNextItems
from pydantic import BaseModel
from bson import ObjectId
import random

router = APIRouter()
#deploy
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


@router.get("/", response_model=List[FeedVideo])
async def get_feed(
    current_user: Optional[str] = Depends(get_optional_active_user),
    skip: int = 0,
    limit: int = 20,
    user_id: Optional[str] = None,
    video_id: Optional[str] = None,
    saved: bool = False,
    exclude_following: bool = False,
    sorted_by: Optional[str] = None,
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
                "localization": 1,
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
        cursor = db.users.aggregate(pipeline)
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
            exclude_creator_ids = []
            if exclude_following:
                user_following_ids = user_doc.get("following", []) if user_doc else []
                if user_following_ids:
                    exclude_creator_ids = [ObjectId(uid) for uid in user_following_ids]
            user_country = (user_doc or {}).get("localization", {}).get("country", "NG")
            user_languages = (user_doc or {}).get("localization", {}).get("languages", ["en"])

            match_conditions.update(
                {
                    "privacy": "public",
                    "$or": [
                        {"country": user_country},
                        {"language": {"$in": user_languages}},
                    ]
                }
            )
            if exclude_creator_ids:
                match_conditions["creator_id"] = {"$nin": exclude_creator_ids}

            # Exclude videos from blocked users
            exclude_ids = set(blocked_users + blocked_by)
            if exclude_ids:
                match_conditions["creator_id"] = {
                    **match_conditions.get("creator_id", {}),
                    "$nin": list(exclude_ids),
                }
        else:
            # Anonymous feed: show public, active videos only
            match_conditions.update(
                {
                    "privacy": "public",
                }
            )

        sort_stage = {sorted_by: -1} if sorted_by else {"created_at": -1}
        
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
                    "created_at": {
                        "$dateToString": {
                            "format": "%Y-%m-%dT%H:%M:%S.%LZ",
                            "date": "$created_at",
                        }
                    },
                    "thumbnail": "$urls.thumbnail",
                    "is_active": 1,
                    "user": {
                        "username": "$creator.username",
                        "display_name": "$creator.display_name",
                        "profile_picture": "$creator.profile_picture",
                        "is_active": "$creator.is_active"
                    },
                }
            },
        ]

        cursor = db.videos.aggregate(pipeline)

    videos = []
    async for doc in cursor:
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
                    ),
                )
                if len(videos) > limit:
                    videos = videos[:limit]

    # Insert ads (1:20 ratio) - only for personalized feed, not for specific user videos or saved videos
    if not user_id and not saved and len(videos) >= 20:
        # Insert an ad at a random position
        ad_position = random.randint(5, 15)
        # In production, fetch actual ad from campaigns
        ad = FeedVideo(
            id="ad_placeholder",
            creator_id="advertiser_id",
            title="Sponsored Content",
            views_count=0,
            likes_count=0,
            is_ad=True,
        )
        videos.insert(ad_position, ad)

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

    if next_recomm_id:
        req = RecommendNextItems(next_recomm_id, recombee_limit)
    else:
        req = RecommendItemsToUser(
            recombee_user_id,
            recombee_limit,
            scenario=scenario,
            cascade_create=True,
            filter="'is_active' == true AND 'privacy' == \"public\"",
        )
    req.timeout = 5000
    result = recombee_client.send(req)
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
                "thumbnail": "$urls.thumbnail", "privacy": 1,
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
                ))
                videos = videos[:limit]

    return videos
