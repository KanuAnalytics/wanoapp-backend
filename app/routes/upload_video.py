#app/routes/upload_video.py


from fastapi import File, UploadFile, HTTPException, APIRouter, Query
from fastapi.responses import JSONResponse
from fastapi import BackgroundTasks  # NEW
from bson import ObjectId
from app.services.transcode_hls import transcode_and_publish  # NEW
from app.services.upload_DO import upload_to_spaces, allowed_file, secure_filename, get_content_type, is_image_file, generate_presigned_upload_url
router = APIRouter(prefix="/video", tags=["Upload Video"])

@router.post("/upload")
async def upload_video(
    video: UploadFile = File(...),
    isAudio: bool = False,
    isImage: bool = False,
    bg: BackgroundTasks = None,
):
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
            # === Start HLS transcode non-blocking ===
            video_id = str(ObjectId())  # MongoDB Atlas–friendly ID string

            def job():
                try:
                    out = transcode_and_publish(object_key, video_id)
                    # TODO: Persist to DB here (when you have the full Video payload ready):
                    # - urls.hls_playlist = out["master_url"]
                    # - urls.thumbnail = out["poster_url"]
                    # - urls.original = result
                    # - processing_status = "READY"
                except Exception as e:
                    import traceback, logging
                    logging.exception("HLS transcode failed: %s", e)


            if bg:
                bg.add_task(job)


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
                    'media_type': media_type.lower(),
                    'video_id': video_id,  # NEW
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
        result = generate_presigned_upload_url(filename=filename, folder=folder)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))