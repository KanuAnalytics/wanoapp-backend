import boto3
from botocore.exceptions import ClientError
import os
import uuid
import re
from fastapi import UploadFile

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
        'ico': 'image/x-icon'
    }
    # Default to video/mp4 for unknown, or audio/mpeg if isAudio
    return content_types.get(ext, 'audio/mpeg' if ext in ['mp3', 'wav', 'aac', 'ogg', 'flac', 'm4a'] else 'video/mp4')

async def upload_to_spaces(file_obj: UploadFile, filename: str, isAudio: bool = False):
    client = boto3.client(
        's3',
        endpoint_url=os.getenv('DO_SPACES_ENDPOINT'),
        aws_access_key_id=os.getenv('DO_SPACES_ACCESS_KEY_ID'),
        aws_secret_access_key=os.getenv('DO_SPACES_SECRET_KEY'),
    )

    folder = "audio" if isAudio else "videos"
    unique_filename = f"{folder}/{uuid.uuid4()}_{filename}"

    try:
        file_content = await file_obj.read()
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(file_content)
            tmp_path = tmp.name

        client.upload_file(
            tmp_path,
            os.getenv('DO_SPACES_BUCKET_NAME'),
            unique_filename,
            ExtraArgs={
                'ContentType': get_content_type(filename),
                'ACL': 'public-read'
            }
        )

        os.remove(tmp_path)

        public_url = f"{os.getenv('DO_SPACES_ENDPOINT')}/{os.getenv('DO_SPACES_BUCKET_NAME')}/{unique_filename}"
        return True, public_url, unique_filename

    except ClientError as e:
        return False, str(e), None
    except Exception as e:
        return False, f"Unexpected error: {str(e)}", None