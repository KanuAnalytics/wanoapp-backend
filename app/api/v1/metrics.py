"""
Metrics management endpoints

app/api/v1/metrics.py

"""
from fastapi import APIRouter, Depends, Query
from app.api.deps import require_admin
from app.services.metrics_service import metrics_buffer

router = APIRouter()  # Make sure this line exists!

@router.post("/flush")
async def flush_metrics(
    current_user: str = Depends(require_admin)
):
    """Manually flush all buffered metrics (admin only)"""
    await metrics_buffer.flush_all()
    return {"message": "Metrics flushed successfully"}

@router.get("/buffer-status")
async def get_buffer_status(
    current_user: str = Depends(require_admin)
):
    """Get current buffer status (admin only)"""
    # Get some statistics about the buffer
    total_videos = len(set(
        list(metrics_buffer.views_buffer.keys()) +
        list(metrics_buffer.likes_buffer.keys()) +
        list(metrics_buffer.comments_buffer.keys())
    ))
    
    return {
        "total_videos_in_buffer": total_videos,
        "total_updates_pending": metrics_buffer.total_updates,
        "last_flush": metrics_buffer.last_flush.isoformat(),
        "views_pending": sum(metrics_buffer.views_buffer.values()),
        "likes_pending": sum(metrics_buffer.likes_buffer.values()),
        "comments_pending": sum(metrics_buffer.comments_buffer.values()),
        "flush_interval": metrics_buffer.flush_interval,
        "batch_size": metrics_buffer.batch_size,
        "thresholds": metrics_buffer.metrics_threshold
    }

@router.get("/for-you")
async def for_you_report(
    days: int = Query(7, ge=1, le=90),
    current_user: str = Depends(require_admin),
):
    """For You A/B report per variant and per source: impressions, how many were
    watched, average share watched, completion (>= 90%) and skip (< 20%) rates."""
    from app.services import for_you
    from app.core.database import get_database
    return await for_you.report(get_database(), days)
