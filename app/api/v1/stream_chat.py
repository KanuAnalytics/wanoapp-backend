"""
Stream Chat endpoints

app/api/v1/stream_chat.py
"""
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from bson import ObjectId
from app.api.deps import get_current_active_user
from app.core.config import settings
from app.core.database import get_database
from app.services.stream_chat import get_stream_client, build_stream_user


router = APIRouter()


class StreamTokenResponse(BaseModel):
    api_key: str
    user_id: str
    token: str


class StreamUserSyncResponse(BaseModel):
    user_id: str
    synced: bool


class StreamChannelCreateRequest(BaseModel):
    channel_type: str = Field(default="messaging", min_length=1)
    channel_id: str | None = None
    members: list[str] = Field(..., min_items=1)
    name: str | None = None
    image: str | None = None
    data: dict[str, Any] | None = None


class StreamChannelCreateResponse(BaseModel):
    channel_id: str | None = None
    channel_type: str
    cid: str | None = None


def _parse_object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid user id: {value}",
        )


async def _load_user_doc(user_id: str) -> dict:
    db = get_database()
    return await db.users.find_one({"_id": _parse_object_id(user_id)})


@router.post("/token", response_model=StreamTokenResponse)
async def get_stream_token(current_user: str = Depends(get_current_active_user)):
    user = await _load_user_doc(current_user)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    client = get_stream_client()
    client.upsert_user(build_stream_user(current_user, user))
    token = client.create_token(current_user)

    return {
        "api_key": settings.STREAM_API_KEY,
        "user_id": current_user,
        "token": token,
    }


@router.post("/users/sync", response_model=StreamUserSyncResponse)
async def sync_stream_user(current_user: str = Depends(get_current_active_user)):
    user = await _load_user_doc(current_user)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    client = get_stream_client()
    client.upsert_user(build_stream_user(current_user, user))
    return {"user_id": current_user, "synced": True}


@router.post("/channels", response_model=StreamChannelCreateResponse)
async def create_stream_channel(
    request: StreamChannelCreateRequest,
    current_user: str = Depends(get_current_active_user),
):
    members = list(dict.fromkeys(request.members + [current_user]))
    client = get_stream_client()

    db = get_database()
    member_ids = [_parse_object_id(m) for m in members]
    user_docs = await db.users.find({"_id": {"$in": member_ids}}).to_list(length=len(members))
    if not user_docs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No users found for members")
    found_ids = {str(doc["_id"]) for doc in user_docs}
    missing = [m for m in members if m not in found_ids]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Users not found for members: {', '.join(missing)}",
        )
    for doc in user_docs:
        user_id = str(doc["_id"])
        client.upsert_user(build_stream_user(user_id, doc))

    data = dict(request.data or {})
    data["members"] = members
    if request.name:
        data["name"] = request.name
    if request.image:
        data["image"] = request.image

    channel = client.channel(request.channel_type, request.channel_id, data)
    channel.create(current_user)

    return {
        "channel_id": channel.id,
        "channel_type": request.channel_type,
        "cid": channel.cid,
    }
