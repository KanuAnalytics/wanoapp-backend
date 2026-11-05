"""
Wano TV endpoints

app/api/v1/tv.py

The app downloads the whole catalogue and searches/filters it on the client,
so there is no server-side search or home layout here.
"""

import logging
from datetime import datetime
from typing import List, Optional
from bson import ObjectId
from pydantic import ValidationError
from fastapi import APIRouter, Depends, HTTPException, status
from app.api.deps import get_current_active_user
from app.core.database import get_database
from app.models.tv import TvGenreResponse, TvMaker, TvTitleResponse, TvWatchResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# How many titles the History tab shows.
HISTORY_LIMIT = 50


async def _title_responses(db, docs: List[dict]) -> List[TvTitleResponse]:
    """
    Builds the responses, filling in each title's makers from their user
    accounts in one query. Makers whose account is gone or deactivated are
    left out, so the app never links to a deleted profile. A malformed
    title is logged and skipped, so one bad catalogue entry can't 500 the
    whole list.
    """
    maker_ids = {
        maker.get("user_id")
        for doc in docs
        for maker in doc.get("makers") or []
        if isinstance(maker, dict) and maker.get("user_id")
    }
    users = {}
    if maker_ids:
        async for user in db.users.find(
            {"_id": {"$in": list(maker_ids)}, "is_active": True},
            {"username": 1, "display_name": 1, "profile_picture": 1},
        ):
            users[user["_id"]] = user

    responses = []
    for doc in docs:
        raw_id = doc.get("_id")
        try:
            makers = []
            for maker in doc.get("makers") or []:
                user = users.get(maker.get("user_id"))
                if not user:
                    continue
                makers.append(TvMaker(
                    user_id=str(user["_id"]),
                    username=(user.get("username") or "").strip(),
                    display_name=user.get("display_name"),
                    profile_picture=user.get("profile_picture"),
                    role=maker["role"],
                ))
            data = {**doc, "id": str(doc["_id"]), "makers": makers}
            data.pop("_id", None)
            responses.append(TvTitleResponse(**data))
        except (ValidationError, KeyError, TypeError, ValueError, AttributeError) as e:
            logger.warning("Skipping invalid tv_titles document %s: %s", raw_id, e)
    return responses


def _genre_response(doc: dict) -> Optional[TvGenreResponse]:
    try:
        return TvGenreResponse(name=doc["name"], image=doc["image"])
    except (ValidationError, KeyError, TypeError, ValueError) as e:
        logger.warning("Skipping invalid tv_genres document %s: %s", doc.get("_id"), e)
        return None


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
    return await _title_responses(db, docs)


@router.get("/genres", response_model=List[TvGenreResponse])
async def get_genres(current_user: str = Depends(get_current_active_user)):
    """Every genre in display order, including ones with no titles yet."""
    db = get_database()
    docs = await db.tv_genres.find({"is_active": True}).sort("order", 1).to_list(
        length=None
    )
    genres = (_genre_response(doc) for doc in docs)
    return [g for g in genres if g is not None]


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
    return await _title_responses(db, [by_id[i] for i in title_ids if i in by_id])


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
