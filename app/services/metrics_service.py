"""
Metrics service for batching views, likes, and comments updates

app/services/metrics_service.py

"""
from typing import Dict, Set
from datetime import datetime, timedelta
import asyncio
from collections import defaultdict
from app.core.database import get_database
from app.core.config import settings
from bson import ObjectId
from pymongo import UpdateOne
import logging

logger = logging.getLogger(__name__)


class MetricsBuffer:
    """
    In-memory buffer for metrics that periodically flushes to database
    """
    def __init__(
        self,
        flush_interval: int = None,
        batch_size: int = None,
        metrics_threshold: Dict[str, int] = None
    ):
        self.flush_interval = flush_interval or settings.METRICS_FLUSH_INTERVAL
        self.batch_size = batch_size or settings.METRICS_BATCH_SIZE
        
        # Default thresholds for flushing
        self.metrics_threshold = metrics_threshold or {
            "views": settings.METRICS_VIEW_THRESHOLD,
            "likes": settings.METRICS_LIKE_THRESHOLD,
            "comments": settings.METRICS_COMMENT_THRESHOLD,
        }
        
        # Buffers for each metric type
        self.views_buffer: Dict[str, int] = defaultdict(int)
        self.likes_buffer: Dict[str, int] = defaultdict(int)
        self.comments_buffer: Dict[str, int] = defaultdict(int)
        
        # Track total updates to trigger batch flush
        self.total_updates = 0
        self.last_flush = datetime.utcnow()
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
        
        # Start background flush task
        self._flush_task = None

    async def start(self):
        """Start the background flush task"""
        self._flush_task = asyncio.create_task(self._periodic_flush())
        logger.info(f"MetricsBuffer started with flush_interval={self.flush_interval}s")

    async def stop(self):
        """Stop the background flush task"""
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            await self.flush_all()  # Final flush
        logger.info("MetricsBuffer stopped")

    async def increment_view(self, video_id: str):
        """Increment view count for a video"""
        async with self._lock:
            self.views_buffer[video_id] += 1
            self.total_updates += 1
            
            # Check if we should flush this specific video
            if self.views_buffer[video_id] >= self.metrics_threshold["views"]:
                await self._flush_video_metrics(video_id)
            # Or if total updates exceed batch size
            elif self.total_updates >= self.batch_size:
                await self.flush_all()

    async def increment_like(self, video_id: str):
        """Increment like count for a video"""
        async with self._lock:
            self.likes_buffer[video_id] += 1
            self.total_updates += 1
            
            if self.likes_buffer[video_id] >= self.metrics_threshold["likes"]:
                await self._flush_video_metrics(video_id)
            elif self.total_updates >= self.batch_size:
                await self.flush_all()

    async def decrement_like(self, video_id: str):
        """Decrement like count for a video"""
        async with self._lock:
            self.likes_buffer[video_id] -= 1
            self.total_updates += 1
            
            # For decrements, flush immediately if buffer would go negative
            if self.likes_buffer[video_id] < -5:  # Allow small negative buffer
                await self._flush_video_metrics(video_id)

    async def increment_comment(self, video_id: str):
        """Increment comment count for a video"""
        async with self._lock:
            self.comments_buffer[video_id] += 1
            self.total_updates += 1
            
            if self.comments_buffer[video_id] >= self.metrics_threshold["comments"]:
                await self._flush_video_metrics(video_id)
            elif self.total_updates >= self.batch_size:
                await self.flush_all()

    async def _flush_video_metrics(self, video_id: str):
        """Flush metrics for a specific video"""
        db = get_database()
        
        update_doc = {}
        
        # Collect all updates for this video
        if video_id in self.views_buffer and self.views_buffer[video_id] != 0:
            update_doc["views_count"] = self.views_buffer[video_id]
            self.views_buffer[video_id] = 0
            
        if video_id in self.likes_buffer and self.likes_buffer[video_id] != 0:
            update_doc["likes_count"] = self.likes_buffer[video_id]
            self.likes_buffer[video_id] = 0
            
        if video_id in self.comments_buffer and self.comments_buffer[video_id] != 0:
            update_doc["comments_count"] = self.comments_buffer[video_id]
            self.comments_buffer[video_id] = 0
        
        if update_doc:
            try:
                # Update database with increments
                await db.videos.update_one(
                    {"_id": ObjectId(video_id)},
                    {"$inc": update_doc}
                )
                logger.debug(f"Flushed metrics for video {video_id}: {update_doc}")
            except Exception as e:
                logger.error(f"Error flushing metrics for video {video_id}: {e}")

    async def flush_all(self):
        """Flush all buffered metrics to database"""
        async with self._lock:
            db = get_database()
            
            # Collect all video IDs that need updates
            video_ids = set()
            video_ids.update(self.views_buffer.keys())
            video_ids.update(self.likes_buffer.keys())
            video_ids.update(self.comments_buffer.keys())
            
            if not video_ids:
                return
            
            # Batch update operations
            bulk_operations = []
            
            for video_id in video_ids:
                update_doc = {}
                
                if video_id in self.views_buffer and self.views_buffer[video_id] != 0:
                    update_doc["views_count"] = self.views_buffer[video_id]
                    
                if video_id in self.likes_buffer and self.likes_buffer[video_id] != 0:
                    update_doc["likes_count"] = self.likes_buffer[video_id]
                    
                if video_id in self.comments_buffer and self.comments_buffer[video_id] != 0:
                    update_doc["comments_count"] = self.comments_buffer[video_id]
                
                if update_doc:
                    bulk_operations.append(
                        UpdateOne(
                            {"_id": ObjectId(video_id)},
                            {"$inc": update_doc}
                        )
                    )
            
            # Execute bulk update
            if bulk_operations:
                try:
                    result = await db.videos.bulk_write(bulk_operations)
                    logger.info(f"Flushed {len(bulk_operations)} video metrics updates")
                except Exception as e:
                    logger.error(f"Error in bulk flush: {e}")
            
            # Clear buffers
            self.views_buffer.clear()
            self.likes_buffer.clear()
            self.comments_buffer.clear()
            self.total_updates = 0
            self.last_flush = datetime.utcnow()

    async def _periodic_flush(self):
        """Background task to periodically flush metrics"""
        while True:
            try:
                await asyncio.sleep(self.flush_interval)
                
                # Check if enough time has passed since last flush
                if datetime.utcnow() - self.last_flush >= timedelta(seconds=self.flush_interval):
                    await self.flush_all()
                    
            except asyncio.CancelledError:
                logger.info("Periodic flush task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in periodic flush: {e}")

    async def get_buffered_counts(self, video_id: str) -> Dict[str, int]:
        """Get current buffered counts for a video (useful for immediate display)"""
        async with self._lock:
            return {
                "views": self.views_buffer.get(video_id, 0),
                "likes": self.likes_buffer.get(video_id, 0),
                "comments": self.comments_buffer.get(video_id, 0)
            }


# Global metrics buffer instance
metrics_buffer = MetricsBuffer()