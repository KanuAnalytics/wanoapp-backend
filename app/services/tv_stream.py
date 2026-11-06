"""
Protected Wano TV playback.

Films used to be public files on the Spaces CDN: anyone with the address could
download the whole film. Now the catalogue hands the app a link to this
backend instead, carrying a signed token that expires after TOKEN_TTL_SECONDS:

    {BACKEND_URL}/api/v1/tv/stream/<token>/master.m3u8

That endpoint reads the playlist from Spaces (privately), and rewrites it:
sub-playlists (quality levels, subtitles) point back through the same token;
video segments get Spaces links signed for the rest of the token's life.
Anything pointing outside the title's own folder is dropped. The player needs
no headers, so app builds old and new play it unchanged.

Trailers, posters and artwork stay public. After deploying (and checking a film
plays on a phone), make the film files private with
    python -m scripts.protect_wano_tv            # dry run
    python -m scripts.protect_wano_tv --apply
New film uploads are private already (acl_for).
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import posixpath
import re
import time
from typing import Optional
from urllib.parse import urlparse

from app.core.config import settings

logger = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 6 * 3600
# Segment links outlive the token so a film started late, or paused, keeps
# playing; capped by the S3v4 maximum.
MIN_SEGMENT_SECONDS = 4 * 3600
MAX_SEGMENT_SECONDS = 7 * 24 * 3600
PLAYLIST_CACHE_SECONDS = 300
PLAYLIST_CACHE_MAX = 1000
PREFIX = "wano-tv/"
SPACES_HOST_SUFFIX = ".digitaloceanspaces.com"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)*\.m3u8$")
_URI_ATTR = re.compile(r'URI="([^"]*)"')
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")

_cache: dict = {}


class NotFound(Exception):
    pass


def clear_cache():
    _cache.clear()
    _locks.clear()


# ---------------------------------------------------------------- tokens

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _key() -> bytes:
    return hashlib.sha256(("wano-tv-stream:" + str(settings.SECRET_KEY)).encode()).digest()


def sign(title_id: str, viewer: str, exp: int) -> str:
    who = hashlib.sha256(str(viewer).encode()).hexdigest()[:8]
    payload = f"{title_id}.{int(exp)}.{who}".encode()
    sig = hmac.new(_key(), payload, hashlib.sha256).digest()[:16]
    return f"{_b64(payload)}.{_b64(sig)}"


def verify(token: str) -> str:
    """The title id, or ValueError if the token is forged, malformed or expired."""
    if not isinstance(token, str) or len(token) > 200 or token.count(".") != 1:
        raise ValueError("bad token")
    p, s = token.split(".")
    try:
        payload, sig = _unb64(p), _unb64(s)
    except Exception:
        raise ValueError("bad token")
    good = hmac.new(_key(), payload, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(sig, good):
        raise ValueError("bad signature")
    try:
        title_id, exp, _ = payload.decode().split(".")
        exp = int(exp)
    except Exception:
        raise ValueError("bad token")
    if exp < time.time() or not re.fullmatch(r"[a-f0-9]{24}", title_id):
        raise ValueError("expired")
    return title_id


# ---------------------------------------------------------------- storage

def spaces_key(url) -> Optional[str]:
    """The Spaces key of a Wano TV file from its CDN or origin URL, or None."""
    if not isinstance(url, str):
        return None
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme != "https" or not (p.hostname or "").endswith(SPACES_HOST_SUFFIX):
        return None
    path = p.path or ""
    i = path.find("/" + PREFIX)
    if i < 0:
        return None
    key = posixpath.normpath(path[i + 1:])
    return key if key.startswith(PREFIX) and ".." not in key.split("/") else None


def _client():
    import boto3
    from botocore.client import Config
    return boto3.client("s3", endpoint_url=settings.DO_SPACES_ENDPOINT,
                        aws_access_key_id=settings.DO_SPACES_ACCESS_KEY_ID,
                        aws_secret_access_key=settings.DO_SPACES_SECRET_KEY,
                        config=Config(signature_version="s3v4"))


def _fetch_object(key: str) -> str:
    from botocore.exceptions import ClientError
    try:
        obj = _client().get_object(Bucket=settings.DO_SPACES_BUCKET_NAME, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
            raise KeyError(key)
        raise
    return obj["Body"].read().decode("utf-8")


def _presign(key: str, expires_in: int) -> str:
    return _client().generate_presigned_url("get_object", Params={"Bucket": settings.DO_SPACES_BUCKET_NAME, "Key": key},
                                            ExpiresIn=int(expires_in))


_locks: dict = {}


async def _playlist(key: str) -> str:
    """Cached; when many players start at once, one reads storage and the rest wait for it."""
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < PLAYLIST_CACHE_SECONDS:
        return hit[1]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < PLAYLIST_CACHE_SECONDS:
            return hit[1]
        try:
            text = await asyncio.to_thread(_fetch_object, key)
        except KeyError:
            raise NotFound(key)
        if len(_cache) >= PLAYLIST_CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.monotonic(), text)
    if len(_locks) > PLAYLIST_CACHE_MAX * 2:
        _locks.clear()
    return text


# ---------------------------------------------------------------- links

def _base() -> str:
    return str(getattr(settings, "BACKEND_URL", "") or "").rstrip("/")


def stream_url(title_id: str, viewer: str, key: str) -> Optional[str]:
    """Signed playback link for a file in the title's folder (e.g. its master)."""
    folder = title_folder(key)
    if not folder or not key.endswith(".m3u8"):
        return None
    token = sign(title_id, viewer, int(time.time()) + TOKEN_TTL_SECONDS)
    return f"{_base()}/api/v1/tv/stream/{token}/{key[len(folder):]}"


def title_folder(key: Optional[str]) -> Optional[str]:
    """wano-tv/<folder>/ for a title's master playlist key."""
    if not key or not key.startswith(PREFIX):
        return None
    parts = key.split("/")
    return "/".join(parts[:2]) + "/" if len(parts) >= 3 else None


def _resolve(uri: str, here: str, folder: str) -> Optional[str]:
    uri = uri.strip()
    if not uri:
        return None
    if re.match(r"^[a-z]+://", uri, re.I):
        key = spaces_key(uri)
    elif uri.startswith("/"):
        return None
    else:
        key = posixpath.normpath(posixpath.join(here, uri))
    if not key or not key.startswith(folder) or ".." in key.split("/"):
        return None
    return key


def rewrite(text: str, key: str, folder: str, token: str, exp: int) -> str:
    here = posixpath.dirname(key)
    ttl = min(MAX_SEGMENT_SECONDS, max(MIN_SEGMENT_SECONDS, int(exp - time.time())))

    def link(k: str) -> str:
        if k.endswith(".m3u8"):
            return f"{_base()}/api/v1/tv/stream/{token}/{k[len(folder):]}"
        return _presign(k, ttl)

    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            bad = False

            def sub(m):
                nonlocal bad
                k = _resolve(m.group(1), here, folder)
                if not k:
                    bad = True
                    return m.group(0)
                return f'URI="{link(k)}"'
            new = _URI_ATTR.sub(sub, s)
            if not bad:
                out.append(new)
            continue
        k = _resolve(s, here, folder)
        if not k:
            # Drop the URI and the #EXTINF / #EXT-X-STREAM-INF that described it.
            while out and (out[-1].startswith("#EXTINF") or out[-1].startswith("#EXT-X-STREAM-INF")
                           or out[-1].startswith("#EXT-X-BYTERANGE")):
                out.pop()
            continue
        out.append(link(k))
    return "\n".join(out) + "\n"


async def serve(db, token: str, path: str) -> str:
    """The rewritten playlist, or raises ValueError (403), NotFound (404)."""
    from bson import ObjectId
    title_id = verify(token)
    if len(path) > 200 or not _SAFE_PATH.match(path):
        raise NotFound(path)
    title = await db.tv_titles.find_one({"_id": ObjectId(title_id), "is_active": True}, {"video_url": 1})
    folder = title_folder(spaces_key((title or {}).get("video_url")))
    if not folder:
        raise NotFound(title_id)
    key = folder + path
    exp = int(_unb64(token.split(".")[0]).decode().split(".")[1])
    return rewrite(await _playlist(key), key, folder, token, exp)


# ---------------------------------------------------------------- locking files

def acl_for(key: str) -> str:
    """Films (and their subtitles) private; trailers and artwork public."""
    k = key.lower()
    if "/trailer/" in k or k.endswith(_IMAGE_EXT):
        return "public-read"
    return "private"


class SpacesAdapter:
    def __init__(self):
        self.c = _client()

    def list(self, prefix):
        pages = self.c.get_paginator("list_objects_v2").paginate(Bucket=settings.DO_SPACES_BUCKET_NAME, Prefix=prefix)
        for page in pages:
            for obj in page.get("Contents", []):
                yield obj["Key"]

    def set_private(self, key):
        self.c.put_object_acl(Bucket=settings.DO_SPACES_BUCKET_NAME, Key=key, ACL="private")


def protect_films(client, apply: bool = False) -> dict:
    private = [k for k in client.list(PREFIX) if acl_for(k) == "private"]
    if apply:
        for k in private:
            client.set_private(k)
    return {"private": private, "applied": apply}
