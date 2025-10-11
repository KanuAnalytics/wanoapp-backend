"""
API v1 routers

app/api/v1/_init_.py
"""
from fastapi import APIRouter

# Create the main API router
api_router = APIRouter()

# Import individual routers
from app.api.v1.auth import router as auth_router
from app.api.v1.users import router as users_router
from app.api.v1.videos import router as videos_router
from app.api.v1.comments import router as comments_router
from app.api.v1.feed import router as feed_router
from app.api.v1.metrics import router as metrics_router
from app.api.v1.reports import router as reports_router
from app.api.v1.drafts import router as drafts_router
from app.api.v1.music import router as music_router
from app.api.v1.categories import router as categories_router



# Include all routers
api_router.include_router(auth_router, prefix="/auth", tags=["authentication"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(videos_router, prefix="/videos", tags=["videos"])
api_router.include_router(comments_router, prefix="", tags=["comments"])
api_router.include_router(feed_router, prefix="/feed", tags=["feed"])
api_router.include_router(metrics_router, prefix="/metrics", tags=["metrics"])
api_router.include_router(reports_router, prefix="/reports", tags=["reports"])
api_router.include_router(drafts_router, prefix="/drafts", tags=["drafts"])
api_router.include_router(music_router, prefix="/music", tags=["music"])
api_router.include_router(categories_router, prefix="/categories", tags=["categories"])
