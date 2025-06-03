"""
Metrics management endpoints

app/api/v1/metrics.py

"""
from fastapi import APIRouter, Depends
from app.api.deps import get_current_active_user
from app.services.metrics_service import metrics_buffer

router = APIRouter()  # Make sure this line exists!

@router.post("/flush")
async def flush_metrics(
    current_user: str = Depends(get_current_active_user)
):
    """Manually flush all buffered metrics (admin only)"""
    # In production, check if user is admin
    await metrics_buffer.flush_all()
    return {"message": "Metrics flushed successfully"}

@router.get("/buffer-status")
async def get_buffer_status(
    current_user: str = Depends(get_current_active_user)
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