"""
app/models/base.py
"""


from datetime import datetime
from typing import Optional
from enum import Enum
from pydantic import BaseModel, Field, ConfigDict
from bson import ObjectId

# Simple PyObjectId for Pydantic v2
# We'll just use string type and handle ObjectId conversion in the database layer
PyObjectId = str

# Enums
class UserType(str, Enum):
    STANDARD = "standard"
    ARTIST = "artist"
    ADVERTISER = "advertiser"
    ADMIN = "admin"

class VideoType(str, Enum):
    REGULAR = "regular"
    BITS = "bits"

class VideoOrientation(str, Enum):
    HORIZONTAL = "horizontal"  # 16:9
    VERTICAL = "vertical"      # 9:16

class VideoPrivacy(str, Enum):
    PUBLIC = "public"
    UNLISTED = "unlisted"
    PRIVATE = "private"

class AfricanRegion(str, Enum):
    WEST = "west_africa"
    EAST = "east_africa"
    CENTRAL = "central_africa"
    SOUTH = "south_africa"

class ReportReason(str, Enum):
    INAPPROPRIATE_CONTENT = "inappropriate_content"
    SPAM = "spam"
    HARASSMENT = "harassment"
    COPYRIGHT = "copyright"
    MISINFORMATION = "misinformation"
    OTHER = "other"


class ReportCategory(str, Enum):
    """Extended report categories for more specific violation types"""
    VIOLENCE = "violence"
    HATE_SPEECH = "hate_speech"
    NUDITY = "nudity"
    SUICIDE_SELF_HARM = "suicide_self_harm"
    FALSE_INFORMATION = "false_information"
    BULLYING = "bullying"
    TERRORISM = "terrorism"
    CHILD_EXPLOITATION = "child_exploitation"
    DANGEROUS_ACTS = "dangerous_acts"
    REGULATED_GOODS = "regulated_goods"
    INTELLECTUAL_PROPERTY = "intellectual_property"
    SPAM = "spam"
    OTHER = "other"

class CampaignStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

class NotificationType(str, Enum):
    LIKE = "like"
    COMMENT = "comment"
    FOLLOW = "follow"
    VIDEO_UPLOADED = "video_uploaded"
    MENTION = "mention"
    SYSTEM = "system"

# Base Models
class BaseDocument(BaseModel):
    """Base model for all documents with common fields"""
    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={
            ObjectId: str,
            datetime: lambda v: v.isoformat()
        }
    )
    
    id: Optional[str] = Field(default=None, alias="_id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    is_active: bool = Field(default=True)

# Localization Models
class LocalizationPreferences(BaseModel):
    """User localization preferences"""
    country: str = Field(..., min_length=2, max_length=2, description="ISO 3166-1 alpha-2 country code")
    languages: list[str] = Field(default=["en"], description="List of languages with primary first")
    tribes: Optional[list[str]] = Field(default=None, description="List of tribes with primary first")
    region: Optional[AfricanRegion] = None

class ThemeCustomization(BaseModel):
    """Theme customization based on region"""
    primary_color: str
    secondary_color: str
    country_flag_icon: Optional[str] = None
    gradient_png: Optional[str] = None