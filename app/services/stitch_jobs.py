"""
app/services/stitch_jobs.py

Multi-clip posts. A job waits in `pending_videos` until Cloudflare Stream has
processed every clip, then the editVideo Lambda stitches them into one video
and the result is saved as a normal post.
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Optional

import boto3
from botocore.config import Config
from bson import ObjectId

from app.core.config import settings
from app.core.database import get_database
from app.services.upload_DO import (
    delete_stream_video,
    extract_stream_uid,
    get_stream_video_name,
    get_stream_video_status,
)
from app.services.video_posts import DEFAULT_THUMBNAIL, create_video_post

logger = logging.getLogger(__name__)

STATUS_WAITING = "waiting"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# Only videos uploaded under this folder are ever deleted after stitching. Posts are
# uploaded under "videos/", so a post passed in as a clip can never be removed.
CLIP_NAME_PREFIX = "clips/"

STITCHED_WIDTH = 1080
STITCHED_HEIGHT = 1920

# asyncio only keeps weak references to tasks, so running jobs are held here until they finish.
_running_tasks = set()


async def create_stitch_job(
    user_id: str,
    clips: list,
    audio_url: Optional[str],
    keep_original_sound: bool,
    post: dict,
) -> str:
    """
    clips: [{"url", "start", "end"}] in playback order; end=None means to the end of the clip.
    post: {"description", "privacy", "thumbnail", "categoryId", "subcategoryId", "comments_enabled"}
    """
    db = get_database()
    now = datetime.utcnow()

    clip_docs = []
    for clip in clips:
        stream_uid = extract_stream_uid(clip["url"])
        clip_docs.append({
            "url": clip["url"],
            "stream_uid": stream_uid,
            "start": clip.get("start") or 0.0,
            "end": clip.get("end"),
            # Clips that are not on Cloudflare Stream (plain MP4 links) need no processing.
            "ready": stream_uid is None,
        })

    result = await db.pending_videos.insert_one({
        "creator_id": ObjectId(user_id),
        "status": STATUS_WAITING,
        "clips": clip_docs,
        "audio_url": audio_url,
        "keep_original_sound": keep_original_sound,
        "post": post,
        "error": None,
        "result": None,
        "clips_cleaned_at": None,
        "clip_cleanup": None,
        "created_at": now,
        "updated_at": now,
    })
    job_id = result.inserted_id

    # The job is saved before the clips are checked, so a webhook that arrives
    # during the check still finds it. A clip that finished earlier sends no
    # further webhook, which is why each one is also asked for directly here.
    for stream_uid in {c["stream_uid"] for c in clip_docs if c["stream_uid"]}:
        status = await asyncio.to_thread(get_stream_video_status, stream_uid)
        if status.get("readyToStream"):
            await _mark_clip_ready(stream_uid)

    await _start_if_all_ready(job_id)
    return str(job_id)


async def handle_stream_video_ready(stream_uid: str) -> None:
    """Called by the Cloudflare webhook when any Stream video becomes ready."""
    db = get_database()
    await _mark_clip_ready(stream_uid)
    cursor = db.pending_videos.find(
        {"status": STATUS_WAITING, "clips.stream_uid": stream_uid},
        {"_id": 1},
    )
    async for job in cursor:
        await _start_if_all_ready(job["_id"])

    await _delete_clips_once_stitched_video_is_ready(stream_uid)


async def handle_stream_video_failed(stream_uid: str, reason: str) -> None:
    """Called by the Cloudflare webhook when Stream could not process a video."""
    db = get_database()
    await db.pending_videos.update_many(
        {"status": STATUS_WAITING, "clips.stream_uid": stream_uid},
        {"$set": {
            "status": STATUS_FAILED,
            "error": f"Cloudflare could not process clip {stream_uid}: {reason}",
            "updated_at": datetime.utcnow(),
        }},
    )


async def _delete_clips_once_stitched_video_is_ready(stitched_uid: str) -> None:
    """
    Source clips are only removed after Cloudflare has processed the stitched
    video, so a result that fails to process never costs the user their footage.
    """
    db = get_database()
    # Claimed in a single update, so a repeated webhook cannot run the cleanup twice.
    job = await db.pending_videos.find_one_and_update(
        {"status": STATUS_COMPLETED, "result.stream_uid": stitched_uid, "clips_cleaned_at": None},
        {"$set": {"clips_cleaned_at": datetime.utcnow()}},
    )
    if job is None:
        return

    outcomes = []
    for stream_uid in dict.fromkeys(c["stream_uid"] for c in job["clips"] if c["stream_uid"]):
        try:
            kept_because = await _reason_to_keep_clip(stream_uid, job["_id"])
            if kept_because is None:
                await asyncio.to_thread(delete_stream_video, stream_uid)
        except Exception as e:
            logger.exception("Could not clean up clip %s of stitch job %s", stream_uid, job["_id"])
            kept_because = f"error: {e}"
        outcomes.append({"stream_uid": stream_uid, "deleted": kept_because is None, "kept_because": kept_because})

    await db.pending_videos.update_one({"_id": job["_id"]}, {"$set": {"clip_cleanup": outcomes}})


async def _reason_to_keep_clip(stream_uid: str, job_id: ObjectId) -> Optional[str]:
    db = get_database()

    name = await asyncio.to_thread(get_stream_video_name, stream_uid)
    if name is None:
        return "already gone from Cloudflare"
    if not name.startswith(CLIP_NAME_PREFIX):
        return f"not uploaded as a clip (its name does not start with {CLIP_NAME_PREFIX})"

    if await db.videos.find_one({"remoteUrl_CF": {"$regex": re.escape(stream_uid)}}, {"_id": 1}):
        return "it is the video of a post"

    other_job = await db.pending_videos.find_one(
        {
            "_id": {"$ne": job_id},
            "status": {"$in": [STATUS_WAITING, STATUS_PROCESSING]},
            "clips.stream_uid": stream_uid,
        },
        {"_id": 1},
    )
    if other_job:
        return "another stitch job still needs it"

    return None


async def _mark_clip_ready(stream_uid: str) -> None:
    db = get_database()
    await db.pending_videos.update_many(
        {"status": STATUS_WAITING, "clips.stream_uid": stream_uid},
        {"$set": {"clips.$[clip].ready": True, "updated_at": datetime.utcnow()}},
        array_filters=[{"clip.stream_uid": stream_uid}],
    )


async def _start_if_all_ready(job_id: ObjectId) -> None:
    db = get_database()
    # Moving the job from waiting to processing in a single update means only
    # one caller can start it, even if two webhooks arrive together.
    job = await db.pending_videos.find_one_and_update(
        {"_id": job_id, "status": STATUS_WAITING, "clips.ready": {"$ne": False}},
        {"$set": {"status": STATUS_PROCESSING, "updated_at": datetime.utcnow()}},
    )
    if job is None:
        return

    task = asyncio.create_task(_run_job(job))
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)


async def _run_job(job: dict) -> None:
    db = get_database()
    try:
        stitched = await asyncio.to_thread(_invoke_edit_video_lambda, _build_lambda_payload(job))
        video_id = await _create_post(job, stitched)
        await db.pending_videos.update_one(
            {"_id": job["_id"]},
            {"$set": {
                "status": STATUS_COMPLETED,
                "result": {
                    "stream_uid": stitched["stream_uid"],
                    "file_url": stitched["file_url"],
                    "video_id": video_id,
                },
                "updated_at": datetime.utcnow(),
            }},
        )
    except Exception as e:
        logger.exception("Stitch job %s failed", job["_id"])
        await db.pending_videos.update_one(
            {"_id": job["_id"]},
            {"$set": {"status": STATUS_FAILED, "error": str(e), "updated_at": datetime.utcnow()}},
        )


def _build_lambda_payload(job: dict) -> dict:
    video_urls = []
    for clip in job["clips"]:
        item = {"url": clip["url"], "start": clip["start"]}
        if clip.get("end") is not None:
            item["end"] = clip["end"]
        video_urls.append(item)

    payload = {"videoUrls": video_urls, "filename": f"stitched_{job['_id']}.mp4"}
    if job.get("audio_url"):
        payload["audio"] = {"uri": job["audio_url"]}
        payload["keepOriginalSound"] = bool(job.get("keep_original_sound"))
    return payload


def _invoke_edit_video_lambda(payload: dict) -> dict:
    client = boto3.client(
        "lambda",
        region_name=settings.EDIT_VIDEO_LAMBDA_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        # A retry would stitch and upload the video a second time, so there are none.
        config=Config(read_timeout=900, connect_timeout=10, retries={"total_max_attempts": 1}),
    )
    response = client.invoke(
        FunctionName=settings.EDIT_VIDEO_LAMBDA_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    body = json.loads(response["Payload"].read())

    if response.get("FunctionError"):
        raise RuntimeError(f"editVideo failed: {body.get('errorMessage', body)}")

    result = json.loads(body["body"])
    if not result.get("stream_uid") or not result.get("file_url"):
        raise RuntimeError(f"editVideo returned an unexpected result: {result}")
    return result


def _requested_duration(job: dict) -> float:
    return float(sum((clip["end"] - clip["start"]) for clip in job["clips"] if clip.get("end") is not None))


async def _create_post(job: dict, stitched: dict) -> str:
    post = job.get("post") or {}
    duration = float(stitched.get("duration") or _requested_duration(job))
    return await create_video_post(
        job["creator_id"],
        remote_url=stitched["file_url"],
        remote_url_cf=stitched["file_url"],
        description=post.get("description"),
        privacy=post.get("privacy") or "public",
        thumbnail=post.get("thumbnail") or DEFAULT_THUMBNAIL,
        duration=duration,
        end=duration,
        # The Cloudflare webhook flips this once Stream has processed the stitched video.
        is_ready_to_stream=False,
        width=STITCHED_WIDTH,
        height=STITCHED_HEIGHT,
        comments_enabled=post.get("comments_enabled", True),
        category_id=post.get("categoryId"),
        subcategory_id=post.get("subcategoryId"),
        supports_landscape=False,
    )
