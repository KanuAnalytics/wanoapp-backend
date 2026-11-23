"""Wano TV films on Cloudflare Stream (uploaded from Admin > Wano TV titles).

Films made with Nasir's scripts live on Spaces as HLS (see tv_stream). Films
uploaded from the admin page go straight to Cloudflare Stream instead, which
does the encoding, so nobody needs ffmpeg on their computer.

- Uploads are resumable (tus), marked "requireSignedURLs", so the film can't
  be watched from a copied link.
- Playback: one signed token per film, made through the Stream API and kept
  for TOKEN_REUSE_SECONDS (the token lives TOKEN_TTL_SECONDS), so a busy title
  makes a handful of API calls a day, not one per viewer.
- Subtitles: uploaded to Stream, which adds them to the film's HLS stream (the
  only way iOS shows them).
- Offline copies: Stream makes an MP4 when asked (enable_download); the app
  downloads it through a token marked downloadable.

A film is stored on the title as video_url = MANIFEST.format(uid) plus
cf_uid = uid (episodes the same). Network calls are blocking; call them through
asyncio.to_thread. Tests replace _request.
"""

import base64
import logging
import re
import time
from typing import Optional

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)

API = "https://api.cloudflare.com/client/v4"
HOST = "https://videodelivery.net"
MANIFEST = HOST + "/{}/manifest/video.m3u8"
TOKEN_TTL_SECONDS = 12 * 3600
TOKEN_REUSE_SECONDS = 6 * 3600
MAX_FILM_SECONDS = 6 * 3600
MAX_FILM_BYTES = 30 * 1024 ** 3          # Stream's own limit is 30 GB
UID = re.compile(r"^[a-f0-9]{32}$")
LANG = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")

_tokens: dict = {}


class CfError(Exception):
    pass


def clear_cache():
    _tokens.clear()


def valid_uid(uid) -> bool:
    return isinstance(uid, str) and bool(UID.match(uid))


def _account() -> str:
    acc = settings.CLOUDFLARE_ACCOUNT_ID
    if not acc or not settings.CLOUDFLARE_STREAM_API_TOKEN:
        raise CfError("Cloudflare Stream isn't set up on the server")
    return acc


def _request(method: str, path: str, **kw) -> requests.Response:
    headers = {"Authorization": f"Bearer {settings.CLOUDFLARE_STREAM_API_TOKEN}", **kw.pop("headers", {})}
    return requests.request(method, f"{API}/accounts/{_account()}/stream{path}", headers=headers, timeout=30, **kw)


def _ok(r: requests.Response, what: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 300 or not data.get("success", r.status_code < 300):
        logger.warning("Cloudflare %s failed: %s %s", what, r.status_code, str(data)[:300])
        raise CfError(f"Cloudflare couldn't {what} ({r.status_code})")
    return data.get("result") or {}


def upload_url(name: str, size: int) -> dict:
    """A resumable (tus) upload for one film. {upload_url, uid}."""
    if not isinstance(size, int) or size <= 0 or size > MAX_FILM_BYTES:
        raise CfError("The film must be under 30 GB")

    def b64(s: str) -> str:
        return base64.b64encode(s.encode()).decode()
    meta = ",".join([f"name {b64('wano-tv/' + name[:150])}", f"maxdurationseconds {b64(str(MAX_FILM_SECONDS))}",
                     "requiresignedurls"])
    r = _request("POST", "?direct_user=true", headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(size),
                                                        "Upload-Metadata": meta})
    if r.status_code not in (200, 201):
        logger.warning("Cloudflare tus create failed: %s %s", r.status_code, r.text[:300])
        raise CfError(f"Cloudflare couldn't start the upload ({r.status_code})")
    uid = r.headers.get("stream-media-id")
    loc = r.headers.get("Location")
    if not valid_uid(uid) or not loc:
        raise CfError("Cloudflare didn't return an upload address")
    return {"upload_url": loc, "uid": uid, "video_url": MANIFEST.format(uid)}


def status(uid: str) -> dict:
    """{ready, state, duration_minutes, pct} for an uploaded film."""
    res = _ok(_request("GET", f"/{uid}"), "read the film")
    st = res.get("status") or {}
    dur = res.get("duration")
    return {"ready": bool(res.get("readyToStream")), "state": st.get("state") or "",
            "pct": st.get("pctComplete"), "error": st.get("errorReasonText") or "",
            "duration_minutes": int(round(dur / 60)) if isinstance(dur, (int, float)) and dur > 0 else None}


def token(uid: str, downloadable: bool = False, now: Optional[float] = None) -> str:
    now = now or time.time()
    key = (uid, downloadable)
    hit = _tokens.get(key)
    if hit and now - hit[1] < TOKEN_REUSE_SECONDS:
        return hit[0]
    body = {"exp": int(now) + TOKEN_TTL_SECONDS}
    if downloadable:
        body["downloadable"] = True
    tok = _ok(_request("POST", f"/{uid}/token", json=body), "sign the film").get("token")
    if not isinstance(tok, str) or not tok:
        raise CfError("Cloudflare didn't sign the film")
    _tokens[key] = (tok, now)
    if len(_tokens) > 2000:
        _tokens.clear()
    return tok


def play_url(uid: str) -> str:
    return MANIFEST.format(token(uid))


def enable_download(uid: str) -> dict:
    """Ask Stream to make the MP4 used for offline copies. {ready, pct}."""
    res = _ok(_request("POST", f"/{uid}/downloads"), "make the offline copy")
    d = res.get("default") or {}
    return {"ready": d.get("status") == "ready", "pct": d.get("percentComplete")}


def download_status(uid: str) -> dict:
    res = _ok(_request("GET", f"/{uid}/downloads"), "check the offline copy")
    d = res.get("default") or {}
    return {"ready": d.get("status") == "ready", "pct": d.get("percentComplete")}


def download_url(uid: str) -> str:
    return f"{HOST}/{token(uid, downloadable=True)}/downloads/default.mp4"


def put_captions(uid: str, language: str, vtt: str) -> None:
    if not LANG.match(language or ""):
        raise CfError("Language must be a code like en or fr")
    files = {"file": ("captions.vtt", vtt.encode("utf-8"), "text/vtt")}
    _ok(_request("PUT", f"/{uid}/captions/{language}", files=files), "add the subtitles")


def delete_captions(uid: str, language: str) -> None:
    if not LANG.match(language or ""):
        raise CfError("Language must be a code like en or fr")
    _ok(_request("DELETE", f"/{uid}/captions/{language}"), "remove the subtitles")


def clip(uid: str, start: float, end: float, name: str) -> str:
    """tv_clip_it: Stream cuts a new, public video out of a film (no copy made
    by Wano). Returns the clip's uid; it becomes ready like any upload (the
    webhook / stuck-upload rescue mark its post ready)."""
    body = {"clippedFromVideoUID": uid, "startTimeSeconds": int(start), "endTimeSeconds": int(end),
            "meta": {"name": ("wano-tv-clip/" + name)[:120]}, "requireSignedURLs": False}
    res = _ok(_request("POST", "/clip", json=body), "make the clip")
    new = res.get("uid")
    if not valid_uid(new):
        raise CfError("Cloudflare didn't return the clip")
    return new
