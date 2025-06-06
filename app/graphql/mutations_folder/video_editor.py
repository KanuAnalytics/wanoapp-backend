import time
import strawberry
from typing import Optional
from uuid import UUID
from app.models.video_editor import VideoModelForEditor
from app.graphql.types_folder.video_editor import Video
from app.graphql.inputs.video_editor import CompileVideoInput
from app.services.upload_DO import upload_to_spaces
from app.services.video_editor import change_video_ratio, convert_image_to_video, stitch_videos, trim_video, add_audio_to_video,download_file_from_url
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
    
    return  result

@strawberry.type
class VideoEditorMutation:
    @strawberry.mutation
    async def compile_video(self, input: CompileVideoInput) -> Video:
        try:
            
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
                # Assuming input.ratio is a string like "16:9"
                # width, height = map(int, input.ratio.split(':'))
                stitched_video_path = await change_video_ratio(stitched_video_path, new_ratio=input.ratio)
            
            # Generate filename for the compiled video
            result = await process_and_upload_video(stitched_video_path, ordered_paths, upload_to_spaces)
            # Prepare video data for database
            
            total_duration = sum(video.duration for video in input.video)
            
            
            
            print(f"Video compiled and uploaded successfully: {result}")

            video_data = {
                "remoteUrl": result,
                "duration": total_duration,
                "start": 0,  # Assuming start is 0 for the compiled video
                "end": total_duration,  # Assuming end is total duration for the compiled video
                "type": 'video'
                # Add other fields as necessary
            }
            
            # Clean up temp directories if they exist
            for temp_dir in ["temp_audio", "temp_videos", "temp_photos", "output"]:
                if os.path.exists(temp_dir) and os.path.isdir(temp_dir):
                    try:
                        shutil.rmtree(temp_dir)
                    except Exception as cleanup_err:
                        print(f"Failed to remove {temp_dir}: {cleanup_err}")
            
            # Create and save the video record
            video_record = VideoModelForEditor(**video_data)
            # await video_record.insert()
            
            return Video.from_pydantic(video_record)
            # return video_data
            
        except HTTPException:
            # Re-raise HTTP exceptions
            raise
        except Exception as e:
            # Clean up any temporary files in case of error
            try:
                if 'local_paths' in locals():
                    for path in local_paths:
                        if os.path.exists(path):
                            os.unlink(path)
            except:
                pass
            
            raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")
