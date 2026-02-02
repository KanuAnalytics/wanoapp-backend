"""
Twilio WhatsApp messaging service wrapper

app/services/twilio_whatsapp_service.py
"""
import json
import logging
from typing import Any, Dict, Optional

from twilio.rest import Client

from app.core.config import settings

logger = logging.getLogger(__name__)


class TwilioWhatsAppServiceError(Exception):
    """Raised when Twilio WhatsApp message requests fail."""


class TwilioWhatsAppService:
    def __init__(self) -> None:
        self.account_sid = getattr(settings, "TWILIO_ACCOUNT_SID", None)
        self.auth_token = getattr(settings, "TWILIO_AUTH_TOKEN", None)
        self.from_number = getattr(settings, "TWILIO_WHATSAPP_FROM", None)
        self.content_sid = getattr(settings, "TWILIO_WHATSAPP_CONTENT_SID", None)
        self.is_configured = bool(self.account_sid and self.auth_token and self.from_number and self.content_sid)

        if self.is_configured:
            self.client = Client(self.account_sid, self.auth_token)
        else:
            self.client = None
            logger.warning(
                "Twilio WhatsApp not configured. Missing TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, or TWILIO_WHATSAPP_CONTENT_SID."
            )

    async def send_template_message(
        self,
        to: str,
        content_variables: Optional[Dict[str, Any]] = None,
        content_sid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a WhatsApp template message using Content API."""

        variables_payload = json.dumps(content_variables or {})
        message_content_sid = content_sid or self.content_sid
        message_to = f"whatsapp:{to}"

        try:
            message = self.client.messages.create(
                from_=self.from_number,
                to=message_to,
                content_sid=message_content_sid,
                content_variables=variables_payload,
            )
        except Exception as exc:
            logger.error("Twilio WhatsApp send failed: %s", exc)
            raise TwilioWhatsAppServiceError("Failed to send WhatsApp message") from exc

        return {
            "sid": message.sid,
            "status": message.status,
            "to": message.to,
            "from": message.from_,
            "content_sid": message_content_sid,
        }


twilio_whatsapp_service = TwilioWhatsAppService()
