"""
app/graphql/inputs/video_editor.py

"""


import strawberry
from typing import Optional

@strawberry.input
class VideoInput:
    FEid: str
    duration: float
    start: float
    end: float
    remoteUrl: str
    type: str
    index: Optional[int] = None
    isTrimmed: Optional[bool] = False

@strawberry.input
class CompileVideoInput:
    video: list[VideoInput]
    audio_url: Optional[str] = None
    ratio: Optional[str] = None
    videoType: Optional[str] = "public"
    description: Optional[str] = None
    thumbnail: Optional[str] = None
    categoryId: Optional[str] = None
    subcategoryId: Optional[str] = None
    