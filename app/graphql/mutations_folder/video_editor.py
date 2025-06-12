"""

app/graphql/mutations_folder/video_editor.py

"""


import time
import strawberry
from typing import Optional
from bson import ObjectId
from datetime import datetime
from app.graphql.types import VideoType, VideoTypeEnum, VideoPrivacyEnum
from app.graphql.inputs.video_editor import CompileVideoInput
from app.services.upload_DO import upload_to_spaces
from app.services.video_editor import (
    change_video_ratio, convert_image_to_video, stitch_videos, 
    trim_video, add_audio_to_video, download_file_from_url
)
from app.core.database import get_database
import tempfile
import os
from fastapi import HTTPException
from app.services.upload_DO import allowed_file, secure_filename, get_content_type
import shutil

async def process_and_upload_video(stitched_video_path, local_paths, upload_to_spaces):
    # Generate filename for the compiled video
    compiled_filename = f"compiled_video_{int(time.time())}.mp4"
    
    # Validate file type
    if not allowed_file(compiled_filename):
        raise HTTPException(
            status_code=400, 
            detail="Invalid file type. Allowed: mp4, avi, mov, wmv, flv, webm, mkv"
        )
    
    secure_compiled_filename = secure_filename(compiled_filename)
    
    # Read the stitched video file and create an UploadFile-like object
    with open(stitched_video_path, 'rb') as video_file:
        video_content = video_file.read()
        
    # Create a temporary file-like object for upload
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
    temp_file.write(video_content)
    temp_file.seek(0)
    
    # Create an UploadFile object for the upload function
    class MockUploadFile:
        def __init__(self, file_path, filename):
            self.filename = filename
            self.size = os.path.getsize(file_path)
            self._file = open(file_path, 'rb')
        
        async def read(self, size=-1):
            return self._file.read(size)
        
        async def seek(self, offset, whence=0):
            return self._file.seek(offset, whence)
        
        def close(self):
            self._file.close()
    
    mock_video = MockUploadFile(temp_file.name, secure_compiled_filename)
    
    # Upload to Spaces
    success, result, object_key = await upload_to_spaces(mock_video, secure_compiled_filename)
    
    if not success:
        raise HTTPException(status_code=500, detail=f"Upload failed: {result}")
    
    # Clean up temporary files
    mock_video.close()
    os.unlink(temp_file.name)
    
    # Clean up downloaded video files
    for path in local_paths:
        if os.path.exists(path):
            os.unlink(path)
    
    # Clean up stitched video if different from original
    if stitched_video_path not in local_paths and os.path.exists(stitched_video_path):
        os.unlink(stitched_video_path)
    
    return result

@strawberry.type
class VideoEditorMutation:
    @strawberry.mutation
    async def compile_video(self, info, input: CompileVideoInput) -> VideoType:
        try:
            # Check authentication
            user_id = info.context.get("user_id")
            if not user_id:
                raise Exception("Authentication required")
            
            db = get_database()
            
            # Get user info and check verification
            user = await db.users.find_one({"_id": ObjectId(user_id)})
            # if not user.get("is_verified", False):
            #     raise Exception("Please verify your email before uploading videos")
            
            # Download all videos/photos locally and map by index
            local_paths = {}
            
            print(f"Compiling video with input: {input}")

            for video in input.video:
                idx = video.index  # Assuming 'index' is provided in each video input
                if video.type == 'photo':
                    local_path_image = await download_file_from_url(video.remoteUrl, temp_dir="temp_photos")
                    local_path = await convert_image_to_video(local_path_image, video.duration)
                    local_paths[idx] = local_path
                else:
                    local_path = await download_file_from_url(video.remoteUrl, temp_dir="temp_videos")
                    if video.isTrimmed:
                        trimmed_video_path = await trim_video(local_path, video.start, video.end)
                        print(f"Trimmed video path: {trimmed_video_path}")
                        local_paths[idx] = trimmed_video_path
                    else:
                        local_paths[idx] = local_path

            # Sort local_paths by index for stitching
            ordered_paths = [local_paths[i] for i in sorted(local_paths.keys())]
            
            print(f"Ordered video paths: {ordered_paths}")
            
            # Stitch videos together
            if len(ordered_paths) == 0:
                raise HTTPException(status_code=400, detail="No videos provided")
            
            if len(ordered_paths) == 1:
                # Only one video, no stitching needed
                stitched_video_path = ordered_paths[0]
            else:
                # Stitch multiple videos in order
                stitched_video_path = ordered_paths[0]
                for i in range(1, len(ordered_paths)):
                    stitched_video_path = await stitch_videos(stitched_video_path, ordered_paths[i])
                    
            if input.audio_url:
                # Add audio to the stitched video
                temp_audio_path = await download_file_from_url(input.audio_url, temp_dir="temp_audio")
                stitched_video_path = await add_audio_to_video(stitched_video_path, temp_audio_path)
            
            if input.ratio:
                # Resize the video to the specified ratio
                stitched_video_path = await change_video_ratio(stitched_video_path, new_ratio=input.ratio)
            
            # Upload and get URL
            result = await process_and_upload_video(stitched_video_path, ordered_paths, upload_to_spaces)
            
            # Calculate total duration
            total_duration = sum(video.duration for video in input.video)
            
            print(f"Video compiled and uploaded successfully: {result}")

            # Create video document in database
            video_doc = {
                "creator_id": ObjectId(user_id),
                "title": None,  # Can be updated later by user
                "description": None,
                "video_type": "regular",
                "privacy": input.videoType,
                "metadata": {
                    "duration": total_duration,
                    "width": 1080,  # You might want to detect this from the actual video
                    "height": 1920,
                    "fps": 30.0,
                    "file_size": 0  # You can calculate this during upload
                },
                "urls": {
                    "original": result,
                    "hls_playlist": result,  # In production, generate HLS separately
                    "thumbnail": result,  # In production, generate thumbnail separately
                    "download": result
                },
                # Additional fields for compatibility
                "FEid": None,
                "start": 0,
                "end": total_duration,
                "duration": total_duration,
                "remoteUrl": result,
                "type": 'video',
                # Standard fields
                "hashtags": [],
                "categories": [],
                "remix_enabled": True,
                "comments_enabled": True,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
                "is_active": True,
                "views_count": 0,
                "likes_count": 0,
                "comments_count": 0,
                "shares_count": 0,
                "bookmarks_count": 0,
                "is_approved": True,
                "is_flagged": False,
                "report_count": 0,
                "is_remix": False,
                "remix_count": 0,
                "country": user.get("localization", {}).get("country", "NG"),
                "language": user.get("localization", {}).get("languages", ["en"])[0]
            }
            
            # Insert into database
            result_insert = await db.videos.insert_one(video_doc)
            
            # Update user's video count
            await db.users.update_one(
                {"_id": ObjectId(user_id)},
                {"$inc": {"videos_count": 1}}
            )
            
            # Clean up temp directories if they exist
            for temp_dir in ["temp_audio", "temp_videos", "temp_photos", "output"]:
                if os.path.exists(temp_dir) and os.path.isdir(temp_dir):
                    try:
                        shutil.rmtree(temp_dir)
                    except Exception as cleanup_err:
                        print(f"Failed to remove {temp_dir}: {cleanup_err}")
            
            # Return VideoType from your existing types
            return VideoType(
                id=str(result_insert.inserted_id),
                creator_id=str(video_doc["creator_id"]),
                title=video_doc.get("title"),
                remoteUrl=result,
                type= video_doc["type"],
                duration=video_doc["duration"],
                start=video_doc["start"],
                end=video_doc["end"],
                description=video_doc.get("description"),
                video_type=VideoTypeEnum(video_doc["video_type"]),
                privacy=VideoPrivacyEnum(video_doc["privacy"]),
                views_count=video_doc.get("views_count", 0),
                likes_count=video_doc.get("likes_count", 0),
                comments_count=video_doc.get("comments_count", 0),
                shares_count=video_doc.get("shares_count", 0),
                hashtags=video_doc.get("hashtags", []),
                categories=video_doc.get("categories", []),
                created_at=video_doc["created_at"],
                buffered_views=0,
                buffered_likes=0,
                buffered_comments=0
            )
            
        except HTTPException:
            # Re-raise HTTP exceptions
            raise
        except Exception as e:
            # Clean up any temporary files in case of error
            try:
                if 'local_paths' in locals():
                    for path in local_paths.values():
                        if os.path.exists(path):
                            os.unlink(path)
            except:
                pass
            
            raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")