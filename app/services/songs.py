"""
app/services/songs.py

The song library behind GET /api/v1/music/songs, favourites and the sound
saved on a post.

Songs live in the `songs` collection. Each one keeps its original id from
songs.json in `song_id`, and that is the id the app sees, so ids already
stored on devices, favourites and posts never change. Until the collection is
imported (scripts/import_songs.py), or if it can't be read, songs.json is used.
"""

import json
import logging
import os
from typing import List, Optional

from app.core.database import get_database

logger = logging.getLogger(__name__)

SONGS_FILE = os.path.join(os.path.dirname(__file__), "..", "api", "v1", "json", "songs.json")

_json_songs: Optional[List[dict]] = None


def duration_to_seconds(duration) -> Optional[float]:
    """'2:23' -> 143.0. Anything unreadable becomes None."""
    if duration is None:
        return None
    if isinstance(duration, (int, float)):
        return float(duration)
    try:
        parts = [float(p) for p in str(duration).strip().split(":")]
    except ValueError:
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def load_json_songs() -> List[dict]:
    """songs.json in the same shape as a `songs` document, read once per process."""
    global _json_songs
    if _json_songs is None:
        with open(SONGS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        _json_songs = [
            {
                "song_id": str(s.get("id")),
                "title": s.get("title") or "",
                "uri": s.get("uri"),
                "duration": s.get("duration"),
                "duration_sec": duration_to_seconds(s.get("duration")),
                "artist": None,
                "cover_url": None,
                "genres": [],
                "is_active": True,
                "sort_order": index,
            }
            for index, s in enumerate(raw)
            if s.get("id") is not None and s.get("uri")
        ]
    return _json_songs


def song_out(doc: dict) -> dict:
    """
    API shape of a song. id/title/uri/duration are the fields older app
    versions read and must keep their meaning; the rest are additions.
    """
    return {
        "id": str(doc.get("song_id")),
        "title": doc.get("title") or "",
        "uri": doc.get("uri"),
        "duration": doc.get("duration"),
        "duration_sec": doc.get("duration_sec"),
        "artist": doc.get("artist") or None,
        "cover_url": doc.get("cover_url") or None,
        "genres": doc.get("genres") or [],
    }


async def list_songs() -> List[dict]:
    db = get_database()
    try:
        docs = await db.songs.find(
            {"is_active": {"$ne": False}, "song_id": {"$ne": None}},
        ).sort([("sort_order", 1), ("_id", 1)]).to_list(length=None)
        if docs:
            return [song_out(d) for d in docs]
    except Exception:
        logger.exception("Reading songs from the database failed; using songs.json")
    return [song_out(s) for s in load_json_songs()]


async def list_genres() -> List[dict]:
    db = get_database()
    try:
        docs = await db.music_genres.find(
            {"is_active": {"$ne": False}, "slug": {"$ne": None}},
        ).sort([("sort_order", 1), ("name", 1)]).to_list(length=None)
    except Exception:
        logger.exception("Reading music genres failed")
        return []
    return [{"id": d["slug"], "name": d.get("name") or d["slug"]} for d in docs]


async def find_song(song_id: Optional[str]) -> Optional[dict]:
    """An active song by its app-facing id, or None."""
    if not song_id:
        return None
    song_id = str(song_id)
    db = get_database()
    try:
        doc = await db.songs.find_one({"song_id": song_id, "is_active": {"$ne": False}})
        if doc:
            return doc
        if await db.songs.estimated_document_count() > 0:
            return None
    except Exception:
        logger.exception("Looking up song %s failed; using songs.json", song_id)
    return next((s for s in load_json_songs() if s["song_id"] == song_id), None)


def post_sound(song: Optional[dict]) -> Optional[dict]:
    """
    The copy of a song saved on a post as `sound`. It is a copy so the post
    keeps showing its song even if the song is renamed or removed later.
    """
    if not song or not song.get("uri"):
        return None
    return {
        "id": str(song.get("song_id")),
        "title": song.get("title") or "",
        "uri": song.get("uri"),
        "artist": song.get("artist") or None,
    }


async def sound_for_post(song_id: Optional[str] = None, audio_uri: Optional[str] = None) -> Optional[dict]:
    """
    Resolve the sound to save on a post: by id first, then by the audio URL
    (older app versions only send the URL to /videos/stitch). Never raises --
    a post is still created when its sound can't be resolved.
    """
    try:
        song = await find_song(song_id)
        if song is None and audio_uri:
            db = get_database()
            song = await db.songs.find_one({"uri": audio_uri})
            if song is None:
                song = next((s for s in load_json_songs() if s["uri"] == audio_uri), None)
        return post_sound(song)
    except Exception:
        logger.exception("Resolving the sound for a post failed (song_id=%s)", song_id)
        return None
