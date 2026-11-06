#app/routes/upload_video.py


import logging
import os
import tempfile
import time
from typing import Optional
from datetime import datetime
import hashlib
import hmac
import json
from app.services import thumbnails
import re
from fastapi import BackgroundTasks, Depends, File, UploadFile, HTTPException, APIRouter, Query, Request, status
from fastapi.responses import JSONResponse
import httpx
from bson import ObjectId
from pydantic import BaseModel, Field
from app.api.deps import get_verified_user, oauth2_scheme_optional, get_current_user, get_current_active_user
from app.core.config import Settings
from app.core.database import get_database
from app.models.upload_video import CheckStatusReq
from recombee_api_client.api_requests import SetItemValues
from app.services.expo import send_push_message
from app.services.recombee_service import recombee_send
from app.services.sqs_publisher import push_video_processing_job
from app.services.stitch_jobs import handle_stream_video_failed, handle_stream_video_ready
from app.services.upload_DO import generate_cf_tus_upload_url, upload_to_spaces, upload_path_to_spaces, allowed_file, secure_filename, get_content_type, is_image_file, max_upload_size, generate_presigned_upload_url, generate_stream_direct_upload_url, get_stream_video_status
from app.services.video_posts import stream_uid_exact_query
import asyncio
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/video", tags=["Upload Video"])

FIRST_VIDEO_WELCOME_COMMENTER_ID = "6845fdb76cd85e35c8f722f6"
FIRST_VIDEO_WELCOME_COMMENTER_DISPLAY_NAME = "Wano Team"
FIRST_VIDEO_WELCOME_COMMENT = (
    "First video on Wano! \U0001F30D\U0001F525 Every voice adds something. "
    "Thanks for adding yours. Keep creating, keep sharing, keep being you."
)

MAX_PRESIGN_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
UPLOAD_READ_CHUNK = 1024 * 1024
WEBHOOK_TOLERANCE_SECONDS = 300
LAMBDA_SECRET_HEADER = "x-lambda-callback-secret"
_STREAM_UID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")


class PresignRequest(BaseModel):
    filename: str = Field(..., max_length=255)
    fileSize: int = Field(..., gt=0, le=MAX_PRESIGN_FILE_SIZE)
    # Only names the upload in Cloudflare's metadata.
    folder: Optional[str] = Field('videos', max_length=64, pattern=r"^[A-Za-z0-9_-]*$")


def _lambda_secret_ok(request: Request) -> bool:
    """The editVideo Lambda authenticates with a shared secret (LAMBDA_CALLBACK_SECRET),
    not a user token. Unset secret means no Lambda access."""
    expected = os.environ.get("LAMBDA_CALLBACK_SECRET") or ""
    supplied = request.headers.get(LAMBDA_SECRET_HEADER) or ""
    return bool(expected) and hmac.compare_digest(expected.encode(), supplied.encode())


async def get_uploader(request: Request, token: Optional[str] = Depends(oauth2_scheme_optional)) -> str:
    """A verified user, or the editVideo Lambda presenting the shared secret."""
    if _lambda_secret_ok(request):
        return "lambda"
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = await get_current_user(token)
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")
    user_id = await get_current_active_user(user_id, token)
    return await get_verified_user(user_id)


@router.post("/upload")
async def upload_video(
    video: UploadFile = File(...),
    isAudio: bool = False,
    isImage: bool = False,
    current_user: str = Depends(get_verified_user),
):
    # Validate file presence
    if not video.filename:
        raise HTTPException(status_code=400, detail="No file selected")

    filename = secure_filename(video.filename)

    # Validate file type (on both the raw and the cleaned name, since the
    # cleaned one is what the stored content type comes from)
    if not allowed_file(video.filename) or not allowed_file(filename):
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Allowed: mp4, avi, mov, wmv, flv, webm, mkv, mp3, wav, aac, ogg, flac, m4a, jpg, jpeg, png, gif, bmp, tiff, webp, ico, heic"
        )

    # Auto-detect if it's an image file if not explicitly specified
    if not isImage and not isAudio:
        isImage = is_image_file(filename)

    max_size = max_upload_size(filename)
    tmp_path = None
    try:
        # Stream to disk, refusing the upload as soon as it passes the limit,
        # instead of reading an unbounded body into memory.
        file_size = 0
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
            while True:
                chunk = await video.read(UPLOAD_READ_CHUNK)
                if not chunk:
                    break
                file_size += len(chunk)
                if file_size > max_size:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File too large (max {max_size // (1024 * 1024)} MB)",
                    )
                tmp.write(chunk)

        if file_size == 0:
            raise HTTPException(status_code=400, detail="Empty file")

        success, result, object_key = await upload_path_to_spaces(tmp_path, filename, isAudio=isAudio, isImage=isImage)

        if not success:
            logger.error("Spaces upload failed for %s: %s", filename, result)
            raise HTTPException(status_code=500, detail="Upload failed")

        if isImage:
            media_type = "Image"
        elif isAudio:
            media_type = "Audio"
        else:
            media_type = "Video"

        return JSONResponse(
            content={
                'message': f'{media_type} uploaded successfully',
                'url': result,
                'filename': filename,
                'object_key': object_key,
                'file_size': str(file_size),
                'content_type': get_content_type(filename),
                'media_type': media_type.lower()
            },
            status_code=200
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Upload of %s failed", filename)
        raise HTTPException(status_code=500, detail="Server error")
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        try:
            await video.close()
        except Exception:
            pass


@router.post("/v2/presigned-upload")
async def get_presigned_upload_url_v2(
    payload: PresignRequest,
    uploader: str = Depends(get_uploader),
):
    """
    Get a Cloudflare Stream TUS upload URL. Called by the app (user token) and
    by the editVideo Lambda (shared-secret header).
    """
    try:
        result = await asyncio.to_thread(
            generate_cf_tus_upload_url,
            filename=payload.filename,
            fileSize=payload.fileSize,
            folder=payload.folder or "videos",
        )
        return {
            "status": 200,
            "message": "Pre-signed upload URL generated successfully",
            "data": result
        }
    except Exception:
        logger.exception("Could not create TUS upload URL")
        raise HTTPException(status_code=500, detail="Could not create upload URL")


@router.post("/presigned-upload")
async def get_presigned_upload_url(
    payload: PresignRequest,
    current_user: str = Depends(get_verified_user),
):
    """
    Get a Cloudflare Stream upload URL (TUS for large files, direct upload otherwise).
    """
    try:
        MAX_FILE_SIZE = 200 * 1024 * 1024
        folder = payload.folder or "videos"

        if payload.fileSize >= MAX_FILE_SIZE:
            result = await asyncio.to_thread(
                generate_cf_tus_upload_url, filename=payload.filename, fileSize=payload.fileSize, folder=folder
            )
        else:
            result = await asyncio.to_thread(
                generate_stream_direct_upload_url, filename=payload.filename, folder=folder
            )
        return {
            "status": 200,
            "message": "Pre-signed upload URL generated successfully",
            "data": result
        }
    except Exception:
        logger.exception("Could not create upload URL")
        raise HTTPException(status_code=500, detail="Could not create upload URL")


async def call_video_service_check_status_api(uId: str, videoId: str):
        """
        Call the second backend's GraphQL API
        """
        # GraphQL endpoint URL of your second backend
        SERVICE_API_URL = settings.VIDEO_SERVICE_URL  # Update this URL
        
        # Prepare the GraphQL mutation query
        
        
        mutation = """
        mutation CheckStreamStatus($input: VideoStatusInput!) {
            checkStreamStatus(input: $input)
        }
        """
        
        # Convert the input to include user_id
        variables = {
            "input": {
                "videoId":videoId,
                "uId": uId
            }
        }
        
        # Prepare the request payload
        payload = {
            "query": mutation,
            "variables": variables
        }
        
        # Make the HTTP request to the second backend with longer timeout
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    SERVICE_API_URL,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        # Add any authentication headers if needed
                        # "Authorization": f"Bearer {token}",
                    },
                    timeout=600.0  # 10 minutes timeout for long-running operations
                )
                
                # Check if the request was successful
                response.raise_for_status()
                
                # Parse the response
                response_data = response.json()
                
                # Check for GraphQL errors
                if "errors" in response_data:
                    error_messages = [error.get("message", "Unknown error") for error in response_data["errors"]]
                    raise Exception(f"GraphQL errors: {', '.join(error_messages)}")
                
                # Extract the data from the response
                if "data" in response_data and "checkStreamStatus" in response_data["data"]:
                    print("Response Data:", response_data["data"]["checkStreamStatus"])
                    return response_data["data"]["checkStreamStatus"]
                else:
                    raise Exception("Invalid response format from service API")
                    
            except httpx.TimeoutException:
                raise Exception("Request to service API timed out")
            except httpx.HTTPStatusError as e:
                raise Exception(f"HTTP error from service API: {e.response.status_code}")
            except Exception as e:
                raise Exception(f"Error calling service API: {str(e)}")


async def call_video_service_check_status_api_background(uId: str, videoId: str):
        """
        Background task to call the second backend's GraphQL API
        This runs independently and doesn't block the main response
        """
        try:
            print("Starting background video compilation task for u_id", uId)
            await call_video_service_check_status_api(uId, videoId)
            # Optionally: Store success status in database, send notification, etc.
            print(f"Video status completed successfully for uid: {uId}")
            
        except Exception as e:
            # Handle errors in background task
            # Optionally: Store error status in database, send error notification, etc.
            print(f"Video compilation failed for uid {uId}: {str(e)}")
            # You might want to log this properly or store in database
    
@router.get("/{uid}")
async def video_status(
    uid: str,
    current_user: str = Depends(get_verified_user),
):
    if not _STREAM_UID_RE.match(uid):
        raise HTTPException(status_code=400, detail="Invalid video UID")
    try:
        return await asyncio.to_thread(get_stream_video_status, uid)
    except Exception:
        logger.exception("Could not fetch Stream status for %s", uid)
        raise HTTPException(status_code=500, detail="Could not fetch video status")


@router.post('/check-stream-status')
async def check_stream_status(
    input: CheckStatusReq,
    current_user: str = Depends(get_verified_user),
):
    """
    Queue a check of a Cloudflare Stream video's processing status. Only the
    creator of `videoId` may ask; the worker then updates that video.
    """
    if not ObjectId.is_valid(input.videoId):
        raise HTTPException(status_code=400, detail="Invalid video ID")
    if not _STREAM_UID_RE.match(input.uId or ""):
        raise HTTPException(status_code=400, detail="Invalid video UID")

    db = get_database()
    video = await db.videos.find_one(
        {"_id": ObjectId(input.videoId), "creator_id": ObjectId(current_user)},
        {"_id": 1},
    )
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    try:
        # boto3 blocks; keep it off the event loop.
        await asyncio.to_thread(
            push_video_processing_job,
            videoId=input.videoId,
            uId=input.uId,
        )
        return {"message": "Video status check initiated in background"}
    except Exception:
        logger.exception("Could not queue status check for video %s", input.videoId)
        raise HTTPException(status_code=500, detail="Could not start status check")


def _verify_cf_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    Verify Cloudflare Stream's webhook signature header, formatted as
    "time=<unix_ts>,sig1=<hex_hmac_sha256>". See Cloudflare Stream webhook docs.
    """
    secret = getattr(settings, "CLOUDFLARE_STREAM_WEBHOOK_SECRET", None) or ""
    if not secret or not signature_header:
        return False

    parts = dict(p.split("=", 1) for p in signature_header.split(",") if "=" in p)
    timestamp = parts.get("time")
    signature = parts.get("sig1")
    if not timestamp or not signature:
        return False

    # Reject replays of old (or future-dated) deliveries.
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - sent_at) > WEBHOOK_TOLERANCE_SECONDS:
        return False

    try:
        signed_payload = f"{timestamp}.{raw_body.decode()}".encode()
    except UnicodeDecodeError:
        return False
    expected = hmac.new(
        secret.encode(),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _later(background_tasks, fn, *args):
    """Run after the response when there is one (webhook), otherwise now in the
    background (stuck-upload rescue)."""
    if background_tasks is not None:
        background_tasks.add_task(fn, *args)
    else:
        async def _run():
            try:
                if asyncio.iscoroutinefunction(fn):
                    await fn(*args)
                else:
                    # Blocking helpers (send_push_message uses requests) run off the event loop.
                    res = await asyncio.to_thread(fn, *args)
                    if asyncio.iscoroutine(res):
                        await res
            except Exception:
                logger.exception("Background task %s failed", getattr(fn, "__name__", fn))
        asyncio.ensure_future(_run())


async def mark_stream_ready(uid: str, background_tasks=None):
    """A Stream video finished processing: mark its post ready (once), give it
    a real thumbnail, tell Recombee and the creator. Returns the post, or None
    if no post was waiting on this video. Used by the webhook and the
    stuck-upload rescue."""
    db = get_database()
    # isReadyToStream: {"$ne": True} makes this idempotent - Cloudflare may retry
    # the same webhook, and we only want to notify the creator once.
    # Exact stream_uid first; older videos without one fall back to an
    # anchored match on the UID as the playback URL's first path segment.
    video = await db.videos.find_one_and_update(
        {"stream_uid": uid, "isReadyToStream": {"$ne": True}},
        {"$set": {"isReadyToStream": True}},
        projection={"creator_id": 1, "urls.thumbnail": 1, "status": 1, "remoteUrl_CF": 1, "remoteUrl": 1,
                    "media_type": 1, "images": 1},
    )
    if not video:
        legacy_query = stream_uid_exact_query(uid)
        legacy_query["$or"] = legacy_query["$or"][1:]
        video = await db.videos.find_one_and_update(
            {**legacy_query, "isReadyToStream": {"$ne": True}},
            {"$set": {"isReadyToStream": True}},
            projection={"creator_id": 1, "urls.thumbnail": 1, "status": 1, "remoteUrl_CF": 1, "remoteUrl": 1,
                    "media_type": 1, "images": 1},
        )
    if not video:
        return None

    # Rescued after the stuck-upload check gave up on it (or while it was checking).
    await db.videos.update_one({"_id": video["_id"], "upload_check": {"$exists": True}},
                               {"$set": {"upload_check.state": "rescued"}})

    # Posts saved with the grey placeholder get Cloudflare's own frame.
    urls = video.get("urls") if isinstance(video.get("urls"), dict) else {}
    fixed = thumbnails.best_thumbnail(video)
    if fixed and fixed != urls.get("thumbnail"):
        await db.videos.update_one({"_id": video["_id"]}, {"$set": {"urls.thumbnail": fixed}})
        video["urls"] = {**urls, "thumbnail": fixed}

    # A scheduled post is announced, synced and notified when it is published,
    # not when its video finishes processing.
    if video.get("status") in ("scheduled", "publishing"):
        return video

    try:
        req = SetItemValues(str(video["_id"]), {"is_ready_to_stream": True}, cascade_create=True)
        req.timeout = 5000
        await recombee_send(req)
    except Exception as e:
        logger.warning("Failed to set is_ready_to_stream on Recombee item %s: %s", video["_id"], e)

    creator = await db.users.find_one(
        {"_id": video["creator_id"]},
        {"expo_push_tokens": 1},
    )
    tokens = (creator or {}).get("expo_push_tokens") or []
    thumbnail_url = (video.get("urls") or {}).get("thumbnail")

    if str(video["creator_id"]) != FIRST_VIDEO_WELCOME_COMMENTER_ID:
        total_videos = await db.videos.count_documents({"creator_id": video["creator_id"]})
        # This is the video that just became ready, so 1 total means it's their first.
        # Counting is_active-agnostic on purpose - once welcomed, always welcomed,
        # even if this first video later gets deleted.
        if total_videos == 1:
            welcome_comment = await db.comments.insert_one({
                "video_id": video["_id"],
                "user_id": ObjectId(FIRST_VIDEO_WELCOME_COMMENTER_ID),
                "user_display_name": FIRST_VIDEO_WELCOME_COMMENTER_DISPLAY_NAME,
                "content": FIRST_VIDEO_WELCOME_COMMENT,
                "parent_id": None,
                "likes_count": 0,
                "replies_count": 0,
                "liked_by": [],
                "is_edited": False,
                "edited_at": None,
                "is_pinned": False,
                "is_hearted": False,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
                "is_active": True,
            })
            await db.videos.update_one(
                {"_id": video["_id"]},
                {"$inc": {"comments_count": 1}},
            )

            # Same notification + push shape create_comment uses for a real
            # user's comment (app/api/v1/comments.py) - recipient is always the
            # creator here since we already skip when they *are* the commenter.
            await db.notifications.insert_one({
                "recipient_id": video["creator_id"],
                "type": "comment",
                "user_id": ObjectId(FIRST_VIDEO_WELCOME_COMMENTER_ID),
                "post_id": video["_id"],
                "comment_id": welcome_comment.inserted_id,
                "date": datetime.utcnow(),
            })

            comment_preview = FIRST_VIDEO_WELCOME_COMMENT[:30] + (
                "..." if len(FIRST_VIDEO_WELCOME_COMMENT) > 30 else ""
            )
            for token in tokens:
                _later(background_tasks, 
                    send_push_message,
                    token,
                    comment_preview,
                    {"video_id": str(video["_id"]), "commenter_id": FIRST_VIDEO_WELCOME_COMMENTER_ID},
                    f"{FIRST_VIDEO_WELCOME_COMMENTER_DISPLAY_NAME} commented on your video",
                    thumbnail_url,
                )

    for token in tokens:
        _later(background_tasks, 
            send_push_message,
            token,
            "Your video has been successfully uploaded!",
            {"screen": "profile_v2"},
            None,
            thumbnail_url,
        )

    await db.notifications.insert_one({
        "recipient_id": video["creator_id"],
        "type": "video_ready",
        "post_id": video["_id"],
        "date": datetime.utcnow(),
    })
    return video


@router.post("/cf-webhook")
async def cloudflare_stream_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Cloudflare Stream calls this when a video's processing state changes.
    Once a video first becomes ready to stream, notify its creator.
    """
    raw_body = await request.body()
    signature_header = request.headers.get("webhook-signature", "")

    if not _verify_cf_webhook_signature(raw_body, signature_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")
    uid = payload.get("uid")
    if uid is not None and (not isinstance(uid, str) or not _STREAM_UID_RE.match(uid)):
        return {"ok": True}
    ready = payload.get("readyToStream", False)

    if uid:
        clip_status = payload.get("status") or {}
        if ready:
            await handle_stream_video_ready(uid)
        elif clip_status.get("state") == "error":
            await handle_stream_video_failed(
                uid,
                clip_status.get("errorReasonText") or clip_status.get("errorReasonCode") or "unknown error",
            )

    if not uid or not ready:
        return {"ok": True}

    await mark_stream_ready(uid, background_tasks)
    return {"ok": True}
