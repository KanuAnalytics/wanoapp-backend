#app/services/video_editor.py


from moviepy import VideoFileClip, concatenate_videoclips, CompositeVideoClip, CompositeAudioClip, ImageClip, vfx
import os
import tempfile
import requests
from urllib.parse import urlparse
from typing import Optional
import logging
from fastapi import HTTPException
import uuid

logger = logging.getLogger(__name__)

async def convert_image_to_video(
    image_path: str, 
    duration: int = 5, 
    output_dir: Optional[str] = "temp_videos",
) -> str:
    """
    Converts an image to a video with a specified duration.
    
    Parameters:
    - image_path (str): Path to the input image file.
    - duration (int): Duration of the output video in seconds.
    - output_dir (str): Optional directory to save the output video if output_path is not provided.
    
    Returns:
    - str: Path to the output video file.
    """
    try:
        # Load the image
        image_clip = ImageClip(image_path, duration=duration)
        
        # Set output path if not provided
        unique_name = f"image2video_{uuid.uuid4().hex}.mp4"
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            output_path = os.path.join(output_dir, unique_name)
        else:
            output_path = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False).name
        
        # Write the video file
        image_clip.write_videofile(output_path, fps=24, codec='libx264', audio_codec='aac')
        
        logger.info(f"Video created from image: {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"Error converting image to video: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to convert image to video: {str(e)}")

async def download_file_from_url(url: str, temp_dir: Optional[str] = "temp_videos") -> str:
    """
    Downloads a file from URL to a temporary location.
    
    Parameters:
    - url (str): URL of the file to download
    - temp_dir (str): Optional directory to store temp files
    
    Returns:
    - str: Path to the downloaded temporary file
    """
    try:
        # Parse URL to get filename
        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path)
        
        # If no filename in URL, generate one
        if not filename or '.' not in filename:
            filename = "temp_media"
        
        # Guess file extension from URL or default to .mp4 (video) or .mp3 (audio)
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ['.mp4', '.mov', '.avi', '.mkv', '.mp3', '.wav', '.aac', '.flac', '.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.heic']:
            ext = '.mp4'  # Default to .mp4 if unknown

        # Ensure temp_dir exists if provided
        if temp_dir:
            os.makedirs(temp_dir, exist_ok=True)

        # Create temporary file
        temp_file = tempfile.NamedTemporaryFile(
            delete=False,
            suffix=ext,
            prefix='media_',
            dir=temp_dir
        )
        temp_path = temp_file.name
        temp_file.close()
        
        # Download file with streaming
        logger.info(f"Downloading from {url}...")
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded_size = 0
        
        with open(temp_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)
        
        logger.info(f"Download completed: {temp_path}")
        return temp_path
        
    except Exception as e:
        logger.error(f"Error downloading file from {url}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to download video from URL: {str(e)}")

async def change_video_ratio(video_path: str, new_ratio: str, output_dir: Optional[str] = "output") -> str:
    """
    Changes the aspect ratio of a video to a new ratio.
    
    Parameters:
    - video_path (str): Path to the input video file.
    - new_ratio (str): New aspect ratio in the format 'width:height' (e.g., '16:9').
    - output_dir (str): Optional directory to save the output video. If None, uses system temp dir.
    
    Returns:
    - str: Path to the output video file with the new aspect ratio.
    """
    try:
        # Load the video
        clip = VideoFileClip(video_path)
        
        # Parse new ratio
        ratio_width, ratio_height = map(int, new_ratio.split(':'))

        # Calculate new dimensions to fit the aspect ratio while preserving the original width
        orig_width, orig_height = clip.size
        new_width = orig_width
        new_height = int(orig_width * ratio_height / ratio_width)

        # If new height is greater than original, fit by height instead
        if new_height > orig_height:
            new_height = orig_height
            new_width = int(orig_height * ratio_width / ratio_height)

        # Resize and crop/pad to fit the aspect ratio exactly
        resized_clip = clip.with_effects([
            vfx.Resize(new_size=(new_width, new_height)),
            vfx.Crop(x1=0, y1=0, x2=new_width, y2=new_height)
        ])
        
        # Generate unique filename
        unique_name = f"resized_{uuid.uuid4().hex}.mp4"
        
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, unique_name)
        
        # Write the output file
        resized_clip.write_videofile(output_path, codec="libx264", audio_codec="aac")
        
        logger.info(f"Video resized and saved to {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"Error changing video aspect ratio: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to change video aspect ratio: {str(e)}")

def is_url(path: str) -> bool:
    """Check if the given path is a URL"""
    return path.startswith(('http://', 'https://'))

async def stitch_videos(video_path1, video_path2, output_dir="output"):
    """
    Stitches two videos together end-to-end and saves the result.

    Parameters:
    - video_path1 (str): Path to the first video file.
    - video_path2 (str): Path to the second video file.
    - output_dir (str): Directory to save the stitched video.
    """
    try:
        # Load both videos
        clip1 = VideoFileClip(video_path1)
        clip2 = VideoFileClip(video_path2)
        
        # Resize both clips to the same size if needed
        if clip1.size != clip2.size:
            target_size = (min(clip1.w, clip2.w), min(clip1.h, clip2.h))
            clip1 = clip1.with_effects([vfx.Resize(new_size=target_size)])
            clip2 = clip2.with_effects([vfx.Resize(new_size=target_size)])

        # Concatenate videos
        final_clip = concatenate_videoclips([clip1, clip2])
        
        unique_name = f"stitched_video_{uuid.uuid4().hex}.mp4"
        
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, unique_name)
        
        # Write the output file
        final_clip.write_videofile(output_path, codec="libx264", audio_codec="aac")

        print(f"Video saved to {output_path}")
        return output_path
    except Exception as e:
        print(f"Error stitching videos: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to stitch videos: {str(e)}")

async def trim_video(video_path, start_time, end_time, output_dir="temp_videos"):
    """
    Trims a video between start_time and end_time and saves it.

    Parameters:
    - video_path (str): Path to the input video.
    - start_time (float): Start time in seconds.
    - end_time (float): End time in seconds.
    - output_dir (str): Directory to save the trimmed video. If None, uses system temp dir.

    Returns:
    - str: Path to the trimmed video file.
    """
    try:
        clip = VideoFileClip(video_path)

        if start_time < 0 or end_time > clip.duration or start_time >= end_time:
            raise ValueError("Invalid start or end time.")

        trimmed_clip = clip.subclipped(start_time, end_time)
        final_clip = CompositeVideoClip([trimmed_clip])

        # Generate unique filename
        unique_name = f"trimmed_{uuid.uuid4().hex}.mp4"
        
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, unique_name)

        final_clip.write_videofile(
            output_path,
            codec="libx264",
            audio_codec="aac",
            ffmpeg_params=[
                "-pix_fmt", "yuv420p",     # Required for Android
                "-profile:v", "main",      # Better compression than baseline
                "-level", "3.1"            # Good for 720p and most Android devices
            ]
        )        
        print(f"Trimmed video saved to {output_path}")
        return output_path
    except Exception as e:
        print(f"Error trimming video: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to trim video: {str(e)}")

from moviepy import VideoFileClip, AudioFileClip

async def add_audio_to_video(video_path, audio_path, output_dir="output"):
    """
    Removes original audio from a video and adds a new audio file.

    Parameters:
    - video_path (str): Path to the input video file.
    - audio_path (str): Path to the audio file to add.
    - output_dir (str): Directory to save the final video.
    """
    try:
        # Load video and remove existing audio
        video_clip = VideoFileClip(video_path)
        
        # Load audio
        audio_clip = AudioFileClip(audio_path)
        
        croped_audio = audio_clip.subclipped(0, min(video_clip.duration, audio_clip.duration))
        
        croped_audio = CompositeAudioClip([croped_audio])
        
        # Set the audio to the video
        clip = video_clip.with_audio(croped_audio)
        
        final_clip = CompositeVideoClip([clip])

        # Generate unique filename
        unique_name = f"video_with_audio_{uuid.uuid4().hex}.mp4"
        
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, unique_name)
        
        # Write the final video with new audio
        final_clip.write_videofile(output_path, codec="libx264", audio_codec="aac")
        print(f"Video with new audio saved to {output_path}")
        return output_path
    except Exception as e:
        print(f"Error adding audio to video: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to add audio to video: {str(e)}")