"""
app/models/video_editor.py

"""




from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict
from uuid import UUID, uuid4

class VideoModelForEditor(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={
            UUID: str,
            datetime: lambda v: v.isoformat()
        }
    )

    id: Optional[UUID] = Field(default_factory=uuid4, alias="_id")
    FEid: Optional[str] = None
    duration: float
    start: float
    end: float
    remoteUrl: str
    type: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    is_active: bool = Field(default=True)
