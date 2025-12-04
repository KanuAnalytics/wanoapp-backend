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
ffmpeg -y -i "{src}" -filter_complex "\
[0:v]split=5[v0][v1][v2][v3][v4]; \
[v0]scale=w=1920:h=-2:force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p[v0out]; \
[v1]scale=w=1280:h=-2:force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p[v1out]; \
[v2]scale=w=854:h=-2:force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p[v2out]; \
[v3]scale=w=640:h=-2:force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p[v3out]; \
[v4]scale=w=426:h=-2:force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p[v4out]" \
-map "[v0out]" -map a:0? \
-map "[v1out]" -map a:0? \
-map "[v2out]" -map a:0? \
-map "[v3out]" -map a:0? \
-map "[v4out]" -map a:0? \
-c:v libx264 -preset veryfast -g 48 -keyint_min 48 -sc_threshold 0 \
-profile:v:0 high -b:v:0 5500k -maxrate:v:0 6000k -bufsize:v:0 11000k \
-profile:v:1 high -b:v:1 3000k -maxrate:v:1 3500k -bufsize:v:1 7000k  \
-profile:v:2 main -b:v:2 1500k -maxrate:v:2 1800k -bufsize:v:2 3600k  \
-profile:v:3 main -b:v:3 800k  -maxrate:v:3 950k  -bufsize:v:3 1900k  \
-profile:v:4 baseline -b:v:4 450k  -maxrate:v:4 550k  -bufsize:v:4 1100k \
-c:a aac -b:a 128k -ac 2 \
-f hls -hls_time 4 -hls_playlist_type vod -hls_segment_type fmp4 -hls_flags independent_segments \
-hls_fmp4_init_filename "init_%v.mp4" \
-hls_segment_filename "{out_dir}/v%v/seg_%06d.m4s" \
-master_pl_name master.m3u8 \
-var_stream_map "v:0,a:0 v:1,a:1 v:2,a:2 v:3,a:3 v:4,a:4" \
"{out_dir}/v%v/prog_index.m3u8"

# Poster
ffmpeg -y -ss 1 -i "{src}" -vframes 1 -vf "scale=720:-1" "{out_dir}/poster.jpg"
'''

        subprocess.run(["bash","-lc", cmd], check=True)

        master_url = publish_dir(out_dir, f"hls/{video_id}")
        poster_url = f"{settings.DO_SPACES_CDN_URL}/{settings.DO_SPACES_BUCKET_NAME}/hls/{video_id}/poster.jpg"
        return {"master_url": master_url, "poster_url": poster_url}
