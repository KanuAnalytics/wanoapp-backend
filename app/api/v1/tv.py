"""
Wano TV endpoints

app/api/v1/tv.py

The app downloads the whole catalogue and searches/filters it on the client,
so there is no server-side search or home layout here.
"""

from datetime import datetime
from typing import List
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from app.api.deps import get_current_active_user
from app.core.database import get_database
from app.models.tv import TvGenreResponse, TvTitleResponse, TvWatchResponse

router = APIRouter()

# How many titles the History tab shows.
HISTORY_LIMIT = 50


def _title_response(doc: dict) -> TvTitleResponse:
    doc["id"] = str(doc.pop("_id"))
    return TvTitleResponse(**doc)


def _title_object_id(title_id: str) -> ObjectId:
    if not ObjectId.is_valid(title_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid title ID",
        )
    return ObjectId(title_id)


@router.get("/titles", response_model=List[TvTitleResponse])
async def get_titles(current_user: str = Depends(get_current_active_user)):
    """Every active title, most popular first."""
    db = get_database()
    docs = await db.tv_titles.find({"is_active": True}).sort(
        [("popularity", -1), ("_id", 1)]
    ).to_list(length=None)
    return [_title_response(doc) for doc in docs]


@router.get("/titles/{title_id}", response_model=TvTitleResponse)
async def get_title(
    title_id: str,
    current_user: str = Depends(get_current_active_user),
):
    db = get_database()
    doc = await db.tv_titles.find_one(
        {"_id": _title_object_id(title_id), "is_active": True}
    )
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Title not found",
        )
    return _title_response(doc)


@router.get("/genres", response_model=List[TvGenreResponse])
async def get_genres(current_user: str = Depends(get_current_active_user)):
    """Every genre in display order, including ones with no titles yet."""
    db = get_database()
    docs = await db.tv_genres.find({"is_active": True}).sort("order", 1).to_list(
        length=None
    )
    return [TvGenreResponse(name=doc["name"], image=doc["image"]) for doc in docs]


@router.get("/history", response_model=List[TvTitleResponse])
async def get_history(current_user: str = Depends(get_current_active_user)):
    """Titles the user has started watching, most recent first."""
    db = get_database()
    rows = await db.tv_watch_history.find(
        {"user_id": ObjectId(current_user)}, {"title_id": 1}
    ).sort("watched_at", -1).limit(HISTORY_LIMIT).to_list(length=None)

    title_ids = [row["title_id"] for row in rows]
    docs = await db.tv_titles.find(
        {"_id": {"$in": title_ids}, "is_active": True}
    ).to_list(length=None)
    by_id = {doc["_id"]: doc for doc in docs}

    # Keep the history order; skip titles that have since been taken down.
    return [_title_response(by_id[i]) for i in title_ids if i in by_id]


@router.post("/history/{title_id}", response_model=TvWatchResponse)
async def record_watch(
    title_id: str,
    current_user: str = Depends(get_current_active_user),
):
    """Called when the user starts playing a title; moves it to the top of History."""
    db = get_database()
    object_id = _title_object_id(title_id)

    exists = await db.tv_titles.find_one(
        {"_id": object_id, "is_active": True}, {"_id": 1}
    )
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Title not found",
        )

    now = datetime.utcnow()
    await db.tv_watch_history.update_one(
        {"user_id": ObjectId(current_user), "title_id": object_id},
        {
            "$set": {"watched_at": now},
            "$setOnInsert": {"first_watched_at": now},
        },
        upsert=True,
    )
    return TvWatchResponse(title_id=title_id, watched_at=now)
