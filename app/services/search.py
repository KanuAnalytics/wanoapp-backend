"""
Video and comment search.

With ATLAS_SEARCH on and the indexes created (python -m scripts.create_search_indexes),
search uses Atlas Search: relevance with typo tolerance, boosted by views. Without
it, or if Atlas Search errors, it falls back to whole-word matching (every word
must appear in the title, description or hashtags), most watched first, and
waits ATLAS_RETRY_SECONDS before trying Atlas again.

Either way only posts anyone can see are returned (public, live, ready, not
held, not scheduled, not future-dated), and comments only on such posts.
User input is always matched literally.
"""

import logging
import re
import time
from typing import List, Optional

from bson import ObjectId
from pymongo.errors import ExecutionTimeout

from app.core.config import settings
from app.services.creators import _showable
from app.services.new_feed import date_ok, ready_filter

logger = logging.getLogger(__name__)

VIDEO_INDEX = "wano_videos_search"
COMMENT_INDEX = "wano_comments_search"
MAX_TERM = 100
MAX_WORDS = 5
ATLAS_RETRY_SECONDS = 600
QUERY_MS = 3000
COMMENT_SCAN = 2000

_down_until: dict = {}


def reset():
    _down_until.clear()


def atlas_enabled() -> bool:
    return bool(getattr(settings, "ATLAS_SEARCH", False))


def _atlas_ok(index: str) -> bool:
    return atlas_enabled() and _down_until.get(index, 0) <= time.monotonic()


def _atlas_failed(index: str, e: Exception) -> None:
    _down_until[index] = time.monotonic() + ATLAS_RETRY_SECONDS
    logger.warning("Atlas Search (%s) unavailable, using word match for %ss: %s", index, ATLAS_RETRY_SECONDS, e)


def clean(q) -> str:
    return " ".join(str(q or "").split())[:MAX_TERM]


def _word(w: str) -> dict:
    return {"$regex": rf"(?<!\w){re.escape(w)}(?!\w)", "$options": "i"}


def visible_videos() -> dict:
    """Posts anyone may see."""
    return {**_showable(), "created_at": date_ok(), "$and": [ready_filter()]}


def _word_match(term: str) -> dict:
    words = term.split()[:MAX_WORDS]
    return {"$and": [{"$or": [{"title": _word(w)}, {"description": _word(w)},
                              {"hashtags": {"$regex": f"^#?{re.escape(w.lstrip('#'))}$", "$options": "i"}}]}
                     for w in words]}


def atlas_video_stages(term: str) -> List[dict]:
    term = clean(term)
    return [
        {"$search": {
            "index": VIDEO_INDEX,
            "compound": {
                "should": [
                    {"text": {"query": term, "path": ["title", "description"],
                              "fuzzy": {"maxEdits": 1, "prefixLength": 2}}},
                    {"text": {"query": term.lstrip("#"), "path": "hashtags", "score": {"boost": {"value": 3}}}},
                ],
                "minimumShouldMatch": 1,
                "score": {"function": {"multiply": [
                    {"score": "relevance"},
                    {"add": [{"constant": 1},
                             {"log1p": {"path": {"value": "views_count", "undefined": 0}}}]},
                ]}},
            },
        }},
        {"$match": visible_videos()},
    ]


async def _atlas_videos(db, term: str, skip: int, limit: int, tail: List[dict]) -> list:
    pipeline = atlas_video_stages(term) + [{"$skip": skip}, {"$limit": limit}] + tail
    return await db.videos.aggregate(pipeline, maxTimeMS=QUERY_MS).to_list(length=limit)


async def _plain_videos(db, term: str, skip: int, limit: int, tail: List[dict]) -> list:
    pipeline = [
        {"$match": {**visible_videos(), "$and": [ready_filter(), _word_match(term)]}},
        {"$sort": {"views_count": -1, "created_at": -1, "_id": -1}},
        {"$skip": skip},
        {"$limit": limit},
    ] + tail
    return await db.videos.aggregate(pipeline, maxTimeMS=QUERY_MS).to_list(length=limit)


async def find_videos(db, q, skip: int = 0, limit: int = 20, tail: Optional[List[dict]] = None) -> list:
    term = clean(q)
    if not re.search(r"\w", term):
        return []
    tail = tail or []
    if _atlas_ok(VIDEO_INDEX):
        try:
            return await _atlas_videos(db, term, skip, limit, tail)
        except Exception as e:
            _atlas_failed(VIDEO_INDEX, e)
    try:
        return await _plain_videos(db, term, skip, limit, tail)
    except ExecutionTimeout:
        logger.warning("Video search for %r timed out", term)
        return []


# ---------------------------------------------------------------- comments

async def _comment_candidates(db, term: str, video: Optional[ObjectId]):
    base = {"is_active": True}
    if video is not None:
        base["video_id"] = video
    if _atlas_ok(COMMENT_INDEX):
        search = {"index": COMMENT_INDEX,
                  "text": {"query": term, "path": "content", "fuzzy": {"maxEdits": 1, "prefixLength": 2}}}
        if video is not None:
            search = {"index": COMMENT_INDEX, "compound": {
                "must": [{"text": search["text"]}],
                "filter": [{"equals": {"path": "video_id", "value": video}}]}}
        try:
            return await db.comments.aggregate(
                [{"$search": search}, {"$match": base}, {"$limit": COMMENT_SCAN}], maxTimeMS=QUERY_MS
            ).to_list(length=COMMENT_SCAN)
        except Exception as e:
            _atlas_failed(COMMENT_INDEX, e)
    words = term.split()[:MAX_WORDS]
    q = {**base, "$and": [{"$or": [{"content": {"$regex": re.escape(w), "$options": "i"}},
                                   {"user_display_name": {"$regex": re.escape(w), "$options": "i"}}]}
                          for w in words]}
    try:
        return await db.comments.find(q).sort("created_at", -1).limit(COMMENT_SCAN).max_time_ms(QUERY_MS).to_list(
            length=COMMENT_SCAN)
    except ExecutionTimeout:
        logger.warning("Comment search for %r timed out", term)
        return []


async def find_comments(db, q, viewer: Optional[str], video: Optional[ObjectId] = None,
                        skip: int = 0, limit: int = 20) -> list:
    """Matching comments on posts the viewer may see (public ones, or their own)."""
    term = clean(q)
    if not term:
        return []
    rows = await _comment_candidates(db, term, video)
    vids = list({r.get("video_id") for r in rows if isinstance(r.get("video_id"), ObjectId)})
    ok = set()
    if vids:
        ors = [visible_videos()]
        if viewer and ObjectId.is_valid(viewer):
            ors.append({"creator_id": ObjectId(viewer), "is_active": True})
        ok = {d["_id"] async for d in db.videos.find({"_id": {"$in": vids}, "$or": ors}, {"_id": 1})}
    shown = [r for r in rows if r.get("video_id") in ok]
    return shown[skip:skip + limit]
