from fastapi import APIRouter, Depends
from app.api.deps import require_admin
from app.core.database import get_database
from app.core.config import settings
from bson import json_util
from recombee_api_client.api_requests import SetItemValues, Batch
import json
import re
import httpx
import asyncio
from app.services.recombee_service import recombee_send
from app.services import recommendations

router = APIRouter()


@router.post("/test-first-item")
async def get_first_video(current_user: str = Depends(require_admin)):
    db = get_database()
    videos = await db.videos.find().sort("created_at", -1).to_list(length=10)
    return json.loads(json_util.dumps(videos))


def extract_cf_video_id(remote_url_cf: str) -> str | None:
    match = re.search(r"videodelivery\.net/([a-f0-9]+)/", remote_url_cf)
    return match.group(1) if match else None


async def fetch_cf_dimensions(video_id: str) -> tuple[int, int] | None:
    url = f"{settings.CLOUDFLARE_STREAM_API_BASE}/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/stream/{video_id}"
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
async def ingest_videos(current_user: str = Depends(require_admin)):
    """Send videos Recombee has never seen (kept for compatibility; sync-items covers this)."""
    db = get_database()
    sent = 0
    batch = []
    async for video in db.videos.find({"recombee": {"$ne": True}}):
        batch.append(SetItemValues(str(video["_id"]), recommendations.item_values(video), cascade_create=True))
        if len(batch) >= recommendations.BATCH_SIZE:
            await recombee_send(Batch(batch)); sent += len(batch); batch = []
    if batch:
        await recombee_send(Batch(batch)); sent += len(batch)
    await db.videos.update_many({"recombee": {"$ne": True}}, {"$set": {"recombee": True}})
    return {"total_ingested": sent}


@router.post("/fix-thumbnails")
async def fix_thumbnails(apply: bool = False, current_user: str = Depends(require_admin)):
    """Give posts still showing the grey placeholder Cloudflare's own frame.
    Dry run unless apply=true; safe to run again."""
    from app.services import thumbnails
    return await thumbnails.backfill(get_database(), apply=apply)


@router.post("/rescue-uploads")
async def rescue_uploads(current_user: str = Depends(require_admin)):
    """Run the stuck-upload rescue now (it also runs every 5 minutes)."""
    from app.services import stuck_uploads
    return await stuck_uploads.rescue_once(get_database())


@router.post("/sync-items")
async def sync_items(current_user: str = Depends(require_admin)):
    """Re-send every video's properties (readiness, privacy, counts) to Recombee.
    Run once after deploying the For You fixes, then whenever Recombee's copy
    looks out of date."""
    return await recommendations.sync_items(get_database())


@router.post("/backfill-hashtags")
async def backfill_hashtags(current_user: str = Depends(require_admin)):
    db = get_database()
    videos = await db.videos.find({"supports_landscape": {"$exists": False}}).sort("created_at", -1).skip(0).to_list(length=1100)

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
