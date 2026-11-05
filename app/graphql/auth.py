"""
Auth helpers shared by GraphQL resolvers

app/graphql/auth.py
"""
from typing import Any, Dict, Optional

from app.core.database import get_database
from app.graphql.types import parse_object_id


def current_user_id(info) -> Optional[str]:
    return info.context.get("user_id")


def require_user_id(info) -> str:
    user_id = info.context.get("user_id")
    if not user_id:
        raise Exception("Authentication required")
    return user_id


async def require_user_doc(info) -> Dict[str, Any]:
    user_id = require_user_id(info)
    user = await get_database().users.find_one({"_id": parse_object_id(user_id)})
    if not user:
        raise Exception("User not found")
    return user


async def require_admin(info) -> Dict[str, Any]:
    user = await require_user_doc(info)
    if user.get("user_type") != "admin":
        raise Exception("Admin access required")
    return user


def visible_video_filter(viewer_id: Optional[str]) -> Dict[str, Any]:
    """Active videos that are public, or that the viewer created."""
    if viewer_id:
        return {
            "is_active": True,
            "$or": [
                {"privacy": "public"},
                {"creator_id": parse_object_id(viewer_id)},
            ],
        }
    return {"is_active": True, "privacy": "public"}


def linkable_video_filter(viewer_id: Optional[str]) -> Dict[str, Any]:
    """Opening one video by ID (a share link): public and unlisted work for
    anyone; private only for the creator. Lists keep using visible_video_filter."""
    allowed: list = [{"privacy": {"$in": ["public", "unlisted"]}}]
    if viewer_id:
        allowed.append({"creator_id": parse_object_id(viewer_id)})
    return {"is_active": True, "$or": allowed}
