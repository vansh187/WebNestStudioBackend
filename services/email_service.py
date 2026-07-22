import logging
import smtplib
import time
from email.mime.text import MIMEText

from core.config import Settings

logger = logging.getLogger("webnest.email")


class EmailService:
    """Sends transactional emails (OTP, lead notifications) over SMTP.

    Delivery is best-effort: a broken SMTP config or a transient send failure
    must never fail the request that triggered it (signup, lead capture).
    Every attempt is logged (skip / success / failure) so delivery problems
    are visible in the server logs even though the caller never sees them.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def default_from_address(self) -> str:
        return self._settings.smtp_from_address or self._settings.smtp_user

    def is_configured(self) -> bool:
        return bool(self._settings.smtp_user and self._settings.smtp_app_password)

    def connection_summary(self) -> tuple[str, int, bool]:
        """Returns (host, port, is_configured) for diagnostics - never exposes the password."""
        return self._settings.smtp_host, self._settings.smtp_port, self.is_configured()

    def _send(self, to_address: str, subject: str, body: str) -> bool:
        if not self._settings.smtp_user or not self._settings.smtp_app_password:
            logger.warning(
                "Skipping email to %s: SMTP_USER or SMTP_APP_PASSWORD is not configured", to_address
            )
            return False

        message = MIMEText(body)
        message["Subject"] = subject
        message["From"] = self._settings.smtp_from_address or self._settings.smtp_user
        message["To"] = to_address

        logger.info(
            "Sending email to %s via %s:%s (subject=%r)",
            to_address,
            self._settings.smtp_host,
            self._settings.smtp_port,
            subject,
        )
        started_at = time.monotonic()
        try:
            with smtplib.SMTP(self._settings.smtp_host, self._settings.smtp_port, timeout=10) as server:
                server.starttls()
                server.login(self._settings.smtp_user, self._settings.smtp_app_password)
                server.send_message(message)
            logger.info("Email sent to %s in %.2fs", to_address, time.monotonic() - started_at)
            return True
        except smtplib.SMTPAuthenticationError:
            logger.error(
                "SMTP authentication rejected for %s after %.2fs - check SMTP_USER/SMTP_APP_PASSWORD "
                "(Gmail requires a 16-character App Password, not the account password)",
                self._settings.smtp_user,
                time.monotonic() - started_at,
                exc_info=True,
            )
            return False
        except (smtplib.SMTPException, OSError, TimeoutError):
            logger.warning(
                "Failed to send email to %s after %.2fs", to_address, time.monotonic() - started_at, exc_info=True
            )
            return False

    def send_otp_email(self, to_address: str, otp_code: str, purpose: str) -> bool:
        subject = "Your WebNest Studio verification code"
        body = f"Your one-time code for {purpose.replace('_', ' ')} is: {otp_code}\nThis code expires in {self._settings.otp_expire_minutes} minutes."
        return self._send(to_address, subject, body)

    def send_lead_notification_email(
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
        return self._send(self._settings.team_notification_email, subject, body)
