"""
Metrics service for batching views, likes, and comments updates

app/services/metrics_service.py

"""
from typing import Dict, Optional, Set
from datetime import datetime, timedelta
import asyncio
from collections import defaultdict
from app.core.database import get_database
from app.core.config import settings
from bson import ObjectId
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError
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
                pending = self._take_video_unlocked(video_id)
            # Or if total updates exceed batch size
            elif self.total_updates >= self.batch_size:
                pending = self._take_all_unlocked()
            else:
                pending = None
        # The DB write happens outside the lock so other requests aren't blocked.
        await self._write_pending(pending)

    async def increment_like(self, video_id: str):
        """Increment like count for a video"""
        async with self._lock:
            self.likes_buffer[video_id] += 1
            self.total_updates += 1

            if self.likes_buffer[video_id] >= self.metrics_threshold["likes"]:
                pending = self._take_video_unlocked(video_id)
            elif self.total_updates >= self.batch_size:
                pending = self._take_all_unlocked()
            else:
                pending = None
        await self._write_pending(pending)

    async def decrement_like(self, video_id: str):
        """Decrement like count for a video"""
        async with self._lock:
            self.likes_buffer[video_id] -= 1
            self.total_updates += 1

            # For decrements, flush immediately if buffer would go negative
            if self.likes_buffer[video_id] < -5:  # Allow small negative buffer
                pending = self._take_video_unlocked(video_id)
            else:
                pending = None
        await self._write_pending(pending)

    async def increment_comment(self, video_id: str):
        """Increment comment count for a video"""
        async with self._lock:
            self.comments_buffer[video_id] += 1
            self.total_updates += 1

            if self.comments_buffer[video_id] >= self.metrics_threshold["comments"]:
                pending = self._take_video_unlocked(video_id)
            elif self.total_updates >= self.batch_size:
                pending = self._take_all_unlocked()
            else:
                pending = None
        await self._write_pending(pending)

    async def get_user_videos_like_count(self, user_id: str) -> int:
        """Get total likes count across all videos of a user"""
        try:
            db = get_database()
            pipeline = [
                {"$match": {"creator_id": ObjectId(user_id)}},
                {"$group": {"_id": None, "total_likes": {"$sum": "$likes_count"}}}
            ]
            result = await db.videos.aggregate(pipeline).to_list(length=1)
            if result:
                return result[0].get("total_likes", 0)
            return 0
        except Exception as e:
            logger.error(f"Error fetching user videos like count for user {user_id}: {e}")
            return 0

    # ------------------------------------------------------------ flushing
    #
    # Flushing is two steps: under the lock the pending counts are taken out
    # of the buffers; the lock is then released and they are written to the
    # DB. If the write fails the counts are added back (under the lock), so a
    # DB outage delays counts instead of losing them.

    _FIELDS = (
        ("views_count", "views_buffer"),
        ("likes_count", "likes_buffer"),
        ("comments_count", "comments_buffer"),
    )

    def _take_video_unlocked(self, video_id: str) -> Dict[str, Dict[str, int]]:
        """Remove one video's pending counts from the buffers. Caller holds self._lock."""
        update_doc = {}
        for field, attr in self._FIELDS:
            value = getattr(self, attr).pop(video_id, 0)
            if value:
                update_doc[field] = value
        return {video_id: update_doc} if update_doc else {}

    def _take_all_unlocked(self) -> Dict[str, Dict[str, int]]:
        """Remove every pending count from the buffers. Caller holds self._lock."""
        pending: Dict[str, Dict[str, int]] = {}
        for field, attr in self._FIELDS:
            buffer = getattr(self, attr)
            for video_id, value in buffer.items():
                if value:
                    pending.setdefault(video_id, {})[field] = value
            # clear() in place: other modules read these dicts by attribute.
            buffer.clear()
        self.total_updates = 0
        self.last_flush = datetime.utcnow()
        return pending

    async def _restore(self, pending: Dict[str, Dict[str, int]]):
        """Add counts that failed to reach the DB back into the buffers."""
        if not pending:
            return
        async with self._lock:
            for video_id, update_doc in pending.items():
                for field, attr in self._FIELDS:
                    value = update_doc.get(field)
                    if value:
                        getattr(self, attr)[video_id] += value
            # total_updates is not bumped back up: during a DB outage that
            # would make every following increment retry the write. The
            # periodic flush (or the per-video thresholds) retries instead.

    async def _write_pending(self, pending: Optional[Dict[str, Dict[str, int]]]):
        """Write taken counts to the DB (lock NOT held); restore them on failure."""
        if not pending:
            return

        operations = []
        op_video_ids = []
        for video_id, update_doc in pending.items():
            if not ObjectId.is_valid(video_id):
                # Never matches a video; keeping it would retry forever.
                logger.warning(f"Dropping buffered metrics for invalid video id {video_id!r}: {update_doc}")
                continue
            operations.append(({"_id": ObjectId(video_id)}, {"$inc": update_doc}))
            op_video_ids.append(video_id)

        if not operations:
            return

        db = get_database()
        try:
            if len(operations) == 1:
                await db.videos.update_one(*operations[0])
                logger.debug(f"Flushed metrics for video {op_video_ids[0]}: {pending[op_video_ids[0]]}")
            else:
                await db.videos.bulk_write([UpdateOne(f, u) for f, u in operations], ordered=True)
                logger.info(f"Flushed {len(operations)} video metrics updates")
        except BulkWriteError as e:
            # Ordered bulk write: everything before the first failed op was applied.
            errors = (e.details or {}).get("writeErrors") or []
            first_failed = min((err.get("index", 0) for err in errors), default=0)
            failed_ids = op_video_ids[first_failed:]
            logger.error(f"Error in bulk flush ({len(failed_ids)} updates kept in buffer): {e}")
            await self._restore({vid: pending[vid] for vid in failed_ids})
        except Exception as e:
            logger.error(f"Error flushing metrics ({len(operations)} updates kept in buffer): {e}")
            await self._restore({vid: pending[vid] for vid in op_video_ids})

    async def _flush_video_metrics(self, video_id: str):
        """Flush metrics for a specific video (acquires lock)"""
        async with self._lock:
            pending = self._take_video_unlocked(video_id)
        await self._write_pending(pending)

    async def flush_all(self):
        """Flush all buffered metrics to database (acquires lock only to take the counts)"""
        async with self._lock:
            pending = self._take_all_unlocked()
        await self._write_pending(pending)

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