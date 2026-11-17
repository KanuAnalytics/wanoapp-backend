"""Files for Wano TV titles kept on Spaces (Admin > Wano TV titles).

- Artwork (posters, landscape pictures, episode pictures): sent through the
  server (save_art), checked, stored public in the title's folder.
- Subtitles for an HLS film on Spaces: added as a WebVTT track inside the
  film's stream (iOS ignores separate subtitle files for HLS). Same method as
  scripts/add_hls_subtitles.py, which now calls this.
Offline copies (downloads) always come from Cloudflare Stream (see tv_admin.offline_copy).
"""

import math
import re
import uuid
from typing import Optional

from app.core.config import settings
from app.services import tv_stream

GROUP = "subs"
# ffmpeg's MPEG-TS muxer starts timestamps 1.4 s in (126000 at 90 kHz), so
# that's where 00:00:00.000 of the captions sits in the video segments.
MPEGTS_START = 126000
LANG = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
IMAGE_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
MAX_CAPTION_CHARS = 2_000_000


class FileError(Exception):
    pass


def _put_object(key: str, body: bytes, content_type: str) -> None:
    tv_stream._client().put_object(Bucket=settings.DO_SPACES_BUCKET_NAME, Key=key, Body=body,
                                   ACL=tv_stream.acl_for(key), ContentType=content_type,
                                   CacheControl="public, max-age=300")


def cdn_url(key: str) -> str:
    return f"{settings.DO_SPACES_CDN_URL.rstrip('/')}/{settings.DO_SPACES_BUCKET_NAME}/{key}"


def captions_to_vtt(text: str) -> str:
    """SRT or WebVTT text as WebVTT cues without a header."""
    if not isinstance(text, str) or not text.strip():
        raise FileError("The subtitle file is empty")
    if len(text) > MAX_CAPTION_CHARS:
        raise FileError("The subtitle file is too big")
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").strip()
    if text.startswith("WEBVTT"):
        text = text.split("\n\n", 1)[1] if "\n\n" in text else ""
    text = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text).strip()
    if "-->" not in text:
        raise FileError("That doesn't look like an .srt or .vtt subtitle file")
    return text


def full_vtt(text: str) -> str:
    return "WEBVTT\n\n" + captions_to_vtt(text) + "\n"


def add_spaces_subtitles(folder: str, text: str, language: str, label: str) -> str:
    """Adds (or replaces) a subtitle track in the HLS film at wano-tv/<folder>/.
    Returns the subtitle playlist URL to store on the title. Blocking."""
    if not LANG.match(language or ""):
        raise FileError("Language must be a code like en or fr")
    label = (label or "").strip()[:40] or language
    if '"' in label or "\n" in label:
        raise FileError("The label can't contain quotes")
    base = f"{tv_stream.PREFIX}{folder.strip('/')}"
    cues = captions_to_vtt(text)
    try:
        master = tv_stream._fetch_object(f"{base}/master.m3u8")
    except KeyError:
        raise FileError("The film's master.m3u8 wasn't found in its folder")
    lines = master.splitlines()
    try:
        first_variant = next(lines[i + 1] for i, line in enumerate(lines) if line.startswith("#EXT-X-STREAM-INF:"))
    except (StopIteration, IndexError):
        raise FileError("The film's playlist has no video in it")
    variant = tv_stream._fetch_object(f"{base}/{first_variant.strip()}")
    duration = sum(float(m) for m in re.findall(r"#EXTINF:([\d.]+)", variant)) or 1.0

    sub_dir = f"subs/{language}"
    vtt = f"WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:{MPEGTS_START},LOCAL:00:00:00.000\n\n{cues}\n"
    playlist = "\n".join(["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{math.ceil(duration)}",
                          "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD", f"#EXTINF:{duration:.3f},",
                          "captions.vtt", "#EXT-X-ENDLIST", ""])
    _put_object(f"{base}/{sub_dir}/captions.vtt", vtt.encode("utf-8"), "text/vtt")
    _put_object(f"{base}/{sub_dir}/index.m3u8", playlist.encode("utf-8"), "application/vnd.apple.mpegurl")
    media = (f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="{GROUP}",NAME="{label}",LANGUAGE="{language}",'
             f'DEFAULT=NO,AUTOSELECT=YES,FORCED=NO,URI="{sub_dir}/index.m3u8"')
    out = []
    for line in lines:
        if line.startswith("#EXT-X-MEDIA:TYPE=SUBTITLES") and f'LANGUAGE="{language}"' in line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:") and "SUBTITLES=" not in line:
            line += f',SUBTITLES="{GROUP}"'
        out.append(line)
    first_stream = next(i for i, line in enumerate(out) if line.startswith("#EXT-X-STREAM-INF:"))
    out.insert(first_stream, media)
    _put_object(f"{base}/master.m3u8", ("\n".join(out) + "\n").encode("utf-8"), "application/vnd.apple.mpegurl")
    tv_stream.clear_cache()
    return cdn_url(f"{base}/{sub_dir}/index.m3u8")


def remove_spaces_subtitles(folder: str, language: str) -> None:
    if not LANG.match(language or ""):
        raise FileError("Language must be a code like en or fr")
    base = f"{tv_stream.PREFIX}{folder.strip('/')}"
    master = tv_stream._fetch_object(f"{base}/master.m3u8")
    out = [line for line in master.splitlines()
           if not (line.startswith("#EXT-X-MEDIA:TYPE=SUBTITLES") and f'LANGUAGE="{language}"' in line)]
    _put_object(f"{base}/master.m3u8", ("\n".join(out) + "\n").encode("utf-8"), "application/vnd.apple.mpegurl")
    tv_stream.clear_cache()


MAX_ART_BYTES = 10 * 1024 * 1024


def save_art(slug: str, filename: str, data: bytes) -> str:
    """Stores a picture in the title's folder (public) and returns its URL. Blocking."""
    if not SLUG.match(slug or ""):
        raise FileError("Bad title address")
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    if ext not in IMAGE_TYPES:
        raise FileError("Pictures must be .jpg, .png or .webp")
    if not data or len(data) > MAX_ART_BYTES:
        raise FileError("Pictures must be under 10 MB")
    sniff = {"jpg": b"\xff\xd8", "jpeg": b"\xff\xd8", "png": b"\x89PNG", "webp": b"RIFF"}[ext]
    if not data.startswith(sniff):
        raise FileError("That file isn't the picture its name says")
    key = f"{tv_stream.PREFIX}{slug}/art/{uuid.uuid4().hex[:12]}.{ext}"
    _put_object(key, data, IMAGE_TYPES[ext])
    return cdn_url(key)
