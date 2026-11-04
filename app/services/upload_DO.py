#app/services/upload_DO.py


import asyncio
import base64
import boto3
from botocore.exceptions import ClientError
import os
import uuid
import re
from typing import Optional
from urllib.parse import urlparse
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
    # Images. No svg: it can carry script and is served from our CDN origin.
    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff', 'webp', 'ico','heic'
}

VIDEO_EXTENSIONS = {'mp4', 'avi', 'mov', 'wmv', 'flv', 'webm', 'mkv'}
AUDIO_EXTENSIONS = {'mp3', 'wav', 'aac', 'ogg', 'flac', 'm4a'}
IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff', 'webp', 'ico', 'heic'}

# Folders server-side uploads write to (upload_to_spaces). Only objects under
# these are ever deleted by cleanup; music, TV and other admin-managed
# folders are never touched however a post references them.
SERVER_UPLOAD_FOLDERS = ("profile-pictures/", "audio/", "videos/")

REQUESTS_TIMEOUT = 15

_STREAM_UID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")


def _check_stream_uid(uid: str) -> None:
    """The UID goes into a Cloudflare API path; refuse anything but a plain id."""
    if not uid or not _STREAM_UID_RE.match(uid):
        raise ValueError("Invalid Cloudflare Stream UID")


def file_extension(filename: str) -> str:
    if not filename or '.' not in filename:
        return ''
    ext = ''
    for ch in filename.rsplit('.', 1)[1]:
        if ch == '%':
            break
        ext += ch
    return ext.lower()

def allowed_file(filename):
    ext = file_extension(filename)
    return bool(ext) and ext in ALLOWED_EXTENSIONS


def max_upload_size(filename: str) -> int:
    """Largest accepted /video/upload body for this kind of file, in bytes."""
    ext = file_extension(filename)
    if ext in IMAGE_EXTENSIONS:
        return 20 * 1024 * 1024
    if ext in AUDIO_EXTENSIONS:
        return 50 * 1024 * 1024
    return 200 * 1024 * 1024


def is_image_file(filename):
    """Check if the file is an image based on its extension"""
    if '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in IMAGE_EXTENSIONS

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
        'ico': 'image/x-icon',
        'heic': 'image/heic'
    }
    # Default to video/mp4 for unknown, or audio/mpeg if isAudio
    return content_types.get(ext, 'audio/mpeg' if ext in ['mp3', 'wav', 'aac', 'ogg', 'flac', 'm4a'] else 'video/mp4')

def _spaces_client():
    return boto3.client(
        's3',
        endpoint_url=settings.DO_SPACES_ENDPOINT,
        aws_access_key_id=settings.DO_SPACES_ACCESS_KEY_ID,
        aws_secret_access_key=settings.DO_SPACES_SECRET_KEY,
    )


def _upload_folder(isAudio: bool, isImage: bool) -> str:
    if isImage:
        return "profile-pictures"
    if isAudio:
        return "audio"
    return "videos"


async def upload_path_to_spaces(tmp_path: str, filename: str, isAudio: bool = False, isImage: bool = False):
    """Upload a local file. Returns (success, public_url_or_error, object_key)."""
    unique_filename = f"{_upload_folder(isAudio, isImage)}/{uuid.uuid4()}_{filename}"
    try:
        client = _spaces_client()
        # boto3 blocks; in a thread so a slow upload doesn't stall every other request.
        await asyncio.to_thread(
            client.upload_file,
            tmp_path,
            settings.DO_SPACES_BUCKET_NAME,
            unique_filename,
            ExtraArgs={
                'ContentType': get_content_type(filename),
                'ACL': 'public-read'
            }
        )
        public_url = f"{settings.DO_SPACES_CDN_URL}/{settings.DO_SPACES_BUCKET_NAME}/{unique_filename}"
        return True, public_url, unique_filename
    except ClientError as e:
        return False, str(e), None
    except Exception as e:
        return False, f"Unexpected error: {str(e)}", None


async def upload_to_spaces(file_obj: UploadFile, filename: str, isAudio: bool = False, isImage: bool = False):
    import tempfile

    tmp_path = None
    try:
        file_content = await file_obj.read()
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(file_content)
            tmp_path = tmp.name
        return await upload_path_to_spaces(tmp_path, filename, isAudio=isAudio, isImage=isImage)
    except Exception as e:
        return False, f"Unexpected error: {str(e)}", None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


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
        "maxDurationSeconds": 330,
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

    resp = requests.post(url, json=payload, headers=headers, timeout=REQUESTS_TIMEOUT)
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
    _check_stream_uid(uid)
    account_id = settings.CLOUDFLARE_ACCOUNT_ID
    api_token = settings.CLOUDFLARE_STREAM_API_TOKEN

    if not account_id or not api_token:
        raise RuntimeError("Cloudflare Stream account ID or API token not configured")

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/stream/{uid}"

    headers = {
        "Authorization": f"Bearer {api_token}",
    }

    resp = requests.get(url, headers=headers, timeout=REQUESTS_TIMEOUT)
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


def get_stream_video_name(uid: str) -> Optional[str]:
    """
    The name a Stream video was uploaded under, e.g. "videos/<uuid>_<filename>".
    Returns None if the video no longer exists.
    """
    _check_stream_uid(uid)
    account_id = settings.CLOUDFLARE_ACCOUNT_ID
    api_token = settings.CLOUDFLARE_STREAM_API_TOKEN

    if not account_id or not api_token:
        raise RuntimeError("Cloudflare Stream account ID or API token not configured")

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/stream/{uid}"
    headers = {"Authorization": f"Bearer {api_token}"}

    resp = requests.get(url, headers=headers, timeout=REQUESTS_TIMEOUT)
    if resp.status_code == 404:
        return None
    if not resp.ok:
        raise RuntimeError(
            f"Failed to fetch video from Cloudflare Stream: {resp.status_code} {resp.text}"
        )

    return (resp.json()["result"].get("meta") or {}).get("name") or ""


# Shared "this video was deleted" stream. Every soft-deleted video record has
# its stream links pointed here, so old feeds, bookmarks and likes play this
# instead of a dead link. It must never be deleted: an earlier version of the
# delete endpoint read the stream id back off an already-deleted video and
# removed the previous placeholder from Cloudflare.
DELETED_VIDEO_PLACEHOLDER_UID = "97002814c3643d7bba41a9311db4e610"
DELETED_VIDEO_PLACEHOLDER_URL = (
    f"https://videodelivery.net/{DELETED_VIDEO_PLACEHOLDER_UID}/manifest/video.m3u8"
)
PROTECTED_STREAM_UIDS = frozenset({DELETED_VIDEO_PLACEHOLDER_UID})


def extract_stream_uid(remote_url_cf: str) -> Optional[str]:
    """Pull the Cloudflare Stream UID out of a videodelivery.net (or
    customer-*.cloudflarestream.com) playback URL."""
    match = re.search(r"(?:videodelivery\.net|cloudflarestream\.com)/([a-f0-9]+)/", remote_url_cf or "")
    return match.group(1) if match else None


def delete_stream_video(uid: str) -> None:
    """
    Delete a video from Cloudflare Stream by UID.
    A 404 (already gone) is treated as success.
    The shared deleted-video placeholder is never deleted, whoever asks.
    """
    if not uid or uid in PROTECTED_STREAM_UIDS:
        return
    _check_stream_uid(uid)

    account_id = settings.CLOUDFLARE_ACCOUNT_ID
    api_token = settings.CLOUDFLARE_STREAM_API_TOKEN

    if not account_id or not api_token:
        raise RuntimeError("Cloudflare Stream account ID or API token not configured")

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/stream/{uid}"
    headers = {"Authorization": f"Bearer {api_token}"}

    resp = requests.delete(url, headers=headers, timeout=REQUESTS_TIMEOUT)
    if resp.status_code == 404:
        return
    if not resp.ok:
        raise RuntimeError(
            f"Failed to delete video from Cloudflare Stream: {resp.status_code} {resp.text}"
        )


def spaces_object_key(file_url: Optional[str]) -> Optional[str]:
    """The object key of a Spaces URL (CDN or origin address), else None."""
    if not file_url:
        return None
    bucket = settings.DO_SPACES_BUCKET_NAME
    prefixes = (
        f"{settings.DO_SPACES_CDN_URL}/{bucket}/",
        f"{settings.DO_SPACES_ENDPOINT}/{bucket}/",
    )
    prefix = next((p for p in prefixes if file_url.startswith(p)), None)
    if prefix is None:
        return None
    key = file_url[len(prefix):]
    return key or None


def is_deletable_spaces_url(file_url: Optional[str]) -> bool:
    """Only files server uploads wrote (SERVER_UPLOAD_FOLDERS) may be deleted."""
    key = spaces_object_key(file_url)
    if not key or ".." in key.split("/"):
        return False
    return key.startswith(SERVER_UPLOAD_FOLDERS)


def is_server_upload_path(file_url: Optional[str]) -> bool:
    """Path-only form of is_deletable_spaces_url: <scheme>://<host>/<bucket>/<folder>/...
    with <folder> one of SERVER_UPLOAD_FOLDERS. Independent of the configured
    hosts, so callers can filter before handing URLs to delete_from_spaces
    (which still checks the full prefix)."""
    if not file_url:
        return False
    try:
        path = urlparse(file_url).path
    except ValueError:
        return False
    segments = path.split("/")
    # ["", bucket, folder, ...file]
    if len(segments) < 4 or not segments[-1] or ".." in segments:
        return False
    return f"{segments[2]}/" in SERVER_UPLOAD_FOLDERS


def delete_from_spaces(file_url: str) -> None:
    """
    Delete an object from DigitalOcean Spaces by its public URL.
    Accepts both the CDN address and the origin (non-CDN) address; some older
    thumbnails and profile pictures were stored with the origin one. Any
    other URL (e.g. a Cloudflare Stream link) is ignored.
    Missing objects don't error (S3-compatible delete is idempotent).
    Objects outside the server upload folders (music, TV, ...) are never deleted.
    """
    if not is_deletable_spaces_url(file_url):
        return
    object_key = spaces_object_key(file_url)

    client = _spaces_client()
    client.delete_object(Bucket=settings.DO_SPACES_BUCKET_NAME, Key=object_key)


def generate_cf_tus_upload_url(filename: str, fileSize: int, folder: str = "videos"):
    """
    Generate a TUS upload URL for Cloudflare Stream.
    Returns dict with upload_url and media_id.
    """

    account_id = settings.CLOUDFLARE_ACCOUNT_ID
    api_token = settings.CLOUDFLARE_STREAM_API_TOKEN

    if not account_id or not api_token:
        raise RuntimeError("Cloudflare Stream account ID or API token not configured")

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/stream?direct_user=true"
    
    safe_filename = os.path.basename(filename) or "unnamed"
    # This is just to namespace/organize metadata, has no folder structure effect in Stream
    video_name = f"{folder}/{uuid.uuid4()}_{safe_filename}"

    MAX_DURATION_SECONDS = 420  # 7 minutes

    encoded_name = base64.b64encode(video_name.encode()).decode()
    encoded_max_duration = base64.b64encode(str(MAX_DURATION_SECONDS).encode()).decode()

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Tus-Resumable": "1.0.0",
        "Upload-Metadata": f"name {encoded_name},maxdurationseconds {encoded_max_duration}",
        "Upload-Length": str(fileSize)    # required by TUS spec
    }

    res = requests.post(url, headers=headers, timeout=REQUESTS_TIMEOUT)

    if res.status_code not in (201, 204):
        raise RuntimeError(f"Failed to create TUS upload URL: {res.text}")

    upload_url = res.headers.get("Location")
    media_id = res.headers.get("stream-media-id")
    if not upload_url or not media_id:
        raise RuntimeError("Cloudflare did not return a TUS upload location")
    
    
    playback_url = f"https://videodelivery.net/{media_id}/manifest/video.m3u8"

    return {
        "upload_url": upload_url,
        "stream_uid": media_id,
        "file_url": playback_url,
        "name": video_name,
    }