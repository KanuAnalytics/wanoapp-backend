"""
Uploads an HLS folder made by scripts/mp4_to_hls.sh to DigitalOcean Spaces
under wano-tv/<name>/, publicly readable, and prints the CDN URL of its
master playlist (what goes in a title's video_url).

Run from the repo root:
    python -m scripts.upload_hls_to_spaces <hls-folder> <name>

<name> is the folder it lands in on Spaces, e.g. the title's slug. Uses the
DO_SPACES_* settings from .env.
"""

import mimetypes
import os
import sys
from concurrent.futures import ThreadPoolExecutor
import boto3
from botocore.client import Config
from app.core.config import settings

PREFIX = "wano-tv"
CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
}
# Segments never change once written; playlists might be regenerated.
CACHE_CONTROL = {
    ".ts": "public, max-age=31536000, immutable",
    ".m3u8": "public, max-age=300",
}


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    folder, name = sys.argv[1], sys.argv[2].strip("/")
    if not os.path.isfile(os.path.join(folder, "master.m3u8")):
        sys.exit(f"No master.m3u8 in {folder}; run scripts/mp4_to_hls.sh first")

    client = boto3.client(
        "s3",
        endpoint_url=settings.DO_SPACES_ENDPOINT,
        aws_access_key_id=settings.DO_SPACES_ACCESS_KEY_ID,
        aws_secret_access_key=settings.DO_SPACES_SECRET_KEY,
        config=Config(signature_version="s3v4"),
    )

    files = []
    for root, _, names in os.walk(folder):
        for filename in names:
            path = os.path.join(root, filename)
            relative = os.path.relpath(path, folder).replace(os.sep, "/")
            files.append((path, f"{PREFIX}/{name}/{relative}"))

    def upload(item):
        path, key = item
        ext = os.path.splitext(path)[1].lower()
        client.upload_file(
            path,
            settings.DO_SPACES_BUCKET_NAME,
            key,
            ExtraArgs={
                "ACL": "public-read",
                "ContentType": CONTENT_TYPES.get(ext)
                or mimetypes.guess_type(path)[0]
                or "application/octet-stream",
                "CacheControl": CACHE_CONTROL.get(ext, "public, max-age=300"),
            },
        )

    print(f"Uploading {len(files)} files to {PREFIX}/{name}/ ...")
    with ThreadPoolExecutor(max_workers=16) as pool:
        for done, _ in enumerate(pool.map(upload, files), start=1):
            if done % 100 == 0 or done == len(files):
                print(f"  {done}/{len(files)}")

    cdn = settings.DO_SPACES_CDN_URL.rstrip("/")
    print(f"Master playlist: {cdn}/{PREFIX}/{name}/master.m3u8")


if __name__ == "__main__":
    main()
