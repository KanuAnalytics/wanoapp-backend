"""

app/core/config.py

"""


from pydantic_settings import BaseSettings
from typing import List
import os

class Settings(BaseSettings):
    # App
    APP_NAME: str = "WanoApp"
    DEBUG: bool = True
    SECRET_KEY: str
    
    # Database
    MONGODB_URL: str
    DATABASE_NAME: str
    
    # CORS
    CORS_ORIGINS: List[str] = ["*"]
    
    # Auth
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    ALGORITHM: str = "HS256"
    
    # File Upload
    MAX_UPLOAD_SIZE: int = 104857600  # 100MB
    
    # Redis (optional)
    REDIS_URL: str = "redis://localhost:6379/0"
    
    # Metrics buffer settings
    METRICS_FLUSH_INTERVAL: int = 30  # seconds
    METRICS_BATCH_SIZE: int = 100
    METRICS_VIEW_THRESHOLD: int = 50
    METRICS_LIKE_THRESHOLD: int = 20
    METRICS_COMMENT_THRESHOLD: int = 10

    # Email settings
    SENDGRID_API_KEY: str
    SENDGRID_FROM_EMAIL: str
    FRONTEND_URL: str = "http://localhost:3000"  # For verification link
    EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS: int = 24


    # DigitalOcean Spaces
    DO_SPACES_ACCESS_KEY_ID: str
    DO_SPACES_BUCKET_NAME: str
    DO_SPACES_ENDPOINT: str
    DO_SPACES_SECRET_KEY: str
    
    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()