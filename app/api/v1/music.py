"""
Music endpoints

app/api/v1/music.py
"""

from fastapi import APIRouter, HTTPException
import os
import json
import logging

logger = logging.getLogger(__name__)

router = APIRouter()

SONGS_FILE = os.path.join(os.path.dirname(__file__), "json/songs.json")

@router.get("/songs")
async def get_songs():
    """Return all songs from songs.json"""
    try:
        with open(SONGS_FILE, "r", encoding="utf-8") as f:
            songs = json.load(f)
        return {"songs": songs}
    except FileNotFoundError:
        logger.error(f"songs.json not found at {SONGS_FILE}")
        raise HTTPException(status_code=404, detail="songs.json not found")
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding songs.json: {e}")
        raise HTTPException(status_code=500, detail="Error decoding songs.json")
    except Exception as e:
        logger.error(f"Unexpected error reading songs.json: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error reading songs.json")