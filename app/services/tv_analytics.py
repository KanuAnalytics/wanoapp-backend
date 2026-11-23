"""Bounded, anonymous playback sessions, separate from title popularity."""
import hashlib
from datetime import datetime, timedelta
from bson import ObjectId
from fastapi import HTTPException
from app.services import tv_release


async def record(db, title_id, viewer, body):
    if not ObjectId.is_valid(title_id):
        raise HTTPException(400, "Invalid title ID")
    doc = await db.tv_titles.find_one({"_id": ObjectId(title_id)})
    trailer = body.kind == "trailer_start" or body.trailer
    if not doc or (not doc.get("is_active") and not (trailer and doc.get("status") == "coming_soon")):
        raise HTTPException(404, "Title not available")
    if trailer and not (doc.get("trailer") or {}).get("video_url"):
        raise HTTPException(404, "Trailer not available")
    if not trailer and body.episode and body.episode.startswith("p"):
        if not any(isinstance(p, dict) and f"p{p.get('number')}" == body.episode for p in doc.get("parts") or []):
            raise HTTPException(404, "Episode not available")
    elif not trailer and body.episode:
        ep = next((e for e in tv_release.episodes(doc) if e.get("id") == body.episode), None)
        if not ep or not tv_release.released(ep):
            raise HTTPException(404, "Episode not available")
    if not viewer:
        return {"saved": False}
    now = datetime.utcnow()
    hashed = hashlib.sha256(viewer.encode()).hexdigest()[:32]
    key = f"{hashed}:{title_id}:{body.session}:{'trailer' if trailer else body.episode or 'film'}"
    if not await db.tv_playback_sessions.find_one({"_id": key}, {"_id": 1}) and await db.tv_playback_sessions.count_documents({"viewer": hashed, "created_at": {"$gte": now - timedelta(days=1)}}, limit=500) >= 500:
        raise HTTPException(429, "Too many playback sessions")
    if body.kind == "error":
        await _note_error(db, doc["_id"], body, now)
    position = min(float(body.position), float(body.duration)) if body.duration else 0
    await db.tv_playback_sessions.update_one({"_id": key}, {
        "$setOnInsert": {"title_id": doc["_id"], "viewer": hashed, "episode": body.episode, "trailer": trailer, "created_at": now},
        "$set": {"updated_at": now}, "$max": {"position": position, "duration": float(body.duration),
            "started": int(body.kind in ("play_start", "trailer_start", "progress", "complete")),
            "completed": int(body.kind == "complete"), "failed": int(body.kind == "error")}}, upsert=True)
    return {"saved": True}


ERROR_KEEP_DAYS = 30


async def _note_error(db, title_id, body, now) -> None:
    """What the player said when a film didn't play (2.3.63 apps send it)."""
    detail = " ".join(str(getattr(body, "detail", "") or "").split())[:300]
    try:
        await db.tv_playback_errors.insert_one({"title_id": title_id, "episode": body.episode, "trailer": bool(body.trailer),
                                                "detail": detail or "(no message: app 2.3.62 or older)",
                                                "platform": getattr(body, "platform", None), "position": float(body.position),
                                                "at": now})
        await db.tv_playback_errors.create_index("at", expireAfterSeconds=ERROR_KEEP_DAYS * 86400)
    except Exception:
        pass


async def recent_errors(db, days: int = 30, limit: int = 30) -> list:
    since = datetime.utcnow() - timedelta(days=days)
    rows = await db.tv_playback_errors.aggregate([
        {"$match": {"at": {"$gte": since}}},
        {"$group": {"_id": {"title_id": "$title_id", "detail": "$detail", "platform": "$platform"},
                    "count": {"$sum": 1}, "last": {"$max": "$at"}}},
        {"$sort": {"count": -1}}, {"$limit": limit}]).to_list(limit)
    return [{"title_id": str(r["_id"]["title_id"]), "detail": r["_id"]["detail"], "platform": r["_id"]["platform"],
             "count": r["count"], "last": r["last"].isoformat() + "Z"} for r in rows]


async def report(db, days):
    since = datetime.utcnow() - timedelta(days=days)
    ratio = {"$cond": [{"$gt": ["$duration", 0]}, {"$divide": ["$position", "$duration"]}, 0]}
    bucket = lambda low, high: {"$sum": {"$cond": [{"$and": [{"$eq": ["$started", 1]}, {"$eq": ["$completed", 0]}, {"$gte": [ratio, low]}, {"$lt": [ratio, high]}]}, 1, 0]}}
    return [r async for r in db.tv_playback_sessions.aggregate([
        {"$match": {"created_at": {"$gte": since}}},
        {"$group": {"_id": {"title_id": "$title_id", "episode": "$episode", "trailer": "$trailer"},
            "starts": {"$sum": "$started"}, "completed": {"$sum": "$completed"}, "errors": {"$sum": "$failed"},
            "stopped_under_25": bucket(0, .25), "stopped_25_50": bucket(.25, .5),
            "stopped_50_75": bucket(.5, .75), "stopped_75_100": bucket(.75, 1.01),
            "avg_pct": {"$avg": {"$cond": [{"$gt": ["$duration", 0]}, {"$divide": ["$position", "$duration"]}, None]}}}},
        {"$sort": {"starts": -1}}, {"$limit": 1000}])]
