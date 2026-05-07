from fastapi import APIRouter
from app.core.database import get_database
from app.core.config import settings
from bson import json_util
from recombee_api_client.api_requests import SetItemValues, Batch
import json
import re
import httpx
import asyncio
from app.services.recombee_service import recombee_client

router = APIRouter()


@router.post("/test-first-item")
async def get_first_video():
    db = get_database()
    videos = await db.videos.find().sort("created_at", -1).to_list(length=10)
    return json.loads(json_util.dumps(videos))


def extract_cf_video_id(remote_url_cf: str) -> str | None:
    match = re.search(r"videodelivery\.net/([a-f0-9]+)/", remote_url_cf)
    return match.group(1) if match else None


async def fetch_cf_dimensions(video_id: str) -> tuple[int, int] | None:
    url = f"{settings.CLOUDFLARE_STREAM_API_BASE}/{settings.CLOUDFLARE_ACCOUNT_ID}/stream/{video_id}"
    headers = {"Authorization": f"Bearer {settings.CLOUDFLARE_STREAM_API_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=headers)
    if response.status_code != 200:
        return None
    data = response.json()
    input_data = data.get("result", {}).get("input", {})
    width = input_data.get("width")
    height = input_data.get("height")
    if width and height:
        return width, height
    return None


@router.post("/ingest-videos")
async def ingest_videos():
    db = get_database()
    videos = await db.videos.find({"recombee": {"$ne": True}, "privacy": "public", "is_active": True}).sort("created_at", -1).to_list(length=None)

    if not videos:
        return {"message": "No new videos to ingest", "total_ingested": 0}

    requests = []
    for video in videos:
        item_id = str(video["_id"])
        values = {
            "creator_id": str(video.get("creator_id", "")),
            "description": video.get("description") or "",
            "video_type": video.get("video_type") or "",
            "duration": float(video.get("duration") or video.get("metadata", {}).get("duration", 0.0)),
            "thumbnail": video.get("urls", {}).get("thumbnail") or "",
            "views_count": int(video.get("views_count", 0)),
            "likes_count": int(video.get("likes_count", 0)),
            "comments_count": int(video.get("comments_count", 0)),
            "bookmarks_count": int(video.get("bookmarks_count", 0)),
            "hashtags": video.get("hashtags") or [],
            "is_active": bool(video.get("is_active", True)),
            "supports_landscape": bool(video.get("supports_landscape", False)),
            "privacy": video.get("privacy") or "public",
            "created_at": video["created_at"].isoformat() if video.get("created_at") else None,
        }
        req = SetItemValues(item_id, values, cascade_create=True)
        req.timeout = 30000
        requests.append(req)

    recombee_client.send(Batch(requests))

    ingested_ids = [video["_id"] for video in videos]
    await db.videos.update_many(
        {"_id": {"$in": ingested_ids}},
        {"$set": {"recombee": True}}
    )

    return {"total_ingested": len(videos), "ingested_ids": [str(i) for i in ingested_ids]}


@router.post("/backfill-hashtags")
async def backfill_hashtags():
    db = get_database()
    videos = await db.videos.find({}).sort("created_at", -1).skip(0).to_list(length=300)

    updated = 0
    cf_failed = 0

    for video in videos:
        updates = {}

        description = video.get("description", "") or ""
        hashtags = re.findall(r"#(\w+)", description)
        if hashtags:
            updates["hashtags"] = hashtags

        remote_url = video.get("remoteUrl_CF", "")
        if remote_url:
            video_id = extract_cf_video_id(remote_url)
            if video_id:
                dimensions = await fetch_cf_dimensions(video_id)
                if dimensions:
                    width, height = dimensions
                    updates["metadata.width"] = width
                    updates["metadata.height"] = height
                    updates["supports_landscape"] = width > height
                else:
                    cf_failed += 1
                await asyncio.sleep(0.3)

        if updates:
            await db.videos.update_one({"_id": video["_id"]}, {"$set": updates})
            updated += 1

    return {"total_processed": len(videos), "total_updated": updated, "cf_failed": cf_failed}
