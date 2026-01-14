"""
Stream Chat client helpers

app/services/stream_chat.py
"""
from stream_chat import StreamChat
from app.core.config import settings


_client: StreamChat | None = None


def get_stream_client() -> StreamChat:
    global _client
    if _client is None:
        _client = StreamChat(
            api_key=settings.STREAM_API_KEY,
            api_secret=settings.STREAM_API_SECRET,
        )
    return _client


def build_stream_user(user_id: str, user_doc: dict) -> dict:
    payload = {"id": user_id}
    name = user_doc.get("display_name") or user_doc.get("username")
    if name:
        payload["name"] = name
    image = user_doc.get("profile_picture")
    if image:
        payload["image"] = image
    return payload
