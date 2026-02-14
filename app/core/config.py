"""

app/core/config.py

"""


from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional

class Settings(BaseSettings):
    # --- Doppler Configuration ---
    DOPPLER_CONFIG: str
    DOPPLER_ENVIRONMENT: str
    DOPPLER_PROJECT: str

    # --- Database Settings ---
    MONGODB_URI: str
    MONGODB_URL: str
    DATABASE_NAME: str

    # --- General App Settings ---
    APP_NAME: str = "WanoApp"
    DEBUG: bool = True
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 259200
    
    # --- URLs ---
    BACKEND_URL: str = "https://devbe.wanoafrica.com"
    FRONTEND_URL: str = "http://localhost:3000"
    
    # --- CORS ---
    CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:3001", 
        "https://devbe.wanoafrica.com",
        "https://wano-web.vercel.app"
    ]    

    # --- File & Metrics Settings ---
    MAX_UPLOAD_SIZE: int = 104857600  # 100MB
    REDIS_URL: str = "redis://localhost:6379/0"
    METRICS_FLUSH_INTERVAL: int = 30  # seconds
    METRICS_BATCH_SIZE: int = 100
    METRICS_VIEW_THRESHOLD: int = 50
    METRICS_LIKE_THRESHOLD: int = 20
    METRICS_COMMENT_THRESHOLD: int = 10

    # --- Email (SendGrid) ---
    SENDGRID_API_KEY: str
    SENDGRID_FROM_EMAIL: str
    EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS: int = 24

    # --- Twilio Settings ---
    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None
    TWILIO_WHATSAPP_FROM: Optional[str] = None
    TWILIO_WHATSAPP_CONTENT_SID: Optional[str] = None

    # --- DigitalOcean Spaces ---
    DO_SPACES_ACCESS_KEY_ID: str
    DO_SPACES_BUCKET_NAME: str
    DO_SPACES_ENDPOINT: str
    DO_SPACES_CDN_URL: str
    DO_SPACES_SECRET_KEY: str
    
    # --- Cloudflare ---
    CLOUDFLARE_ACCOUNT_ID: str
    CLOUDFLARE_STREAM_API_TOKEN: str
    CLOUDFLARE_STREAM_API_BASE: str
    
    # --- Video Service ---
    VIDEO_SERVICE_URL: str
    
    # --- AWS SQS ---
    AWS_REGION: str
    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    SQS_VIDEO_QUEUE_URL: str

    # --- Pydantic V2 Configuration ---
    # This replaces 'class Config' and handles your 'extra_forbidden' error
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",      # This tells Pydantic to ignore extra env vars
        case_sensitive=True
    )

# Instantiate the settings
settings = Settings()