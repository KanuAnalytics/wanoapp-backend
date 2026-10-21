"""
Music endpoints

app/api/v1/music.py
"""

from fastapi import APIRouter, Depends, HTTPException, status
from bson import ObjectId
import logging

from app.api.deps import get_current_active_user
from app.core.database import get_database
from app.services.songs import find_song, list_genres, list_songs

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/songs")
async def get_songs():
    """
    Return every active song and the genre list. The app fetches this once and
    filters by genre and search on the device.
    """
    try:
        songs = await list_songs()
    except Exception as e:
        logger.error(f"Unexpected error reading songs: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error reading songs")
    genres = await list_genres()
    return {"songs": songs, "genres": genres}


@router.get("/favorites")
async def get_favorite_songs(current_user: str = Depends(get_current_active_user)):
    """Ids of the songs the current user has saved, most recent first."""
    db = get_database()
    user = await db.users.find_one({"_id": ObjectId(current_user)}, {"favorite_songs": 1})
    song_ids = (user or {}).get("favorite_songs") or []
    return {"song_ids": [str(s) for s in reversed(song_ids)]}


@router.post("/songs/{song_id}/favorite")
async def add_favorite_song(song_id: str, current_user: str = Depends(get_current_active_user)):
    song = await find_song(song_id)
    if song is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Song not found")
    db = get_database()
    await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {"$addToSet": {"favorite_songs": str(song_id)}},
    )
    return {"message": "Song added to favorites", "song_id": str(song_id)}


@router.delete("/songs/{song_id}/favorite")
async def remove_favorite_song(song_id: str, current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {"$pull": {"favorite_songs": str(song_id)}},
    )
    return {"message": "Song removed from favorites", "song_id": str(song_id)}
