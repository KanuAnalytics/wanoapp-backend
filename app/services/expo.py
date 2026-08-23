"""
Expo push notification helpers.
"""
from typing import List, Optional
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


def send_push_batch(tokens: List[str], message: str, title: Optional[str] = None) -> None:
    """
    Send the same Expo push notification to many tokens in one request.
    Expo caps a single request at 100 messages - callers must chunk larger
    lists themselves before calling this.
    """
    if not tokens:
        return

    payload = [
        {"to": token, "body": message, **({"title": title} if title else {})}
        for token in tokens
    ]

    try:
        response = requests.post(
            EXPO_PUSH_ENDPOINT,
            json=payload,
            headers={
                "accept": "application/json",
                "accept-encoding": "gzip, deflate",
                "content-type": "application/json",
            },
            timeout=15,
        )
        response.raise_for_status()
        response_data = response.json()
        tickets = response_data.get("data")
        if isinstance(tickets, list):
            errors = [t for t in tickets if t.get("status") == "error"]
            if errors:
                logger.error("Expo push batch errors: %s", errors)
        elif response_data.get("errors"):
            logger.error("Expo push errors: %s", response_data.get("errors"))
    except requests.RequestException as exc:
        logger.exception("Expo push batch request failed: %s", exc)
