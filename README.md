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
