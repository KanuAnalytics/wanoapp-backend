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
        
        logger.info("Connected to MongoDB")
    except Exception as e:
        logger.error(f"Could not connect to MongoDB: {e}")
        raise

async def close_mongo_connection():
    """Close database connection."""
    if db.client:
        db.client.close()
        logger.info("Disconnected from MongoDB")

async def create_indexes():
    """Create database indexes for better performance"""
    try:
        # Video indexes
        await db.db.videos.create_index([("created_at", -1)])
        await db.db.videos.create_index([("creator_id", 1)])
        await db.db.videos.create_index([("is_active", 1), ("privacy", 1)])
        
        # User indexes
        await db.db.users.create_index([("username", 1)], unique=True)
        await db.db.users.create_index([("email", 1)], unique=True)
        
        # Comment indexes
        await db.db.comments.create_index([("video_id", 1)])
        await db.db.comments.create_index([("user_id", 1)])
        
        logger.info("Database indexes created")
    except Exception as e:
        logger.error(f"Error creating indexes: {e}")

def get_database():
    """Get database instance"""
    return db.db