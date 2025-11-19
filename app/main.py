"""
app/main.py

"""


from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from app.core.config import settings
from app.core.database import connect_to_mongo, close_mongo_connection
from app.api.v1 import api_router  # This imports the router
from app.graphql.schema import graphql_app
from app.services.metrics_service import metrics_buffer
from app.routes import upload_video
from collections.abc import Mapping



# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Helper to ensure indexes idempotently
async def ensure_indexes(db):
    """Create indexes idempotently, avoiding name/spec conflicts."""
    try:
        users_info_list = [idx async for idx in db.users.list_indexes()]
        videos_info_list = [idx async for idx in db.videos.list_indexes()]
        users_info = {idx.get("name"): idx for idx in users_info_list}
        videos_info = {idx.get("name"): idx for idx in videos_info_list}

        def _spec_to_tuple(spec):
            # Accepts Mapping (dict/SON), list of pairs, or tuple of pairs
            if isinstance(spec, Mapping):
                return tuple(spec.items())
            if isinstance(spec, list):
                return tuple(tuple(p) for p in spec)
            if isinstance(spec, tuple):
                return tuple(tuple(p) for p in spec)
            return tuple()

        def _has_index_with_keys(info: dict, key_list):
            target = list(key_list)
            for name, meta in info.items():
                if _spec_to_tuple(meta.get("key", {})) == _spec_to_tuple(target):
                    return True, name, meta
            return False, None, None

        # Videos compound index (description + created_at)
        video_keys = [("description", 1), ("created_at", -1)]
        exists, existing_name, existing_meta = _has_index_with_keys(videos_info, video_keys)
        if not exists:
            await db.videos.create_index(video_keys, name="videos_description_created_at")
        else:
            logger.info(f"Videos index already exists as '{existing_name}': {existing_meta.get('key')}")

        # Users: username (unique)
        username_keys = [("username", 1)]
        exists, existing_name, existing_meta = _has_index_with_keys(users_info, username_keys)
        if not exists:
            await db.users.create_index(username_keys, name="users_username_unique", unique=True)
        else:
            # If an index exists but uniqueness differs, we won't alter it at startup to avoid disruptive ops.
            if existing_meta.get("unique") is True:
                logger.info(f"Users username index already exists (unique) as '{existing_name}'.")
            else:
                logger.info(f"Users username index already exists as '{existing_name}' (non-unique). Skipping change.")

        # Users: display_name (non-unique)
        display_keys = [("display_name", 1)]
        exists, existing_name, _ = _has_index_with_keys(users_info, display_keys)
        if not exists:
            await db.users.create_index(display_keys, name="users_display_name")
        else:
            logger.info(f"Users display_name index already exists as '{existing_name}'.")

    except Exception as e:
        logger.warning(f"Index creation encountered an unexpected issue: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await connect_to_mongo()
    # Ensure indexes
    db = AsyncIOMotorClient(settings.MONGODB_URI)[settings.DATABASE_NAME]
    await ensure_indexes(db)
    await metrics_buffer.start()  # Start the metrics buffer
    yield
    # Shutdown
    await metrics_buffer.stop()   # Stop the metrics buffer
    await close_mongo_connection()

# Create FastAPI app.
app = FastAPI(
    title=settings.APP_NAME,
    lifespan=lifespan,
    debug=settings.DEBUG
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include the API router - THIS IS WHAT'S MISSING!
app.include_router(api_router, prefix="/api/v1")

app.include_router(upload_video.router)

# Include GraphQL endpoint
app.include_router(graphql_app, prefix="/graphql")

@app.get("/")
async def root():
    return {"message": "Welcome to WanoApp API"}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}