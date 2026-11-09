"""Sending the briefing as a multipart email (plain text and HTML from the same data).

The caller chooses the Message-ID (one per delivery), so a resend after a crash carries
the same ID as the copy that may already have gone, and mail clients can drop the second.
"""

import smtplib
from email.message import EmailMessage
from typing import Any, Protocol

from briefing_engine.config import Settings
from briefing_engine.delivery.render import RenderedEmail


class SendError(Exception):
    """A failed send. `permanent` (an SMTP 5xx) means trying again can't help."""

    def __init__(self, message: str, *, permanent: bool):
        super().__init__(message)
        self.permanent = permanent


class Mailer(Protocol):
    def send(self, to: str, email: RenderedEmail, message_id: str) -> None:
        """Send the email, or raise SendError."""
        ...


def build_message(sender: str, to: str, email: RenderedEmail, message_id: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = email.subject
    message["Message-ID"] = message_id
    message.set_content(email.text)
    message.add_alternative(email.html, subtype="html")
    return message


def classify_smtp_error(exc: Exception) -> SendError:
    """5xx replies are permanent; 4xx replies, disconnects and network errors are not."""
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _ in exc.recipients.values()]
        return SendError(f"recipients refused: {codes}", permanent=all(c >= 500 for c in codes))
    if isinstance(exc, smtplib.SMTPResponseException):
        reply = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else ""
        return SendError(f"SMTP {exc.smtp_code}: {reply}"[:500], permanent=exc.smtp_code >= 500)
    return SendError(f"{type(exc).__name__}: {exc}"[:500], permanent=False)


# SES API errors that no retry can fix: the message itself, or the sender, is refused.
SES_PERMANENT = frozenset(
    {
        "MessageRejected",
        "MailFromDomainNotVerifiedException",
        "AccountSuspendedException",
        "NotFoundException",
        "BadRequestException",
    }
)


def classify_ses_error(code: str, message: str) -> SendError:
    """SES_PERMANENT codes are final. Throttling, sending pauses and quotas are worth a retry."""
    return SendError(f"SES {code}: {message}"[:500], permanent=code in SES_PERMANENT)


class SesMailer:
    """Stage 2 on AWS: Amazon SES through its API, signed with the task role's credentials.

    SES replaces the Message-ID we set with its own, so a resend after a crash can't be
    recognised by mail clients as a duplicate. The delivery state machine still prevents
    every resend except the crash-between-send-and-commit case (§8.3).
    """

    def __init__(self, settings: Settings, client: Any = None):
        self._settings = settings
        if client is None:
            import boto3  # only the AWS deployment needs it at run time

            client = boto3.client("sesv2", region_name=settings.aws_region)
        self._client = client

    def send(self, to: str, email: RenderedEmail, message_id: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        message = build_message(self._settings.mail_from, to, email, message_id)
        try:
            self._client.send_email(
                Destination={"ToAddresses": [to]},
                Content={"Raw": {"Data": message.as_bytes()}},
            )
        except ClientError as exc:
            error = exc.response.get("Error", {})
            raise classify_ses_error(
                str(error.get("Code", "Unknown")), str(error.get("Message", ""))
            ) from exc
        except BotoCoreError as exc:  # network trouble, expired credentials: try again later
            raise SendError(f"{type(exc).__name__}: {exc}"[:500], permanent=False) from exc


def make_mailer(settings: Settings) -> Mailer:
    return SesMailer(settings) if settings.mail_transport == "ses" else SmtpMailer(settings)


class SmtpMailer:
    """Stage 1: SMTP. Mailpit locally; a real mailbox once Q3 is settled."""

    def __init__(self, settings: Settings):
        self._settings = settings

    def send(self, to: str, email: RenderedEmail, message_id: str) -> None:
        s = self._settings
        message = build_message(s.mail_from, to, email, message_id)
        smtp_class = smtplib.SMTP_SSL if s.smtp_use_ssl else smtplib.SMTP
        try:
            with smtp_class(s.smtp_host, s.smtp_port, timeout=30) as smtp:
                if s.smtp_username and s.smtp_password:
                    smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise classify_smtp_error(exc) from exc
