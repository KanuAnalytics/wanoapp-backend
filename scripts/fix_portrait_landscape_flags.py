"""
Clears `supports_landscape` on portrait videos that were saved as landscape.

Why this exists: until Nov 2026 the app set `supports_landscape` from the
uploaded file's stored frame size, which ignores the rotation tag phones put
on portrait recordings (a 1920x1080 frame plus "rotate 90"). Those posts were
saved as landscape, so the feed letterboxed them and showed a Fullscreen
button. The app now reads the upright size before posting; this fixes the
posts saved before that.

Only videos with `supports_landscape: true` are looked at. For each one the
upright size is read from Cloudflare Stream (`result.input.width/height` of
GET /accounts/{id}/stream/{uid}, which has the rotation applied). If Stream
says the video is not wider than it is tall:
- `supports_landscape` is set to false and `metadata.width/height` to
  Stream's numbers;
- if the video was ingested into Recombee, its `supports_landscape`
  property there is set to false too.
Videos Stream confirms as landscape are left alone, as are deleted videos
(they point at a shared placeholder stream) and ones whose size Stream can't
report (no Stream id, not processed yet).

Dry run by default; nothing is written. With --apply the documents that will
change are first backed up as JSON (to --backup-dir). Safe to re-run.

Run from the repo root:
    python -m scripts.fix_portrait_landscape_flags
    python -m scripts.fix_portrait_landscape_flags --apply
"""

import argparse
import asyncio
import os
import re
from datetime import datetime

import httpx
from bson import json_util
from motor.motor_asyncio import AsyncIOMotorClient
from recombee_api_client.api_requests import Batch, SetItemValues

from app.core.config import settings
from app.services.recombee_service import recombee_send

# Soft-deleted videos point at a shared placeholder stream instead of their
# own. The first placeholder was itself deleted from Stream, so videos that
# still point at it have no size to read. Neither belongs to a real post.
DELETED_VIDEO_PLACEHOLDER_UIDS = {
    "97002814c3643d7bba41a9311db4e610",
    "fc6b3da74765fa42f7a2cde3de5b2967",
}
# Parallel Stream API requests; Cloudflare allows 1200 requests / 5 minutes.
CONCURRENCY = 4


def stream_uid(video: dict):
    match = re.search(r"videodelivery\.net/([a-f0-9]+)/", video.get("remoteUrl_CF") or "")
    return match.group(1) if match else None


async def stream_size(client: httpx.AsyncClient, uid: str):
    """(width, height) as Stream plays the video, or None if it can't say."""
    url = f"{settings.CLOUDFLARE_STREAM_API_BASE}/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/stream/{uid}"
    for attempt in range(3):
        response = await client.get(url)
        if response.status_code == 429:
            await asyncio.sleep(5 * (attempt + 1))
            continue
        if response.status_code != 200:
            return None
        size = (response.json().get("result") or {}).get("input") or {}
        width, height = size.get("width"), size.get("height")
        # Stream reports -1 until it has processed the upload.
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            return width, height
        return None
    return None


def backup(videos: list, backup_dir: str):
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, "videos.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json_util.dumps(videos, indent=2))
    print(f"Backup of {len(videos)} videos written to {path}")


async def run(apply: bool, backup_dir: str):
    if not settings.CLOUDFLARE_ACCOUNT_ID or not settings.CLOUDFLARE_STREAM_API_TOKEN:
        raise SystemExit("Cloudflare Stream account ID or API token not configured")

    mongo = AsyncIOMotorClient(settings.MONGODB_URL)
    db = mongo[settings.DATABASE_NAME]
    try:
        videos = await db.videos.find({"supports_landscape": True}).sort("created_at", -1).to_list(length=None)
        print(f"Database {settings.DATABASE_NAME}: {len(videos)} videos have supports_landscape: true")

        to_fix, landscape, deleted, unknown = [], 0, 0, []
        semaphore = asyncio.Semaphore(CONCURRENCY)
        headers = {"Authorization": f"Bearer {settings.CLOUDFLARE_STREAM_API_TOKEN}"}
        async with httpx.AsyncClient(headers=headers, timeout=30) as client:

            async def check(video):
                uid = stream_uid(video)
                if uid in DELETED_VIDEO_PLACEHOLDER_UIDS:
                    return video, "deleted"
                if not uid:
                    return video, None
                async with semaphore:
                    return video, await stream_size(client, uid)

            for video, size in await asyncio.gather(*(check(v) for v in videos)):
                if size == "deleted":
                    deleted += 1
                elif size is None:
                    unknown.append(video)
                elif size[0] > size[1]:
                    landscape += 1
                else:
                    to_fix.append((video, size))

        print(f"  confirmed landscape by Stream: {landscape}")
        print(f"  portrait or square, to fix:    {len(to_fix)}")
        print(f"  deleted (placeholder stream):  {deleted}")
        print(f"  size unknown, left alone:      {len(unknown)}")
        for video, (width, height) in to_fix:
            created = video.get("created_at")
            created = created.strftime("%Y-%m-%d") if created else "?"
            print(
                f"    {video['_id']}  {created}  {width}x{height}  "
                f"recombee={bool(video.get('recombee'))}"
            )
        for video in unknown:
            print(f"    unknown: {video['_id']}  {video.get('remoteUrl_CF') or '(no Stream URL)'}")

        if not to_fix:
            print("Nothing to change.")
            return
        if not apply:
            print("Dry run: nothing written. Re-run with --apply to fix these.")
            return

        backup([video for video, _ in to_fix], backup_dir)

        for video, (width, height) in to_fix:
            await db.videos.update_one(
                {"_id": video["_id"], "supports_landscape": True},
                {"$set": {
                    "supports_landscape": False,
                    "metadata.width": width,
                    "metadata.height": height,
                    "updated_at": datetime.utcnow(),
                }},
            )
        print(f"Updated {len(to_fix)} videos in MongoDB")

        in_recombee = [str(video["_id"]) for video, _ in to_fix if video.get("recombee")]
        if in_recombee:
            requests = []
            for item_id in in_recombee:
                request = SetItemValues(item_id, {"supports_landscape": False}, cascade_create=False)
                request.timeout = 30000
                requests.append(request)
            await recombee_send(Batch(requests))
            print(f"Updated {len(in_recombee)} items in Recombee")
    finally:
        mongo.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    parser.add_argument("--backup-dir", help="where --apply saves the JSON backup")
    args = parser.parse_args()
    backup_dir = args.backup_dir or os.path.join(
        os.getcwd(),
        f"landscape-flags-backup-{datetime.now():%Y-%m-%d-%H%M%S}",
    )
    asyncio.run(run(args.apply, backup_dir))
