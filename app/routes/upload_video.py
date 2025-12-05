"""
app/routes/upload_video.py
"""

import io

from fastapi import File, UploadFile, HTTPException, APIRouter, Query
from fastapi.responses import JSONResponse

from app.services.upload_DO import (
    upload_to_spaces,
    allowed_file,
    secure_filename,
    get_content_type,
    is_image_file,
    generate_presigned_upload_url,
)
from app.services.cloudflare_stream import (
    upload_file_to_stream,
    get_stream_video_details,
    build_cloudflare_urls,
)

router = APIRouter(prefix="/video", tags=["Upload Video"])


@router.post("/upload")
async def upload_video(
    video: UploadFile = File(...),
    isAudio: bool = False,
    isImage: bool = False,
):
    """
    Unified upload endpoint:

    - If isImage=True or auto-detected as image --> upload to DO Spaces.
    - If isAudio=True --> upload to DO Spaces.
    - Else (default) --> upload directly to Cloudflare Stream as VIDEO.
    """
    if not video.filename:
        raise HTTPException(status_code=400, detail="No file selected")

    if not allowed_file(video.filename):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid file type. Allowed: mp4, avi, mov, wmv, flv, webm, mkv, "
                "mp3, wav, aac, ogg, flac, m4a, jpg, jpeg, png, gif, bmp, tiff, "
                "webp, svg, ico, heic"
            ),
        )

    # Auto-detect image if not explicitly specified
    if not isImage and not isAudio:
        isImage = is_image_file(video.filename)

    filename = secure_filename(video.filename)

    # BRANCH 1: Image or Audio --> keep using DO Spaces
    if isImage or isAudio:
        try:
            # Get file size
            file_size = video.size if hasattr(video, "size") else len(
                await video.read()
            )
            await video.seek(0)

            # Upload to DO Spaces
            success, result, object_key = await upload_to_spaces(
                video,
                filename,
                isAudio=isAudio,
                isImage=isImage,
            )

            if not success:
                raise HTTPException(
                    status_code=500,
                    detail=f"Upload failed: {result}",
                )

            if isImage:
                media_type = "Image"
            elif isAudio:
                media_type = "Audio"
            else:
                media_type = "File"

            return JSONResponse(
                content={
                    "message": f"{media_type} uploaded successfully",
                    "url": result,
                    "filename": filename,
                    "object_key": object_key,
                    "file_size": str(file_size),
                    "content_type": get_content_type(filename),
                    "media_type": media_type.lower(),
                },
                status_code=200,
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Server error (DO upload): {str(e)}",
            )

    # BRANCH 2: Video --> upload directly to Cloudflare Stream
    try:
        # Read contents into memory (simplest approach)
        contents = await video.read()
        file_size = len(contents)
        file_obj = io.BytesIO(contents)

        content_type = video.content_type or get_content_type(filename)

        cf_result = upload_file_to_stream(
            file_obj=file_obj,
            filename=filename,
            content_type=content_type,
            meta={"name": filename},
        )

        uid = cf_result.get("uid")
        if not uid:
            raise HTTPException(
                status_code=500,
                detail="Cloudflare Stream did not return a video UID",
            )

        # Get details from Stream (playback URLs, thumbnail, etc.)
        details = get_stream_video_details(uid)
        urls = build_cloudflare_urls(uid, details)

        # Keep the *shape* somewhat similar to your previous response:
        # - url: use Cloudflare "original" (mp4 download) as primary video URL
        # - object_key: not really meaningful for Stream, but keep field for compatibility
        response_payload = {
            "message": "Video uploaded successfully",
            "url": urls["original"],  # now Cloudflare-based, not DO
            "filename": filename,
            "object_key": uid,  # you can store UID here if something expects a string key
            "file_size": str(file_size),
            "content_type": content_type,
            "media_type": "video",
            "cloudflare_stream_uid": uid,
            "cloudflare_stream": {
                "hls": urls["hls"],
                "dash": urls["dash"],
                "preview": urls["preview"],
                "thumbnail": urls["thumbnail"],
                "download": urls["download"],
                "original": urls["original"],
            },
        }

        return JSONResponse(content=response_payload, status_code=200)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Server error (Cloudflare upload): {str(e)}",
        )


@router.get("/presigned-upload")
async def get_presigned_upload_url(
    filename: str = Query(..., description="Original filename, e.g. myimage.png"),
    folder: str = Query(
        "uploads",
        description="Folder in DO Spaces: e.g. 'images', 'audio', 'uploads'",
    ),
):
    """
    Still useful for direct client uploads of IMAGES / AUDIO to DO Spaces.

    Videos should use the /video/upload route (Cloudflare Stream).
    """
    try:
        result = generate_presigned_upload_url(filename=filename, folder=folder)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
