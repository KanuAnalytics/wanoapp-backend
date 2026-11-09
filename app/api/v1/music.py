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

# Song ids are short app-facing strings (not ObjectIds); anything longer is junk.
MAX_SONG_ID_LENGTH = 64


def _song_id(song_id: str) -> str:
    song_id = str(song_id)
    if not song_id or len(song_id) > MAX_SONG_ID_LENGTH:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid song ID")
    return song_id


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
    song_id = _song_id(song_id)
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
    song_id = _song_id(song_id)
    db = get_database()
    await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {"$pull": {"favorite_songs": str(song_id)}},
    )
    return {"message": "Song removed from favorites", "song_id": str(song_id)}


# ---------------------------------------------------------------- trending sounds

from datetime import datetime, timedelta  # noqa: E402
from typing import Optional  # noqa: E402

from fastapi import Query  # noqa: E402

from app.api.deps import get_optional_active_user  # noqa: E402

TRENDING_SOUND_DAYS = 7


def _visible_posts() -> dict:
    from app.services.creators import _showable
    return {**_showable(), "moderation.state": {"$nin": ["held", "removed"]}}


@router.get("/trending")
async def trending_sounds(
    country: Optional[str] = Query(None, pattern=r"^[A-Za-z]{2}$"),
    limit: int = Query(20, ge=1, le=50),
    current_user: Optional[str] = Depends(get_optional_active_user),
):
    """Sounds used by the most different creators in the last 7 days (one creator
    posting a sound 50 times counts once), then by views. Logged out: authenticated=false."""
    db = get_database()
    q = {**_visible_posts(), "created_at": {"$gte": datetime.utcnow() - timedelta(days=TRENDING_SOUND_DAYS)},
         "sound.id": {"$type": "string"}}
    if country:
        q["country"] = {"$in": [country.upper(), country.lower()]}
    rows = await db.videos.aggregate([
        {"$match": q},
        {"$sort": {"created_at": -1}},
        {"$group": {"_id": "$sound.id", "sound": {"$first": "$sound"}, "creators": {"$addToSet": "$creator_id"},
                    "posts": {"$sum": 1}, "views": {"$sum": {"$ifNull": ["$views_count", 0]}}}},
        {"$project": {"sound": 1, "posts": 1, "views": 1, "creators": {"$size": "$creators"}}},
        {"$sort": {"creators": -1, "views": -1, "_id": 1}},
        {"$limit": limit},
    ], maxTimeMS=5000).to_list(limit)
    # A cover (the most-watched post using it) and a few creator faces, so the
    # list has pictures rather than the same music note on every row.
    from app.services import thumbnails
    sids = [r["_id"] for r in rows]
    covers, faces = {}, {}
    if sids:
        tops = await db.videos.aggregate([
            {"$match": {**q, "sound.id": {"$in": sids}}},
            {"$sort": {"views_count": -1, "_id": -1}},
            {"$group": {"_id": "$sound.id", "top": {"$first": "$$ROOT"}, "creators": {"$addToSet": "$creator_id"}}},
        ], maxTimeMS=5000).to_list(len(sids))
        people = {c for t in tops for c in t["creators"][:3]}
        users = {u["_id"]: u async for u in db.users.find(
            {"_id": {"$in": list(people)}, "is_active": {"$ne": False}}, {"profile_picture": 1, "username": 1})}
        for t in tops:
            covers[t["_id"]] = {"post_id": str(t["top"]["_id"]), "thumbnail": thumbnails.best_thumbnail(t["top"])}
            faces[t["_id"]] = [{"id": str(c), "profile_picture": users[c].get("profile_picture"),
                                "username": users[c].get("username")} for c in t["creators"][:3] if c in users]
    return [{"sound": {k: (r.get("sound") or {}).get(k) for k in ("id", "title", "artist", "uri")},
             "creators": r["creators"], "posts": r["posts"], "views": r["views"],
             "cover": covers.get(r["_id"]), "faces": faces.get(r["_id"], [])} for r in rows]


@router.get("/sounds/{sound_id}/posts")
async def sound_posts(
    sound_id: str,
    skip: int = Query(0, ge=0, le=5000),
    limit: int = Query(20, ge=1, le=50),
    current_user: Optional[str] = Depends(get_optional_active_user),
):
    """Public posts using a sound, most watched first, and the sound itself."""
    from app.services import thumbnails
    db = get_database()
    sid = _song_id(sound_id)
    docs = await db.videos.find({**_visible_posts(), "sound.id": sid}).sort(
        [("views_count", -1), ("_id", -1)]).skip(skip).limit(limit).to_list(limit)
    users = {u["_id"]: u async for u in db.users.find(
        {"_id": {"$in": list({d.get("creator_id") for d in docs})}, "is_active": {"$ne": False}},
        {"username": 1, "display_name": 1, "profile_picture": 1})}
    sound = None
    if docs:
        sound = {k: (docs[0].get("sound") or {}).get(k) for k in ("id", "title", "artist", "uri")}
    else:
        song = await find_song(sid)
        if song:
            sound = {"id": sid, "title": song.get("title"), "artist": song.get("artist"), "uri": song.get("uri")}
    items = []
    for d in docs:
        u = users.get(d.get("creator_id"))
        if not u:
            continue
        items.append({"id": str(d["_id"]), "thumbnail": thumbnails.best_thumbnail(d),
                      "views_count": d.get("views_count", 0), "likes_count": d.get("likes_count", 0),
                      "creator": {"id": str(u["_id"]), "username": u.get("username"),
                                  "display_name": u.get("display_name"), "profile_picture": u.get("profile_picture")}})
    return {"sound": sound, "items": items}
