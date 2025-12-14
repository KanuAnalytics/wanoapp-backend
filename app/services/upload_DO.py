#app/services/upload_DO.py


import boto3
from botocore.exceptions import ClientError
import os
import uuid
import re
from fastapi import UploadFile
from app.core.config import settings
from botocore.client import Config
import requests


def secure_filename(filename: str) -> str:
    """Secure a filename by removing/replacing unsafe characters"""
    filename = os.path.basename(filename)
    filename = re.sub(r'[^\w\s.-]', '', filename)
    filename = filename.strip('. ')
    return filename[:255] if filename else 'unnamed'

ALLOWED_EXTENSIONS = {
    # Video
    'mp4', 'avi', 'mov', 'wmv', 'flv', 'webm', 'mkv', 'MP4',
    # Audio
    'mp3', 'wav', 'aac', 'ogg', 'flac', 'm4a',
    # Images
    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff', 'webp', 'svg', 'ico','heic'
}

def allowed_file(filename):
    print(f"Checking if file is allowed: {filename}")
    ext = ''
    if '.' in filename:
        ext_part = filename.rsplit('.', 1)[1]
        for ch in ext_part:
            if ch == '%':
                break
            ext += ch
        ext = ext.lower()
    print('.' in filename and ext)
    return '.' in filename and ext in ALLOWED_EXTENSIONS

def is_image_file(filename):
    """Check if the file is an image based on its extension"""
    if '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    image_extensions = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff', 'webp', 'svg', 'ico', 'heic'}
    return ext in image_extensions

def get_content_type(filename):
    ext = filename.rsplit('.', 1)[1].lower()
    content_types = {
        # Video
        'mp4': 'video/mp4',
        'avi': 'video/x-msvideo',
        'mov': 'video/quicktime',
        'wmv': 'video/x-ms-wmv',
        'flv': 'video/x-flv',
        'webm': 'video/webm',
        'mkv': 'video/x-matroska',
        'MP4': 'video/mp4',
        # Audio
        'mp3': 'audio/mpeg',
        'wav': 'audio/wav',
        'aac': 'audio/aac',
        'ogg': 'audio/ogg',
        'flac': 'audio/flac',
        'm4a': 'audio/mp4',
        # Images
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'png': 'image/png',
        'gif': 'image/gif',
        'bmp': 'image/bmp',
        'tiff': 'image/tiff',
        'webp': 'image/webp',
        'svg': 'image/svg+xml',
        'ico': 'image/x-icon',
        'heic': 'image/heic'
    }
    # Default to video/mp4 for unknown, or audio/mpeg if isAudio
    return content_types.get(ext, 'audio/mpeg' if ext in ['mp3', 'wav', 'aac', 'ogg', 'flac', 'm4a'] else 'video/mp4')

async def upload_to_spaces(file_obj: UploadFile, filename: str, isAudio: bool = False, isImage: bool = False):
    client = boto3.client(
        's3',
        endpoint_url=settings.DO_SPACES_ENDPOINT,
        aws_access_key_id=settings.DO_SPACES_ACCESS_KEY_ID,
        aws_secret_access_key=settings.DO_SPACES_SECRET_KEY,
    )

    if isImage:
        folder = "profile-pictures"
    elif isAudio:
        folder = "audio"
    else:
        folder = "videos"
    
    unique_filename = f"{folder}/{uuid.uuid4()}_{filename}"

    try:
        file_content = await file_obj.read()
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(file_content)
            tmp_path = tmp.name

        client.upload_file(
            tmp_path,
            settings.DO_SPACES_BUCKET_NAME,
            unique_filename,
            ExtraArgs={
                'ContentType': get_content_type(filename),
                'ACL': 'public-read'
            }
        )

        os.remove(tmp_path)

        public_url = f"{settings.DO_SPACES_CDN_URL}/{settings.DO_SPACES_BUCKET_NAME}/{unique_filename}"
        return True, public_url, unique_filename

    except ClientError as e:
        return False, str(e), None
    except Exception as e:
        return False, f"Unexpected error: {str(e)}", None
    
    

def generate_presigned_upload_url(filename: str, folder: str = "videos"):
    """
    Generate a presigned PUT URL to upload directly to DigitalOcean Spaces.
    Returns dict with upload_url, file_url, key, and content_type.
    Compatible with AWS S3 signing pattern used by DigitalOcean.
    """
    
    s3_client = boto3.client(
        "s3",
        endpoint_url=settings.DO_SPACES_ENDPOINT,
        aws_access_key_id=settings.DO_SPACES_ACCESS_KEY_ID,
        aws_secret_access_key=settings.DO_SPACES_SECRET_KEY,
        config=Config(signature_version='s3v4')
    )

    # ✅ Ensure filename is safe
    safe_filename = os.path.basename(filename)
    if not safe_filename:
        safe_filename = "unnamed"

    # ✅ Determine MIME type safely
    content_type = get_content_type(safe_filename)
    if not content_type:
        content_type = "application/octet-stream"

    # ✅ Use UUID to avoid collisions
    object_name = f"{folder}/{uuid.uuid4()}_{safe_filename}"

    try:
        # ✅ Generate pre-signed PUT URL with both ACL and Content-Type
        presigned_url = s3_client.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": settings.DO_SPACES_BUCKET_NAME,
                "Key": object_name,
                "ACL": "public-read",
                "ContentType": content_type,
            },
            ExpiresIn=3600,  
        )

        # ✅ Public CDN URL
        file_url = f"{settings.DO_SPACES_CDN_URL}/{settings.DO_SPACES_BUCKET_NAME}/{object_name}"
        return {
            "upload_url": presigned_url,
            "file_url": file_url,
            "key": object_name,
            "content_type": content_type,
            "acl": "public-read",
        }

    except ClientError as e:
        raise RuntimeError(f"Failed to generate presigned URL: {str(e)}")


def generate_stream_direct_upload_url(filename: str, folder: str = "videos"):
    """
    Generate a Cloudflare Stream direct upload URL.
    Returns dict with upload_url, stream_uid, and meta info.

    Frontend will:
      - POST the file to `upload_url` as multipart/form-data
      - After upload, the video is available via Stream playback URL.
    """

    # Make sure you have these in your settings / env:
    # CF_STREAM_ACCOUNT_ID
    # CF_STREAM_API_TOKEN  (must have Stream:Edit permissions)
    account_id = settings.CLOUDFLARE_ACCOUNT_ID
    api_token = settings.CLOUDFLARE_STREAM_API_TOKEN
    base_url = settings.CLOUDFLARE_STREAM_API_BASE

    if not account_id or not api_token:
        raise RuntimeError("Cloudflare Stream account ID or API token not configured")

    # Optional: attach metadata to the video
    safe_filename = os.path.basename(filename) or "unnamed"
    # This is just to namespace/organize metadata, has no folder structure effect in Stream
    video_name = f"{folder}/{uuid.uuid4()}_{safe_filename}"

    url = f"{base_url}/accounts/{account_id}/stream/direct_upload"

    # Docs: you can send e.g. {"maxDurationSeconds": 3600, "expiry": "2025-01-01T00:00:00Z"} etc.
    # We'll keep it minimal.
    payload = {
        "maxDurationSeconds": 210,
        "meta": {
            "name": video_name,
        },
        # If you want this upload URL to expire quickly, you can add "expiry" here.
        # "expiry": "2025-12-31T23:59:59Z",
    }

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    if not resp.ok:
        raise RuntimeError(
            f"Failed to create Cloudflare Stream direct upload URL: "
            f"{resp.status_code} {resp.text}"
        )

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Cloudflare Stream API responded with error: {data}")

    result = data["result"]
    upload_url = result["uploadURL"]
    stream_uid = result["uid"]

    # Public playback URL patterns (you can choose how you want to expose)
    # HLS:  https://videodelivery.net/{uid}/manifest/video.m3u8
    # Web:  https://watch.videodelivery.net/{uid}
    playback_url = f"https://videodelivery.net/{stream_uid}/manifest/video.m3u8"
    
    print(stream_uid)

    return {
        "upload_url": upload_url,   # where the client will POST the file
        "stream_uid": stream_uid,   # save this in DB for later playback
        "file_url": playback_url,
        "name": video_name,
    }


def get_stream_video_status(uid: str):
    """
    Check Cloudflare Stream video status by UID.
    Calls Cloudflare API using backend-side credential, never exposed to FE.
    """
    account_id = settings.CLOUDFLARE_ACCOUNT_ID
    api_token = settings.CLOUDFLARE_STREAM_API_TOKEN

    if not account_id or not api_token:
        raise RuntimeError("Cloudflare Stream account ID or API token not configured")

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/stream/{uid}"

    headers = {
        "Authorization": f"Bearer {api_token}",
    }

    resp = requests.get(url, headers=headers, timeout=10)
    if not resp.ok:
        raise RuntimeError(
            f"Failed to fetch video status from Cloudflare Stream: "
            f"{resp.status_code} {resp.text}"
        )

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Cloudflare Stream API error: {data}")

    result = data["result"]

    # You can shape this however you want; here's a minimal + useful subset:
    return {
        "uid": result.get("uid"),
        "readyToStream": result.get("readyToStream"),
    }
