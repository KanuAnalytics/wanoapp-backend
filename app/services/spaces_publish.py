# app/services/spaces_publish.py
import os, mimetypes, boto3
from botocore.client import Config
from app.core.config import settings

MIME_OVERRIDES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".m4s":  "video/iso.segment",
    ".ts":   "video/mp2t",
    ".vtt":  "text/vtt",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
}

def _mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return MIME_OVERRIDES.get(ext, mimetypes.guess_type(path)[0] or "application/octet-stream")

def publish_dir(local_dir: str, dest_prefix: str) -> str:
    """
    Recursively upload local_dir to Spaces under dest_prefix with proper
    MIME and cache headers. Returns the master.m3u8 CDN URL.
    """
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.DO_SPACES_ENDPOINT,
        aws_access_key_id=settings.DO_SPACES_ACCESS_KEY_ID,
        aws_secret_access_key=settings.DO_SPACES_SECRET_KEY,
        config=Config(signature_version="s3v4"),
    )
    for root, _, files in os.walk(local_dir):
        for f in files:
            lp = os.path.join(root, f)
            rel = os.path.relpath(lp, start=local_dir).replace("\\","/")
            key = f"{dest_prefix}/{rel}"

            is_playlist = rel.endswith(".m3u8")
            s3.upload_file(
                lp,
                settings.DO_SPACES_BUCKET_NAME,
                key,
                ExtraArgs={
                    "ContentType": _mime(lp),
                    "ACL": "public-read",
                    "CacheControl": "public, max-age=60" if is_playlist
                                    else "public, max-age=31536000, immutable",
                },
            )
    return f"{settings.DO_SPACES_CDN_URL}/{settings.DO_SPACES_BUCKET_NAME}/{dest_prefix}/master.m3u8"
