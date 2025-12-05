"""
app/services/cloudflare_stream.py

Helpers for uploading videos directly to Cloudflare Stream
and building URLs you can store in your VideoUrls schema.
"""

import io
import json
import logging
from typing import BinaryIO, Optional, Dict, Any

import requests
from fastapi import HTTPException

from app.core.config import settings

logger = logging.getLogger(__name__)


def _get_base_url() -> str:
    return settings.CLOUDFLARE_STREAM_API_BASE.rstrip("/")


def _auth_headers() -> dict:
    if not settings.CLOUDFLARE_STREAM_API_TOKEN:
        logger.error("CLOUDFLARE_STREAM_API_TOKEN is not set")
        raise HTTPException(
            status_code=500,
            detail="Cloudflare Stream API token not configured",
        )

    return {
        "Authorization": f"Bearer {settings.CLOUDFLARE_STREAM_API_TOKEN}",
    }


def upload_file_to_stream(
    file_obj: BinaryIO,
    filename: str,
    content_type: str = "video/mp4",
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Upload a video file directly to Cloudflare Stream.

    Uses basic upload:
        POST /accounts/{account_id}/stream
        multipart/form-data with field 'file'.

    Returns:
        dict: Cloudflare Stream "result" object (contains uid, status, etc.)
    """
    try:
        endpoint = (
            f"{_get_base_url()}/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/stream"
        )

        data: dict[str, str] = {}
        if meta:
            # Cloudflare Stream expects 'meta' as JSON string if provided
            data["meta"] = json.dumps(meta)

        files = {
            "file": (filename, file_obj, content_type),
        }

        resp = requests.post(
            endpoint,
            headers=_auth_headers(),
            data=data,
            files=files,
            timeout=300,  # allow large uploads
        )

        if resp.status_code >= 400:
            logger.error(
                "Cloudflare Stream upload failed: %s %s",
                resp.status_code,
                resp.text,
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to upload video to Cloudflare Stream",
            )

        data = resp.json()
        if not data.get("success", False):
            logger.error("Cloudflare Stream upload not successful: %s", data)
            raise HTTPException(
                status_code=500,
                detail="Cloudflare Stream returned an error during upload",
            )

        result = data.get("result") or data
        logger.info("Uploaded video to Cloudflare Stream, uid=%s", result.get("uid"))
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error uploading file to Cloudflare Stream")
        raise HTTPException(
            status_code=500,
            detail=f"Error uploading file to Cloudflare Stream: {str(e)}",
        )


def get_stream_video_details(uid: str) -> Dict[str, Any]:
    """
    Fetch full video details from Cloudflare Stream by UID.

    GET /accounts/{account_id}/stream/{uid}
    Response includes duration, size, playback, thumbnail, preview, etc.
    """
    try:
        endpoint = (
            f"{_get_base_url()}/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/stream/{uid}"
        )

        resp = requests.get(
            endpoint,
            headers=_auth_headers(),
            timeout=30,
        )

        if resp.status_code >= 400:
            logger.error(
                "Cloudflare Stream get video failed: %s %s",
                resp.status_code,
                resp.text,
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to fetch Cloudflare Stream video details",
            )

        data = resp.json()
        if not data.get("success", False):
            logger.error("Cloudflare Stream get video not successful: %s", data)
            raise HTTPException(
                status_code=500,
                detail="Cloudflare Stream returned an error when fetching video",
            )

        result = data.get("result") or data
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching Cloudflare Stream video details")
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching Cloudflare Stream video details: {str(e)}",
        )


def build_cloudflare_urls(uid: str, details: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """
    Build various URLs for your VideoUrls model from Cloudflare details.

    - original / download: direct MP4 download URL
    - hls: HLS manifest URL
    - dash: DASH manifest URL
    - thumbnail: thumbnail image URL
    - preview: preview/watch URL (if available)
    """
    playback = details.get("playback") or {}
    preview = details.get("preview")
    thumbnail = details.get("thumbnail")

    hls = playback.get("hls")
    dash = playback.get("dash")

    # Optional: use a customer code to construct URLs if needed
    customer_code = getattr(settings, "CLOUDFLARE_STREAM_CUSTOMER_CODE", None)
    if not hls and customer_code:
        hls = (
            f"https://customer-{customer_code}.cloudflarestream.com/"
            f"{uid}/manifest/video.m3u8"
        )
    if not dash and customer_code:
        dash = (
            f"https://customer-{customer_code}.cloudflarestream.com/"
            f"{uid}/manifest/video.mpd"
        )

    # Direct MP4 download / original
    original = f"https://videodelivery.net/{uid}/downloads/default.mp4"

    return {
        "uid": uid,
        "original": original,
        "download": original,
        "hls": hls,
        "dash": dash,
        "thumbnail": thumbnail,
        "preview": preview,
    }
