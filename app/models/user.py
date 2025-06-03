"""
app/models/user.py

"""


from typing import Optional, List, Dict, Any, Literal
from datetime import datetime
from pydantic import BaseModel, Field, EmailStr, HttpUrl, validator
from app.models.base import (
    BaseDocument, 
    UserType, 
    LocalizationPreferences, 
    ThemeCustomization, 
    PyObjectId,
    AfricanRegion  # Add this import
)

class UserBase(BaseModel):
    """Base user model with common fields"""
    username: str = Field(..., min_length=3, max_length=30, pattern="^[a-zA-Z0-9_]+$")
    email: EmailStr
    display_name: str = Field(..., min_length=1, max_length=100)
    bio: Optional[str] = Field(None, max_length=500)
    profile_picture: Optional[HttpUrl] = None
    cover_picture: Optional[HttpUrl] = None
    localization: LocalizationPreferences
    theme: Optional[ThemeCustomization] = None
    
    # Verification fields
    is_verified: bool = Field(default=False)
    verification_token: Optional[str] = None
    verification_token_expires: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    
    # Feature flags
    features: Dict[str, Any] = Field(default_factory=dict)
    
    # Limits based on user type
    video_upload_limit: int = Field(default=90, ge=15, le=600)
    can_upload_music: bool = Field(default=False)
    can_create_ads: bool = Field(default=False)
    
    # Stats
    followers_count: int = Field(default=0, ge=0)
    following_count: int = Field(default=0, ge=0)
    videos_count: int = Field(default=0, ge=0)
    likes_count: int = Field(default=0, ge=0)
    
    # Add the property here
    @property
    def can_upload_videos(self) -> bool:
        return self.is_verified

class StandardUser(UserBase, BaseDocument):
    """Standard user model"""
    user_type: Literal[UserType.STANDARD] = UserType.STANDARD
    bookmarked_videos: List[PyObjectId] = Field(default_factory=list)
    liked_videos: List[PyObjectId] = Field(default_factory=list)
    following: List[PyObjectId] = Field(default_factory=list)
    followers: List[PyObjectId] = Field(default_factory=list)
    password_hash: str

class ArtistUser(StandardUser):
    """Artist user model with music upload capabilities"""
    user_type: Literal[UserType.ARTIST] = UserType.ARTIST
    can_upload_music: bool = Field(default=True)
    verified_artist: bool = Field(default=False)
    music_uploads: List[PyObjectId] = Field(default_factory=list)
    artist_metadata: Dict[str, Any] = Field(default_factory=dict)

class AdvertiserMetadata(BaseModel):
    """Advertiser specific metadata"""
    company_name: str = Field(..., min_length=1, max_length=200)
    company_contact_name: str = Field(..., min_length=1, max_length=100)
    company_contact_phone: str = Field(..., pattern="^[+]?[0-9]{10,15}$")
    business_verified: bool = Field(default=False)
    payment_method_id: Optional[str] = None
    billing_address: Optional[Dict[str, str]] = None

class AdvertiserUser(StandardUser):
    """Advertiser user model"""
    user_type: Literal[UserType.ADVERTISER] = UserType.ADVERTISER
    can_create_ads: bool = Field(default=True)
    advertiser_metadata: AdvertiserMetadata
    campaigns: List[PyObjectId] = Field(default_factory=list)
    total_spend: float = Field(default=0.0, ge=0)
    credit_balance: float = Field(default=0.0, ge=0)

class AdminPermissions(BaseModel):
    """Admin permissions model"""
    can_manage_users: bool = Field(default=True)
    can_manage_content: bool = Field(default=True)
    can_manage_ads: bool = Field(default=True)
    can_access_analytics: bool = Field(default=True)
    can_manage_payments: bool = Field(default=True)
    custom_permissions: Dict[str, bool] = Field(default_factory=dict)

class AdminUser(UserBase, BaseDocument):
    """Admin user model"""
    user_type: Literal[UserType.ADMIN] = UserType.ADMIN
    permissions: AdminPermissions
    admin_level: int = Field(default=1, ge=1, le=5)
    managed_regions: List[AfricanRegion] = Field(default_factory=list)
    password_hash: str