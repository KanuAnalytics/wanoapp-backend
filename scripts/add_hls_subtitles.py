"""
Adds a subtitle track to an HLS title already uploaded to Spaces under
<DO_SPACES_BUCKET_NAME>/wano-tv/<folder>/, and prints the subtitle URL.

iOS players only show subtitles that are part of the HLS stream (sidecar
files are ignored for .m3u8), so the captions are uploaded as a WebVTT
rendition under subs/<language>/ and listed in master.m3u8 with
EXT-X-MEDIA. Android's player reads the same track. Re-running replaces the
track for that language.

Run from the repo root:
    python -m scripts.add_hls_subtitles <folder> <captions.srt|.vtt> <language> <label>
e.g.
    python -m scripts.add_hls_subtitles one-field-v2 ONE_FIELD_Film_EN_Captions.srt en English
"""

import math
import re
import sys
import boto3
from botocore.client import Config
from app.core.config import settings

PREFIX = "wano-tv"
GROUP = "subs"
# ffmpeg's MPEG-TS muxer starts timestamps 1.4 s in (126000 at 90 kHz), so
# that's where 00:00:00.000 of the captions sits in the video segments.
MPEGTS_START = 126000


def read_captions(path: str) -> str:
    """The cues as WebVTT, without a header."""
    with open(path, "r", encoding="utf-8-sig") as f:
        text = f.read().replace("\r\n", "\n").strip()
    if text.startswith("WEBVTT"):
        text = text.split("\n\n", 1)[1] if "\n\n" in text else ""
    # SRT timestamps use a comma before the milliseconds; WebVTT uses a dot.
    return re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text).strip()


def main():
    if len(sys.argv) != 5:
        print(__doc__)
        sys.exit(1)
    folder, captions_path, language, label = sys.argv[1:]
    folder = folder.strip("/")

    client = boto3.client(
        "s3",
        endpoint_url=settings.DO_SPACES_ENDPOINT,
        aws_access_key_id=settings.DO_SPACES_ACCESS_KEY_ID,
        aws_secret_access_key=settings.DO_SPACES_SECRET_KEY,
        config=Config(signature_version="s3v4"),
    )
    bucket = settings.DO_SPACES_BUCKET_NAME
    base = f"{PREFIX}/{folder}"

    def get(key: str) -> str:
        return client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")

    def put(key: str, body: str, content_type: str):
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ACL="public-read",
            ContentType=content_type,
            CacheControl="public, max-age=300",
        )

    master = get(f"{base}/master.m3u8")
    lines = master.splitlines()

    # The subtitle segment has to cover the whole film; take its length from
    # the first quality's playlist.
    first_variant = next(
        lines[i + 1] for i, line in enumerate(lines)
        if line.startswith("#EXT-X-STREAM-INF:")
    )
    variant = get(f"{base}/{first_variant}")
    duration = sum(
        float(m) for m in re.findall(r"#EXTINF:([\d.]+)", variant)
    )

    sub_dir = f"subs/{language}"
    vtt = (
        "WEBVTT\n"
        f"X-TIMESTAMP-MAP=MPEGTS:{MPEGTS_START},LOCAL:00:00:00.000\n\n"
        + read_captions(captions_path)
        + "\n"
    )
    playlist = "\n".join([
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{math.ceil(duration)}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXTINF:{duration:.3f},",
        "captions.vtt",
        "#EXT-X-ENDLIST",
        "",
    ])
    put(f"{base}/{sub_dir}/captions.vtt", vtt, "text/vtt")
    put(f"{base}/{sub_dir}/index.m3u8", playlist, "application/vnd.apple.mpegurl")

    # Rebuild the master: drop any earlier entry for this language, list the
    # track, and point every quality at the subtitle group.
    media = (
        f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="{GROUP}",NAME="{label}",'
        f'LANGUAGE="{language}",DEFAULT=NO,AUTOSELECT=YES,FORCED=NO,'
        f'URI="{sub_dir}/index.m3u8"'
    )
    out = []
    for line in lines:
        if line.startswith("#EXT-X-MEDIA:TYPE=SUBTITLES") and f'LANGUAGE="{language}"' in line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:") and "SUBTITLES=" not in line:
            line += f',SUBTITLES="{GROUP}"'
        out.append(line)
    # EXT-X-MEDIA goes before the first quality.
    first_stream = next(i for i, line in enumerate(out) if line.startswith("#EXT-X-STREAM-INF:"))
    out.insert(first_stream, media)
    put(f"{base}/master.m3u8", "\n".join(out) + "\n", "application/vnd.apple.mpegurl")

    cdn = settings.DO_SPACES_CDN_URL.rstrip("/")
    print(f"Added {label} subtitles: {cdn}/{bucket}/{base}/{sub_dir}/captions.vtt")
    print("The CDN may serve the old master.m3u8 for up to 5 minutes.")


if __name__ == "__main__":
    main()
