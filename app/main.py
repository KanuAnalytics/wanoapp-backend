from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging

from app.core.config import settings
from app.core.database import connect_to_mongo, close_mongo_connection
from app.api.v1 import api_router  # This imports the router
from app.graphql.schema import graphql_app
from app.services.metrics_service import metrics_buffer
from app.routes import upload_video

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await connect_to_mongo()
    await metrics_buffer.start()  # Start the metrics buffer
    yield
    # Shutdown
    await metrics_buffer.stop()   # Stop the metrics buffer
    await close_mongo_connection()

# Create FastAPI app
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