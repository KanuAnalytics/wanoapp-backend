# app/services/transcode_hls.py
import os, tempfile, subprocess
import boto3
from botocore.client import Config
from app.core.config import settings
from app.services.spaces_publish import publish_dir

def _s3():
    return boto3.client(
        "s3",
        endpoint_url=settings.DO_SPACES_ENDPOINT,
        aws_access_key_id=settings.DO_SPACES_ACCESS_KEY_ID,
        aws_secret_access_key=settings.DO_SPACES_SECRET_KEY,
        config=Config(signature_version="s3v4"),
    )

def transcode_and_publish(object_key: str, video_id: str) -> dict:
    """
    Downloads the original from Spaces by object_key,
    builds a CMAF HLS ladder under a temp dir, uploads to
    Spaces at hls/{video_id}/..., and returns URLs.
    """
    s3 = _s3()
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "input.mp4")
        with open(src, "wb") as f:
            s3.download_fileobj(settings.DO_SPACES_BUCKET_NAME, object_key, f)

        out_dir = os.path.join(tmp, "hls", video_id)
        os.makedirs(out_dir, exist_ok=True)

        cmd = f'''set -e
ffmpeg -y -i "{src}" -filter_complex "[0:v]split=5[v1080][v720][v480][v360][v240]" \
-map "[v1080]" -map a:0? -c:v h264 -profile:v high -preset veryfast -b:v 5500k -maxrate 6000k -bufsize 11000k -vf "scale=w=1920:h=1080:force_original_aspect_ratio=decrease" \
-g 48 -keyint_min 48 -sc_threshold 0 -c:a aac -b:a 128k -ac 2 \
-f hls -hls_time 4 -hls_playlist_type vod -hls_segment_type fmp4 -master_pl_name master.m3u8 \
-hls_flags independent_segments \
-var_stream_map "v:0,a:0 v:1,a:0 v:2,a:0 v:3,a:0 v:4,a:0" \
-b:v:1 3000k -maxrate:v:1 3500k -bufsize:v:1 7000k -vf:v:1 "scale=w=1280:h=720:force_original_aspect_ratio=decrease" \
-b:v:2 1500k -maxrate:v:2 1800k -bufsize:v:2 3600k -vf:v:2 "scale=w=854:h=480:force_original_aspect_ratio=decrease" \
-b:v:3 800k  -maxrate:v:3 950k  -bufsize:v:3 1900k -vf:v:3 "scale=w=640:h=360:force_original_aspect_ratio=decrease" \
-b:v:4 450k  -maxrate:v:4 550k  -bufsize:v:4 1100k -vf:v:4 "scale=w=426:h=240:force_original_aspect_ratio=decrease" \
-hls_fmp4_init_filename "init_$RepresentationID$.mp4" -seg_duration 4 \
"{out_dir}/prog_index.m3u8"

# Poster
ffmpeg -y -ss 1 -i "{src}" -vframes 1 -vf "scale=720:-1" "{out_dir}/poster.jpg"
'''
        subprocess.run(["bash","-lc", cmd], check=True)

        master_url = publish_dir(out_dir, f"hls/{video_id}")
        poster_url = f"{settings.DO_SPACES_CDN_URL}/{settings.DO_SPACES_BUCKET_NAME}/hls/{video_id}/poster.jpg"
        return {"master_url": master_url, "poster_url": poster_url}
