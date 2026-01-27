"""
Expo push notification helpers.
"""
from typing import Optional
import logging
import requests

logger = logging.getLogger(__name__)


EXPO_PUSH_ENDPOINT = "https://exp.host/--/api/v2/push/send"


def send_push_message(
    token: str,
    message: str,
    extra: Optional[dict] = None,
    title: Optional[str] = None,
    image_url: Optional[str] = None,
) -> None:
    """Send an Expo push notification."""
    payload: dict = {"to": token, "body": message}
    if title:
        payload["title"] = title
    if extra:
        payload["data"] = extra
    if image_url:
        payload["richContent"] = {"image": image_url}

    try:
        response = requests.post(
            EXPO_PUSH_ENDPOINT,
            json=payload,
            headers={
                "accept": "application/json",
                "accept-encoding": "gzip, deflate",
                "content-type": "application/json",
            },
            timeout=10,
        )
        response.raise_for_status()
        response_data = response.json()
        if response_data.get("errors"):
            logger.error("Expo push errors: %s", response_data.get("errors"))
    except requests.RequestException as exc:
        logger.exception("Expo push request failed: %s", exc)
