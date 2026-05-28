"""
Notifications endpoints

app/api/v1/notifications.py
"""
import json
from fastapi import APIRouter
from bson import ObjectId
from bson.json_util import dumps
from app.core.database import get_database
from app.api.deps import get_current_active_user
from fastapi import Depends, Query
from typing import Optional

router = APIRouter()

@router.get("/")
async def get_notifications(
    skip: int = 0,
    limit: int = 10,
    type: Optional[str] = Query(None, description="Filter by notification type"),
    current_user: str = Depends(get_current_active_user),
):
    """Get notifications for the current user (fresh user/post info via lookup)"""
    db = get_database()

    match = {"recipient_id": ObjectId(current_user)}
    if type:
        match["type"] = type

    pipeline = [
        {"$match": match},
        {"$sort": {"date": -1}},
        {"$skip": skip},
        {"$limit": limit},
        {"$lookup": {
            "from": "users",
            "localField": "user_id",
            "foreignField": "_id",
            "as": "user",
        }},
        {"$unwind": {"path": "$user", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {
            "from": "videos",
            "localField": "post_id",
            "foreignField": "_id",
            "as": "post",
        }},
        {"$unwind": {"path": "$post", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {
            "from": "comments",
            "localField": "comment_id",
            "foreignField": "_id",
            "as": "comment",
        }},
        {"$unwind": {"path": "$comment", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {
            "from": "comments",
            "localField": "comment.parent_id",
            "foreignField": "_id",
            "as": "parent_comment",
        }},
        {"$unwind": {"path": "$parent_comment", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "_id": {"$toString": "$_id"},
            "type": 1,
            "date": {"$dateToString": {"format": "%Y-%m-%dT%H:%M:%S.%LZ", "date": "$date"}},
            "user": {
                "id": {"$toString": "$user._id"},
                "name": {"$ifNull": ["$user.display_name", "$user.username"]},
                "username": "$user.username",
                "profile_picture": "$user.profile_picture",
            },
            "is_following": {
                "$cond": [
                    {"$eq": ["$type", "follow"]},
                    {"$in": ["$recipient_id", {"$ifNull": ["$user.followers", []]}]},
                    "$$REMOVE",
                ]
            },
            "post": {
                "id": {"$toString": "$post._id"},
                "thumbnail": "$post.urls.thumbnail",
                "comment": {
                    "$cond": [
                        {"$eq": ["$type", "comment_reply"]},
                        "$parent_comment.content",
                        "$comment.content",
                    ]
                },
                "reply": {
                    "$cond": [
                        {"$eq": ["$type", "comment_reply"]},
                        "$comment.content",
                        None,
                    ]
                },
            },
        }},
    ]

    cursor = db.notifications.aggregate(pipeline)
    docs = await cursor.to_list(length=limit)
    return json.loads(dumps(docs))
