# setup.ps1 - WanoApp Backend Windows Setup Script

Write-Host "Setting up WanoApp Backend on Windows..." -ForegroundColor Green
Write-Host ""

# Create project structure
Write-Host "Creating project structure..." -ForegroundColor Yellow
$directories = @(
    "app",
    "app\models",
    "app\api",
    "app\api\v1",
    "app\core",
    "app\services",
    "app\utils",
    "app\graphql",
    "tests",
    "scripts",
    "docs"
)

foreach ($dir in $directories) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

# Create __init__.py files
Write-Host "Creating __init__.py files..." -ForegroundColor Yellow
$initFiles = @(
    "app\__init__.py",
    "app\models\__init__.py",
    "app\api\__init__.py",
    "app\api\v1\__init__.py",
    "app\core\__init__.py",
    "app\services\__init__.py",
    "app\utils\__init__.py",
    "app\graphql\__init__.py",
    "tests\__init__.py"
)

foreach ($file in $initFiles) {
    New-Item -ItemType File -Force -Path $file | Out-Null
}

# Create requirements.txt
Write-Host "Creating requirements.txt..." -ForegroundColor Yellow
$requirements = @'
# Core dependencies
fastapi==0.104.1
uvicorn[standard]==0.24.0
pydantic==2.5.0
pydantic[email]==2.5.0
python-dotenv==1.0.0

# MongoDB
motor==3.3.2
pymongo==4.6.0

# GraphQL
strawberry-graphql[fastapi]==0.217.1

# Authentication
python-jose[cryptography]==3.3.0
passlib[bcrypt]==1.7.4
python-multipart==0.0.6

# Storage & CDN
boto3==1.29.7

# Redis for caching
redis==5.0.1

# Image processing
pillow==10.1.0

# HTTP client
httpx==0.25.2

# Validation
email-validator==2.1.0
phonenumbers==8.13.26

# Utils
python-dateutil==2.8.2
pytz==2023.3
'@
$requirements | Out-File -Encoding UTF8 requirements.txt

# Create requirements-dev.txt
$requirementsDev = @'
# Include production requirements
-r requirements.txt

# Testing
pytest==7.4.3
pytest-asyncio==0.21.1
pytest-cov==4.1.0
pytest-mock==3.12.0

# Code quality
black==23.11.0
isort==5.13.0
flake8==6.1.0
mypy==1.7.1

# Development tools
ipython==8.18.1
'@
$requirementsDev | Out-File -Encoding UTF8 requirements-dev.txt

# Create .env.example
Write-Host "Creating .env.example..." -ForegroundColor Yellow
$envExample = @'
# Application
APP_NAME=WanoApp
DEBUG=True
SECRET_KEY=your-secret-key-here-change-in-production

# Database
MONGODB_URL=mongodb://localhost:27017
DATABASE_NAME=wanoapp

# Redis (optional for local dev)
REDIS_URL=redis://localhost:6379/0

# Authentication
ACCESS_TOKEN_EXPIRE_MINUTES=30
ALGORITHM=HS256

# File Upload
MAX_UPLOAD_SIZE=104857600

# CORS
CORS_ORIGINS=["http://localhost:3000", "http://localhost:8000"]
'@
$envExample | Out-File -Encoding UTF8 .env.example

# Create .gitignore
Write-Host "Creating .gitignore..." -ForegroundColor Yellow
$gitignore = @'
# Python
__pycache__/
*.py[cod]
*$py.class
.Python
venv/
env/
.venv

# IDE
.vscode/
.idea/
*.swp

# Environment
.env
.env.local

# Testing
.coverage
.pytest_cache/
htmlcov/

# Logs
*.log
logs/

# Uploads
uploads/
temp/

# Windows
Thumbs.db
Desktop.ini
'@
$gitignore | Out-File -Encoding UTF8 .gitignore

# Create app/core/config.py
Write-Host "Creating core configuration..." -ForegroundColor Yellow
$configPy = @'
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
    
    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
'@
$configPy | Out-File -Encoding UTF8 app\core\config.py

# Create app/core/database.py
$databasePy = @'
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
        logger.info("Connected to MongoDB")
    except Exception as e:
        logger.error(f"Could not connect to MongoDB: {e}")
        raise

async def close_mongo_connection():
    """Close database connection."""
    if db.client:
        db.client.close()
        logger.info("Disconnected from MongoDB")

def get_database():
    """Get database instance"""
    return db.db
'@
$databasePy | Out-File -Encoding UTF8 app\core\database.py

# Create app/main.py
Write-Host "Creating main application..." -ForegroundColor Yellow
$mainPy = @'
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging

from app.core.config import settings
from app.core.database import connect_to_mongo, close_mongo_connection

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await connect_to_mongo()
    yield
    # Shutdown
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

@app.get("/")
async def root():
    return {"message": "Welcome to WanoApp API"}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}
'@
$mainPy | Out-File -Encoding UTF8 app\main.py

# Create README.md
Write-Host "Creating README.md..." -ForegroundColor Yellow
$readme = @'
# WanoApp Backend

Backend API for WanoApp - A localized video sharing platform for African markets.

## Tech Stack
- FastAPI
- MongoDB
- GraphQL (Strawberry)
- Python 3.9+

## Quick Start (Windows)

1. Create virtual environment:
   ```powershell
   python -m venv venv
   .\venv\Scripts\activate
   ```

2. Install dependencies:
   ```powershell
   pip install -r requirements-dev.txt
   ```

3. Set up environment:
   ```powershell
   Copy-Item .env.example .env
   # Edit .env with your settings
   ```

4. Run MongoDB (using Docker):
   ```powershell
   docker run -d -p 27017:27017 --name wano-mongo mongo:5.0
   ```

5. Start the server:
   ```powershell
   uvicorn app.main:app --reload
   ```

## API Documentation
- REST API: http://localhost:8000/docs
- GraphQL: http://localhost:8000/graphql
- Health Check: http://localhost:8000/health

## For Deployment Team
Docker configuration and deployment instructions will be provided separately.
'@
$readme | Out-File -Encoding UTF8 README.md

# Create start-dev.bat
Write-Host "Creating start-dev.bat..." -ForegroundColor Yellow
$startBat = @'
@echo off
echo Starting WanoApp Backend Development Server...
echo.

REM Activate virtual environment
call venv\Scripts\activate

REM Check if MongoDB is running (Docker)
docker ps | findstr wano-mongo >nul
if errorlevel 1 (
    echo Starting MongoDB...
    docker start wano-mongo 2>nul || echo Please install Docker and run: docker run -d -p 27017:27017 --name wano-mongo mongo:5.0
) else (
    echo MongoDB is already running
)

echo.
echo Starting FastAPI server...
echo Server will be available at: http://localhost:8000
echo API Documentation: http://localhost:8000/docs
echo Press Ctrl+C to stop the server
echo.

REM Start the server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
'@
$startBat | Out-File -Encoding ASCII start-dev.bat

# Copy .env.example to .env and generate secret key
Write-Host "Setting up environment..." -ForegroundColor Yellow
Copy-Item .env.example .env -Force

# Generate random secret key
$secret = -join ((65..90) + (97..122) + (48..57) | Get-Random -Count 32 | ForEach-Object {[char]$_})
$envContent = Get-Content .env -Raw
$envContent = $envContent -replace 'your-secret-key-here-change-in-production', $secret
$envContent | Set-Content .env -NoNewline

Write-Host ""
Write-Host "Project structure created successfully!" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "1. Create and activate virtual environment:" -ForegroundColor White
Write-Host "   python -m venv venv" -ForegroundColor Gray
Write-Host "   .\venv\Scripts\activate" -ForegroundColor Gray
Write-Host ""
Write-Host "2. Install dependencies:" -ForegroundColor White
Write-Host "   pip install -r requirements-dev.txt" -ForegroundColor Gray
Write-Host ""
Write-Host "3. Start MongoDB:" -ForegroundColor White
Write-Host "   Using Docker:" -ForegroundColor Gray
Write-Host "   docker run -d -p 27017:27017 --name wano-mongo mongo:5.0" -ForegroundColor Gray
Write-Host ""
Write-Host "4. Run the application:" -ForegroundColor White
Write-Host "   uvicorn app.main:app --reload" -ForegroundColor Gray
Write-Host "   OR just double-click start-dev.bat" -ForegroundColor Yellow
Write-Host ""
Write-Host "5. Visit http://localhost:8000/docs to see API documentation" -ForegroundColor White
Write-Host ""
Write-Host "Happy coding!" -ForegroundColor Green