from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.database import get_database

router = APIRouter()


class VersionResponse(BaseModel):
    appVersionNumber: str
    appVersionIos: str | None = None
    appVersionAndroid: str | None = None
    showReviewAndroid: bool = False
    showReviewIos: bool = False
    showUpdateAndroid: bool = False
    showUpdateIos: bool = False


@router.get("/version", response_model=VersionResponse)
async def get_app_version():
    """Return the current app version from the config collection."""
    db = get_database()

    doc = await db.config.find_one(
        {},
        projection={
            "appVersionNumber": 1,
            "appVersionIos": 1,
            "appVersionAndroid": 1,
            "showReviewAndroid": 1,
            "showReviewIos": 1,
            "showUpdateAndroid": 1,
            "showUpdateIos": 1,
        },
        sort=[("_id", -1)],
    )

    if not doc or "appVersionNumber" not in doc:
        raise HTTPException(status_code=404, detail="App version not found")

    raw_version = doc.get("appVersionNumber")
    normalized_version = raw_version.strip('"') if isinstance(raw_version, str) else str(raw_version)

    return VersionResponse(
        appVersionNumber=normalized_version,
        appVersionIos=doc.get("appVersionIos"),
        appVersionAndroid=doc.get("appVersionAndroid"),
        showReviewAndroid=doc.get("showReviewAndroid", False),
        showReviewIos=doc.get("showReviewIos", False),
        showUpdateAndroid=doc.get("showUpdateAndroid", False),
        showUpdateIos=doc.get("showUpdateIos", False),
    )


# Switches for new app features, set in the config document as
# {"features": {"points": true, ...}}. Anything missing, unknown or not exactly
# true is off, so an app release can ship the screens before they're turned on.
APP_FEATURES = ("points", "boost", "earnings", "replies", "challenges", "sounds", "city", "leaderboard",
                "badges", "tv_series", "tv_play", "hashtags", "milestones", "home_ground",
                "picks", "missions", "creator_stats", "trial_posts", "mentions", "ai_label", "feed_fx", "messages",
                "profile_plus", "tv_plus",
                "perf", "small_images", "instant_feed",
                "gzip_api", "msg_lite", "clear_icons",
                "more_categories", "content_guide", "tv_strip",
                "quick_posts", "age_check", "whats_new", "signup_names",
                "phone_signup", "code_login", "msg_plus", "msg_voice", "flyers",
                "wide_fill", "quiet_counts", "follow_plus", "dark_nav", "photo_zoom", "photo_full", "places", "upload_retry",
                "recombee_fast", "recombee_tune", "recombee_eu",
                "lean_preload", "loop_cap", "data_saver", "fast_start", "quick_replies",
                "tv_age_gate", "tv_loves", "tv_coming_soon", "tv_clips", "tv_downloads", "tv_pip", "tv_player_plus",
                "comment_guard", "challenge_fair", "teen_comments", "comment_plus", "brand_links", "reaction_country", "upload_locked", "share_play", "open_after_install", "post_limits",
                # Nov 2026 (server first; the app parts need 2.3.60)
                "safe_ban", "warnings", "staff_view", "staff_quiet", "mod_tools", "reserved_names",
                "wano_team", "whats_new_plus", "attention_cap", "challenge_plus",
                # Nov 2026, app 2.3.61
                "quick_launch", "push_ask", "comeback",
                # Nov 2026, app 2.3.62
                "wa_share", "birthday_needed", "birthday_required", "birthday_unknown_teen", "recombee_detail")


@router.get("/features")
async def get_app_features():
    db = get_database()
    doc = await db.config.find_one({"features": {"$type": "object"}}, projection={"features": 1},
                                   sort=[("_id", -1)]) or {}
    on = doc.get("features") if isinstance(doc.get("features"), dict) else {}
    out = {k: on.get(k) is True for k in APP_FEATURES}
    # A switch that needs another one is off to the apps while that one is off
    # (comment_plus without comment_guard would break signed-out reading).
    from app.services.features import NEEDS
    for k, needed in NEEDS.items():
        if out.get(k) and not out.get(needed):
            out[k] = False
    return out
