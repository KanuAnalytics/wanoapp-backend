"""

app/core/database.py

"""


from motor.motor_asyncio import AsyncIOMotorClient
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

class Database:
    client: AsyncIOMotorClient = None
    db = None

db = Database()

async def connect_to_mongo():
    """Create database connection."""
    try:
        db.client = AsyncIOMotorClient(settings.MONGODB_URL)
        db.db = db.client[settings.DATABASE_NAME]
        
        # Create indexes for better performance
        await create_indexes()
        
        await create_comment_indexes(db.db)
        
        logger.info("Connected to MongoDB")
    except Exception as e:
        logger.error(f"Could not connect to MongoDB: {e}")
        raise

async def close_mongo_connection():
    """Close database connection."""
    if db.client:
        db.client.close()
        logger.info("Disconnected from MongoDB")

async def _safe_create_index(collection, keys, **kwargs):
    """Create one index; log and carry on if it fails so a single bad index
    (e.g. an existing conflicting spec or duplicate data) doesn't stop the rest."""
    try:
        await collection.create_index(keys, **kwargs)
    except Exception as e:
        logger.error(f"Error creating index {keys} on {collection.name}: {e}")


async def create_indexes():
    """Create database indexes for better performance"""
    d = db.db
    specs = [
        # Video indexes
        (d.videos, [("created_at", -1)], {}),
        # Matches the v1 feed's sort, which pairs created_at with an _id tiebreaker;
        # the created_at-only index above can't supply that order on its own.
        (d.videos, [("created_at", -1), ("_id", -1)], {}),
        (d.videos, [("creator_id", 1)], {}),
        (d.videos, [("is_active", 1), ("privacy", 1)], {}),
        # Scheduled posts: the publisher's due-time scan, and each creator's own list.
        (d.videos, [("status", 1), ("scheduled_at", 1)], {}),
        (d.videos, [("creator_id", 1), ("status", 1), ("scheduled_at", 1)], {}),

        # Watch history: one doc per (user, video). Unique index doubles as
        # the upsert key; the second index supports a future "continue
        # watching" query and per-user history listing.
        (d.watch_history, [("user_id", 1), ("video_id", 1)], {"unique": True}),
        (d.watch_history, [("user_id", 1), ("watched_at", -1)], {}),
        # For You feed: what each viewer was shown (kept 30 days for the A/B report)
        (d.feed_impressions, [("viewer", 1), ("at", -1)], {}),
        (d.feed_impressions, [("viewer", 1), ("video_id", 1), ("at", -1)], {}),
        (d.feed_impressions, [("at", 1)], {"expireAfterSeconds": 30 * 24 * 3600}),
        # Trending: hourly engagement (kept 3 days) and once-per-6h view counting
        (d.video_stats_hourly, [("hour", 1)], {"expireAfterSeconds": 3 * 24 * 3600}),
        (d.video_stats_hourly, [("video_id", 1), ("hour", -1)], {}),
        (d.view_dedupe, [("at", 1)], {"expireAfterSeconds": 6 * 3600}),
        (d.engagement_once, [("at", 1)], {"expireAfterSeconds": 3 * 24 * 3600}),
        # For You sources: fresh / trending / following / stream webhook lookups
        (d.videos, [("is_active", 1), ("privacy", 1), ("created_at", -1)], {}),
        (d.videos, [("creator_id", 1), ("created_at", -1)], {}),
        (d.videos, [("stream_uid", 1)], {"partialFilterExpression": {"stream_uid": {"$type": "string"}}}),

        # Wano TV: the catalogue is listed by popularity; slug is the seed
        # script's upsert key. History is one doc per (user, title).
        (d.tv_titles, [("is_active", 1), ("popularity", -1)], {}),
        (d.tv_titles, [("slug", 1)], {"unique": True}),
        (d.tv_genres, [("name", 1)], {"unique": True}),
        (d.tv_watch_history, [("user_id", 1), ("title_id", 1)], {"unique": True}),
        (d.tv_watch_history, [("user_id", 1), ("watched_at", -1)], {}),

        # User indexes
        (d.users, [("username", 1)], {"unique": True}),
        # Partial so phone-only accounts (no email key, or a legacy null) don't
        # collide with each other on a single null value. If an older full
        # unique index named email_1 already exists, this logs a conflict;
        # drop the old index once so this one can be created.
        (d.users, [("email", 1)], {
            "unique": True,
            "name": "users_email_unique_partial",
            "partialFilterExpression": {"email": {"$type": "string"}},
        }),
        (d.users, [("phone_number", 1)], {
            "unique": True,
            "name": "users_phone_unique_partial",
            "partialFilterExpression": {"phone_number": {"$type": "string"}},
        }),

        # Comment indexes
        (d.comments, [("video_id", 1)], {}),
        (d.comments, [("user_id", 1)], {}),

        # Notification indexes
        (d.notifications, [("recipient_id", 1), ("date", -1)], {}),
        (d.notifications, [("type", 1), ("user_id", 1), ("post_id", 1)], {}),
        (d.notifications, [("comment_id", 1)], {}),

        # Stitch jobs: the Cloudflare webhook looks jobs up by the clip that just became ready.
        (d.pending_videos, [("status", 1), ("clips.stream_uid", 1)], {}),
        # ...and looks up the finished job when its stitched video becomes ready.
        (d.pending_videos, [("result.stream_uid", 1)], {}),
    ]
    for collection, keys, kwargs in specs:
        await _safe_create_index(collection, keys, **kwargs)
    logger.info("Database indexes created")

async def create_comment_indexes(db):
    """Create indexes for comments collection"""
    
    # Existing indexes
    await db.comments.create_index("video_id")
    await db.comments.create_index("user_id")
    await db.comments.create_index("parent_id")
    
    # New index for display name
    await db.comments.create_index("user_display_name")
    
    # Compound indexes for common queries
    await db.comments.create_index([
        ("video_id", 1),
        ("parent_id", 1),
        ("is_active", 1),
        ("created_at", -1)
    ])
    
    # Text index for search
    await db.comments.create_index([
        ("content", "text"),
        ("user_display_name", "text")
    ])
    
    # Index for user comments query
    await db.comments.create_index([
        ("user_id", 1),
        ("is_active", 1),
        ("created_at", -1)
    ])

def get_database():
    """Get database instance"""
    return db.db