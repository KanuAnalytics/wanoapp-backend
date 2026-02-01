"""
Twilio Verify service wrapper

app/services/twilio_verify_service.py
"""
import logging
from typing import Any, Dict

from twilio.rest import Client

from app.core.config import settings

logger = logging.getLogger(__name__)


class TwilioVerifyServiceError(Exception):
    """Raised when Twilio Verify requests fail."""


class TwilioVerifyService:
    def __init__(self) -> None:
        self.account_sid = getattr(settings, "TWILIO_ACCOUNT_SID", None)
        self.auth_token = getattr(settings, "TWILIO_AUTH_TOKEN", None)
        self.service_sid = getattr(settings, "TWILIO_VERIFY_SERVICE_SID", None)
        self.is_configured = bool(self.account_sid and self.auth_token and self.service_sid)

        if self.is_configured:
            self.client = Client(self.account_sid, self.auth_token)
        else:
            self.client = None
            logger.warning(
                "Twilio Verify not configured. Missing TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, or TWILIO_VERIFY_SERVICE_SID."
            )

    async def send_whatsapp_verification(self, to: str) -> Dict[str, Any]:
        """Send a WhatsApp verification code via Twilio Verify."""
        return await self.send_verification(to=to, channel="whatsapp")

    async def send_verification(self, to: str, channel: str = "whatsapp") -> Dict[str, Any]:
        """Send a verification code using Twilio Verify."""
        if not self.is_configured:
            return {"mock": True, "to": to, "channel": channel}

        try:
            verification = self.client.verify.v2.services(self.service_sid).verifications.create(
                to=to,
                channel=channel,
            )
        except Exception as exc:
            logger.error("Twilio Verify send failed: %s", exc)
            raise TwilioVerifyServiceError("Failed to send verification") from exc

        return {
            "sid": verification.sid,
            "status": verification.status,
            "to": verification.to,
            "channel": verification.channel,
            "account_sid": verification.account_sid,
            "service_sid": verification.service_sid,
        }

    async def check_verification(self, to: str, code: str) -> Dict[str, Any]:
        """Check a verification code using Twilio Verify."""
        if not self.is_configured:
            return {"mock": True, "to": to, "code": code, "status": "approved", "valid": True}

        try:
            verification_check = (
                self.client.verify.v2.services(self.service_sid).verification_checks.create(
                    to=to,
                    code=code,
                )
            )
        except Exception as exc:
            logger.error("Twilio Verify check failed: %s", exc)
            raise TwilioVerifyServiceError("Failed to verify code") from exc

        return {
            "sid": verification_check.sid,
            "status": verification_check.status,
            "to": verification_check.to,
            "account_sid": verification_check.account_sid,
            "service_sid": verification_check.service_sid,
            "valid": getattr(verification_check, "valid", None),
        }


twilio_verify_service = TwilioVerifyService()
