"""

app/models/_init_.py

"""


from app.models.base import *
from app.models.user import *
from app.models.video import *
from app.models.music import *
from app.models.engagement import *
from app.models.advertising import *
from app.models.analytics import *

__all__ = [
    # Base
    "PyObjectId",
    "UserType",
    "VideoType",
    "VideoOrientation",
    "VideoPrivacy",
    "AfricanRegion",
    "ReportReason",
    "CampaignStatus",
    "NotificationType",
    "BaseDocument",
    "LocalizationPreferences",
    "ThemeCustomization",
    
    # User models
    "UserBase",
    "StandardUser",
    "ArtistUser",
    "AdvertiserUser",
    "AdminUser",
    "AdvertiserMetadata",
    "AdminPermissions",
    
    # Video models
    "Video",
    "VideoDraft",
    "VideoMetadata",
    "VideoUrls",
    
    # Music models
    "Music",
    
    # Engagement models
    "Comment",
    "VideoReport",
    "Notification",
    
    # Advertising models
    "AdTargeting",
    "AdCampaign",
    
    # Analytics models
    "VideoAnalytics",
    "UserAnalytics",
    "UserSession",
]