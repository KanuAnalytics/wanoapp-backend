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
from app.api.v1.scheduled_posts import router as scheduled_posts_router
from app.api.v1.comments import router as comments_router
from app.api.v1.feed import router as feed_router
from app.api.v1.metrics import router as metrics_router
from app.api.v1.reports import router as reports_router
from app.api.v1.drafts import router as drafts_router
from app.api.v1.music import router as music_router
from app.api.v1.categories import router as categories_router
from app.api.v1.config import router as config_router
from app.api.v1.recombee import router as recombee_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.tv import router as tv_router
from app.api.v1.creators import router as creators_router
from app.api.v1.leaderboard import router as leaderboard_router
from app.api.v1.points import router as points_router
from app.api.v1.challenges import router as challenges_router
from app.api.v1.admin import router as admin_router
from app.api.v1.hashtags import router as hashtags_router
from app.api.v1.milestones import router as milestones_router
from app.api.v1.home_ground import router as home_ground_router
from app.api.v1.creator_push import router as creator_push_router



# Include all routers
api_router.include_router(auth_router, prefix="/auth", tags=["authentication"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
# Before videos_router, so "/videos/scheduled" isn't matched by "/videos/{video_id}".
api_router.include_router(scheduled_posts_router, prefix="/videos", tags=["scheduled posts"])
from app.api.v1.feed_fx import router as feed_fx_router  # noqa: E402
api_router.include_router(feed_fx_router, prefix="/videos", tags=["feed-fx"])
from app.api.v1.profile_plus import router as profile_plus_router  # noqa: E402
api_router.include_router(profile_plus_router, prefix="/users", tags=["profile"])
api_router.include_router(videos_router, prefix="/videos", tags=["videos"])
api_router.include_router(comments_router, prefix="", tags=["comments"])
api_router.include_router(feed_router, prefix="/feed", tags=["feed"])
api_router.include_router(metrics_router, prefix="/metrics", tags=["metrics"])
api_router.include_router(reports_router, prefix="/reports", tags=["reports"])
api_router.include_router(drafts_router, prefix="/drafts", tags=["drafts"])
api_router.include_router(music_router, prefix="/music", tags=["music"])
api_router.include_router(categories_router, prefix="/categories", tags=["categories"])
api_router.include_router(config_router, prefix="/config", tags=["config"])
api_router.include_router(recombee_router, prefix="/recombee", tags=["recombee"])
api_router.include_router(notifications_router, prefix="/notifications", tags=["notifications"])
from app.api.v1.perf import router as perf_router  # noqa: E402
api_router.include_router(perf_router, prefix="/perf", tags=["speed"])
from app.api.v1.tv_plus import router as tv_plus_router  # noqa: E402
api_router.include_router(tv_plus_router, prefix="/tv/plus", tags=["wano-tv"])
api_router.include_router(tv_router, prefix="/tv", tags=["wano-tv"])
api_router.include_router(creators_router, prefix="/creators", tags=["creators"])
api_router.include_router(leaderboard_router, prefix="/leaderboard", tags=["leaderboard"])
api_router.include_router(points_router, prefix="/points", tags=["points"])
api_router.include_router(challenges_router, prefix="/challenges", tags=["challenges"])
api_router.include_router(admin_router, prefix="/admin", tags=["admin"])
api_router.include_router(hashtags_router, prefix="/hashtags", tags=["hashtags"])
api_router.include_router(milestones_router, prefix="/milestones", tags=["milestones"])
api_router.include_router(home_ground_router, prefix="/home-ground", tags=["home-ground"])
api_router.include_router(creator_push_router, prefix="/creator", tags=["creator"])
from app.api.v1.messages import router as messages_router  # noqa: E402
api_router.include_router(messages_router, prefix="/messages", tags=["messages"])
from app.api.v1.quick_posts import router as quick_posts_router  # noqa: E402
api_router.include_router(quick_posts_router, prefix="/quick", tags=["quick posts"])
