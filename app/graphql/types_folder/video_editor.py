import strawberry
from typing import Optional
from datetime import datetime
from app.models.video_editor import VideoModelForEditor

@strawberry.type
class Video:
    id: str
    FEid: Optional[str]
    duration: float
    start: float
    end: float
    remoteUrl: str
    type: str
    created_at: datetime
    updated_at: datetime
    is_active: bool

    @classmethod
    def from_pydantic(cls, video: VideoModelForEditor) -> "Video":
        return cls(
            id=str(video.id),
            FEid=video.FEid,
            duration=video.duration,
            start=video.start,
            end=video.end,
            remoteUrl=video.remoteUrl,
            type=video.type,
            created_at=video.created_at,
            updated_at=video.updated_at,
            is_active=video.is_active
        )