import logging
import smtplib
from email.mime.text import MIMEText

from core.config import Settings

logger = logging.getLogger("webnest.email")


class EmailService:
    """Sends transactional emails (OTP, lead notifications) over SMTP.

    Delivery is best-effort: a broken SMTP config or a transient send failure
    must never fail the request that triggered it (signup, lead capture).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _send(self, to_address: str, subject: str, body: str) -> None:
        if not self._settings.smtp_user or not self._settings.smtp_app_password:
            return
        message = MIMEText(body)
        message["Subject"] = subject
        message["From"] = self._settings.smtp_from_address or self._settings.smtp_user
        message["To"] = to_address
        try:
            with smtplib.SMTP(self._settings.smtp_host, self._settings.smtp_port) as server:
                server.starttls()
                server.login(self._settings.smtp_user, self._settings.smtp_app_password)
                server.send_message(message)
        except (smtplib.SMTPException, OSError):
            logger.warning("Failed to send email to %s", to_address, exc_info=True)

    def send_otp_email(self, to_address: str, otp_code: str, purpose: str) -> None:
        subject = "Your WebNest Studio verification code"
        body = f"Your one-time code for {purpose.replace('_', ' ')} is: {otp_code}\nThis code expires in {self._settings.otp_expire_minutes} minutes."
        self._send(to_address, subject, body)

    def send_lead_notification_email(
        self,
        full_name: str | None,
        email: str | None,
        phone_number: str | None,
        source: str,
        message: str | None,
    ) -> None:
        if not self._settings.team_notification_email:
            return
        subject = f"New lead - {source}"
        body = (
            f"New lead: {full_name}\n"
            f"Email: {email}\n"
            f"Phone: {phone_number}\n"
            f"Source: {source}\n"
            f"Message: {message}"
        )
        self._send(self._settings.team_notification_email, subject, body)
