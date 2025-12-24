#app/routes/upload_video.py


from fastapi import File, UploadFile, HTTPException, APIRouter, Query
from fastapi.responses import JSONResponse
import httpx
from app.core.config import Settings
from app.core.database import get_database
from app.models.upload_video import CheckStatusReq
from app.services.upload_DO import upload_to_spaces, allowed_file, secure_filename, get_content_type, is_image_file, generate_presigned_upload_url, generate_stream_direct_upload_url, get_stream_video_status
import asyncio
from app.core.config import settings
router = APIRouter(prefix="/video", tags=["Upload Video"])

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
    
@router.get("/presigned-upload")
async def get_presigned_upload_url(
    filename: str = Query(..., description="Original filename, e.g. myvideo.mp4"),
    folder: str = Query("videos", description="Folder in DO Spaces: videos, audio, profile-pictures")
):
    """
    Get a pre-signed upload URL for DigitalOcean Spaces.
    Automatically determines content type.
    """
    try:
        result = generate_stream_direct_upload_url(filename=filename, folder=folder)
        return result
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
        # Start background task to call the video service API
        asyncio.create_task(call_video_service_check_status_api_background(uId, videoId))
        return {"message": "Video status check initiated in background"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
