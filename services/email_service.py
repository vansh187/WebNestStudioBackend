import logging
import time

import httpx

from core.config import Settings

logger = logging.getLogger("webnest.email")

RESEND_API_URL = "https://api.resend.com/emails"
REQUEST_TIMEOUT_SECONDS = 10.0


class EmailService:
    """Sends transactional emails (OTP, lead notifications) via the Resend HTTP API.

    Uses plain HTTPS (port 443) rather than raw SMTP - some hosts (Render's
    free tier included) throttle or block outbound SMTP ports (25/465/587),
    which previously caused OTP emails to silently fail to send in production
    even though the exact same credentials worked from a local machine.

    Delivery is best-effort: a bad API key, network failure, or Resend outage
    is logged and swallowed so signup/lead-capture never fails because of a
    downstream email problem. Every attempt is logged (skip / success /
    failure) with the real reason, so delivery problems are diagnosable from
    server logs even though the caller never sees them.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def is_configured(self) -> bool:
        return bool(self._settings.resend_api_key)

    def connection_summary(self) -> tuple[str, str, bool]:
        """Returns (provider, from_address, is_configured) for diagnostics - never exposes the API key."""
        return "resend", self._settings.resend_from_address, self.is_configured()

    async def _send(self, to_address: str, subject: str, body: str) -> tuple[bool, str]:
        if not self.is_configured():
            message = "Skipping email: RESEND_API_KEY is not configured"
            logger.warning("%s (to=%s)", message, to_address)
            return False, message

        logger.info("Sending email to %s via Resend (subject=%r)", to_address, subject)
        started_at = time.monotonic()
        payload = {
            "from": self._settings.resend_from_address,
            "to": [to_address],
            "subject": subject,
            "text": body,
        }
        headers = {"Authorization": f"Bearer {self._settings.resend_api_key}"}
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.post(RESEND_API_URL, json=payload, headers=headers)
            elapsed = time.monotonic() - started_at

            if response.status_code in (200, 201):
                logger.info("Email sent to %s via Resend in %.2fs", to_address, elapsed)
                return True, "Email sent successfully."

            error_body = response.text[:500]
            logger.error(
                "Resend rejected email to %s after %.2fs: HTTP %s - %s",
                to_address,
                elapsed,
                response.status_code,
                error_body,
            )
            return False, f"Resend API returned HTTP {response.status_code}: {error_body}"
        except httpx.TimeoutException:
            logger.warning("Resend request to send email to %s timed out after %.2fs", to_address, time.monotonic() - started_at)
            return False, "Resend API request timed out"
        except httpx.HTTPError as exc:
            logger.warning("Resend request failed for %s: %s", to_address, exc, exc_info=True)
            return False, f"Resend API request failed: {exc}"

    async def send_otp_email(self, to_address: str, otp_code: str, purpose: str) -> bool:
        subject = "Your WebNest Studio verification code"
        body = f"Your one-time code for {purpose.replace('_', ' ')} is: {otp_code}\nThis code expires in {self._settings.otp_expire_minutes} minutes."
        sent, _detail = await self._send(to_address, subject, body)
        return sent

    async def send_lead_notification_email(
        self,
        full_name: str | None,
        email: str | None,
        phone_number: str | None,
        source: str,
        message: str | None,
    ) -> bool:
        if not self._settings.team_notification_email:
            logger.warning("Skipping lead notification email: TEAM_NOTIFICATION_EMAIL is not configured")
            return False
        subject = f"New lead - {source}"
        body = (
            f"New lead: {full_name}\n"
            f"Email: {email}\n"
            f"Phone: {phone_number}\n"
            f"Source: {source}\n"
            f"Message: {message}"
        )
        sent, _detail = await self._send(self._settings.team_notification_email, subject, body)
        return sent

    async def send_test_email(self, to_address: str) -> tuple[bool, str]:
        """Like send_otp_email, but surfaces the real success/failure detail -
        used by the admin test-email diagnostic endpoint."""
        subject = "WebNest Studio - test email"
        body = "This is a test email from the WebNest Studio backend to verify email delivery is working."
        return await self._send(to_address, subject, body)
