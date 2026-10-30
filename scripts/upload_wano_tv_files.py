"""
Uploads a title's artwork and caption files to DigitalOcean Spaces under
<DO_SPACES_BUCKET_NAME>/wano-tv/<folder>/, publicly readable, and prints each
file's CDN URL.

.srt captions are converted to WebVTT (.vtt) first: iOS only loads sidecar
subtitles in that format, and Android reads it too.

Run from the repo root:
    python -m scripts.upload_wano_tv_files <folder> <file> [<file> ...]
"""

import mimetypes
import os
import re
import sys
import tempfile
import boto3
from botocore.client import Config
from app.core.config import settings

PREFIX = "wano-tv"
CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".vtt": "text/vtt",
}


def srt_to_vtt(srt_path: str, out_dir: str) -> str:
    """WebVTT is SRT with a header and dots instead of commas in timestamps."""
    with open(srt_path, "r", encoding="utf-8-sig") as f:
        text = f.read().replace("\r\n", "\n")
    text = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
    name = os.path.splitext(os.path.basename(srt_path))[0] + ".vtt"
    vtt_path = os.path.join(out_dir, name)
    with open(vtt_path, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n" + text.strip() + "\n")
    return vtt_path


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    folder = sys.argv[1].strip("/")
    paths = sys.argv[2:]
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        sys.exit(f"Not found: {', '.join(missing)}")

    client = boto3.client(
        "s3",
        endpoint_url=settings.DO_SPACES_ENDPOINT,
        aws_access_key_id=settings.DO_SPACES_ACCESS_KEY_ID,
        aws_secret_access_key=settings.DO_SPACES_SECRET_KEY,
        config=Config(signature_version="s3v4"),
    )
    cdn = settings.DO_SPACES_CDN_URL.rstrip("/")
    bucket = settings.DO_SPACES_BUCKET_NAME

    with tempfile.TemporaryDirectory() as tmp:
        for path in paths:
            if path.lower().endswith(".srt"):
                path = srt_to_vtt(path, tmp)
            ext = os.path.splitext(path)[1].lower()
            key = f"{PREFIX}/{folder}/{os.path.basename(path)}"
            client.upload_file(
                path,
                bucket,
                key,
                ExtraArgs={
                    "ACL": "public-read",
                    "ContentType": CONTENT_TYPES.get(ext)
                    or mimetypes.guess_type(path)[0]
                    or "application/octet-stream",
                    "CacheControl": "public, max-age=300",
                },
            )
            print(f"{cdn}/{bucket}/{key}")


if __name__ == "__main__":
    main()
