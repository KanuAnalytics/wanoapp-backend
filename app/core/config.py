"""

app/core/config.py

"""


from pydantic_settings import BaseSettings
from typing import List, Optional
import os

class Settings(BaseSettings):

    #Doppler
    DOPPLER_CONFIG: str
    DOPPLER_ENVIRONMENT: str
    DOPPLER_PROJECT: str


    # Database
    MONGODB_URI: str
    # App
    APP_NAME: str = "WanoApp"
    DEBUG: bool = True
    SECRET_KEY: str
    
    # Database
    MONGODB_URL: str
    DATABASE_NAME: str
    
    # CORS
    CORS_ORIGINS: List[str] = [
            "http://localhost:3000",
            "http://localhost:3001", 
            "https://devbe.wanoafrica.com",
            "https://wano-web.vercel.app"
            # Add any other origins you need
        ]    
    # Auth
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 259200
    ALGORITHM: str = "HS256"
    
    # File Upload
    MAX_UPLOAD_SIZE: int = 104857600  # 100MB


    BACKEND_URL: str = "https://devbe.wanoafrica.com"
    FRONTEND_URL: str = "http://localhost:3000"
    
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

    # Twilio settings
    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None
    TWILIO_VERIFY_SERVICE_SID: Optional[str] = None
    TWILIO_WHATSAPP_FROM: Optional[str] = None
    TWILIO_WHATSAPP_CONTENT_SID: Optional[str] = None


    # DigitalOcean Spaces
    DO_SPACES_ACCESS_KEY_ID: str
    DO_SPACES_BUCKET_NAME: str
    DO_SPACES_ENDPOINT: str
    DO_SPACES_CDN_URL: str
    DO_SPACES_SECRET_KEY: str
    
    #service URL
    VIDEO_SERVICE_URL: str
    
    class Config:
        env_file = ".env"
        case_sensitive = True
        extra="ignore"

settings = Settings()
