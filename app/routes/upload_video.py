#app/routes/upload_video.py


from typing import Optional
from datetime import datetime
import hashlib
import hmac
import json
import re
from fastapi import BackgroundTasks, Depends, File, UploadFile, HTTPException, APIRouter, Query, Request, status
from fastapi.responses import JSONResponse
import httpx
from bson import ObjectId
from app.api.deps import get_verified_user
from app.core.config import Settings
from app.core.database import get_database
from app.models.upload_video import CheckStatusReq
from recombee_api_client.api_requests import SetItemValues
from app.services.expo import send_push_message
from app.services.recombee_service import recombee_send
from app.services.sqs_publisher import push_video_processing_job
from app.services.upload_DO import generate_cf_tus_upload_url, upload_to_spaces, allowed_file, secure_filename, get_content_type, is_image_file, generate_presigned_upload_url, generate_stream_direct_upload_url, get_stream_video_status
import asyncio
from app.core.config import settings
from pydantic import BaseModel

router = APIRouter(prefix="/video", tags=["Upload Video"])

FIRST_VIDEO_WELCOME_COMMENTER_ID = "6845fdb76cd85e35c8f722f6"
FIRST_VIDEO_WELCOME_COMMENTER_DISPLAY_NAME = "Wano Team"
FIRST_VIDEO_WELCOME_COMMENT = (
    "First video on Wano! \U0001F30D\U0001F525 Every voice adds something. "
    "Thanks for adding yours. Keep creating, keep sharing, keep being you."
)

class PresignRequest(BaseModel):
    filename: str
    fileSize: int
    folder: Optional[str] = 'videos'

@router.post("/upload")
async def upload_video(video: UploadFile = File(...), isAudio: bool = False, isImage: bool = False):
    # Validate file presence
    if not video.filename:
        raise HTTPException(status_code=400, detail="No file selected")
    
    # Validate file type
    if not allowed_file(video.filename):
        raise HTTPException(
            status_code=400, 
            detail="Invalid file type. Allowed: mp4, avi, mov, wmv, flv, webm, mkv, mp3, wav, aac, ogg, flac, m4a, jpg, jpeg, png, gif, bmp, tiff, webp, svg, ico, heic"
        )
    
    # Auto-detect if it's an image file if not explicitly specified
    if not isImage and not isAudio:
        isImage = is_image_file(video.filename)
    
    filename = secure_filename(video.filename)
    
    try:
        # Get file size
        file_size = video.size if hasattr(video, 'size') else len(await video.read())
        await video.seek(0)  # Reset file pointer
        
        # Upload to Spaces
        success, result, object_key = await upload_to_spaces(video, filename, isAudio=isAudio, isImage=isImage)
        
        if success:
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
        else:
            raise HTTPException(status_code=500, detail=f"Upload failed: {result}")
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")
@router.post("/v2/presigned-upload")
async def get_presigned_upload_url(
    payload: PresignRequest,
):
    """
    Get a pre-signed upload URL for DigitalOcean Spaces.
    Automatically determines content type.
    """
    try:
        MAX_FILE_SIZE = 200 * 1024 * 1024
        
        # if(payload.fileSize >= MAX_FILE_SIZE):
        result = generate_cf_tus_upload_url(filename=payload.filename, fileSize=payload.fileSize, folder=payload.folder)
        # else:
        #     result =  generate_stream_direct_upload_url(filename=payload.filename, folder=payload.folder)
        return {
            "status": 200,
            "message": "Pre-signed upload URL generated successfully",
            "data": result
        }
    except Exception as e:
        print(str(e))
        raise HTTPException(status_code=500, detail=str(e))
    
@router.post("/presigned-upload")
async def get_presigned_upload_url(
    payload: PresignRequest,
):
    """
    Get a pre-signed upload URL for DigitalOcean Spaces.
    Automatically determines content type.
    """
    try:
        MAX_FILE_SIZE = 200 * 1024 * 1024
        
        if(payload.fileSize >= MAX_FILE_SIZE):
            result = generate_cf_tus_upload_url(filename=payload.filename, fileSize=payload.fileSize, folder=payload.folder)
        else:
            result =  generate_stream_direct_upload_url(filename=payload.filename, folder=payload.folder)
        return {
            "status": 200,
            "message": "Pre-signed upload URL generated successfully",
            "data": result
        }
    except Exception as e:
        print(str(e))
        raise HTTPException(status_code=500, detail=str(e))
    
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
def video_status(
    uid:str
    ):
    try:
        print("Fetching status for UID:", uid)
        status = get_stream_video_status(uid)
        print("Status fetched:", status)
        return status
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@router.post('/check-stream-status')
async def check_stream_status(input: CheckStatusReq):
    """
    Check the processing status of a Cloudflare Stream video by its UID.
    Polls until readyToStream is true, then updates the videos collection.
    """
    try:
        uId = input.uId
        videoId = input.videoId
        print("Initiating status check for UID:", uId)

        push_video_processing_job(
            videoId=videoId,
            uId=uId,
        )
        # Start background task to call the video service API
        # asyncio.create_task(call_video_service_check_status_api_background(uId, videoId))
        return {"message": "Video status check initiated in background"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _verify_cf_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    Verify Cloudflare Stream's webhook signature header, formatted as
    "time=<unix_ts>,sig1=<hex_hmac_sha256>". See Cloudflare Stream webhook docs.
    """
    if not signature_header:
        return False

    parts = dict(p.split("=", 1) for p in signature_header.split(",") if "=" in p)
    timestamp = parts.get("time")
    signature = parts.get("sig1")
    if not timestamp or not signature:
        return False

    signed_payload = f"{timestamp}.{raw_body.decode()}".encode()
    expected = hmac.new(
        settings.CLOUDFLARE_STREAM_WEBHOOK_SECRET.encode(),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


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

    payload = json.loads(raw_body)
    uid = payload.get("uid")
    ready = payload.get("readyToStream", False)

    if not uid or not ready:
        return {"ok": True}

    db = get_database()
    # isReadyToStream: {"$ne": True} makes this idempotent - Cloudflare may retry
    # the same webhook, and we only want to notify the creator once.
    video = await db.videos.find_one_and_update(
        {"remoteUrl_CF": {"$regex": re.escape(uid)}, "isReadyToStream": {"$ne": True}},
        {"$set": {"isReadyToStream": True}},
        projection={"creator_id": 1, "urls.thumbnail": 1},
    )
    if not video:
        return {"ok": True}

    try:
        req = SetItemValues(str(video["_id"]), {"is_ready_to_stream": True}, cascade_create=True)
        req.timeout = 5000
        await recombee_send(req)
    except Exception as e:
        print(f"Failed to set is_ready_to_stream on Recombee item {video['_id']}: {e}")

    if str(video["creator_id"]) != FIRST_VIDEO_WELCOME_COMMENTER_ID:
        total_videos = await db.videos.count_documents({"creator_id": video["creator_id"]})
        # This is the video that just became ready, so 1 total means it's their first.
        # Counting is_active-agnostic on purpose - once welcomed, always welcomed,
        # even if this first video later gets deleted.
        if total_videos == 1:
            await db.comments.insert_one({
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

    creator = await db.users.find_one(
        {"_id": video["creator_id"]},
        {"expo_push_tokens": 1},
    )
    tokens = (creator or {}).get("expo_push_tokens") or []
    thumbnail_url = (video.get("urls") or {}).get("thumbnail")

    for token in tokens:
        background_tasks.add_task(
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

    return {"ok": True}
