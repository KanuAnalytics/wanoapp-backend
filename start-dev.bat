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
