"""Sending the briefing as a multipart email (plain text and HTML from the same data)."""

import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Protocol

from briefing_engine.config import Settings
from briefing_engine.delivery.render import RenderedEmail


class Mailer(Protocol):
    def send(self, to: str, email: RenderedEmail) -> str:
        """Send the email and return its Message-ID."""
        ...


def build_message(sender: str, to: str, email: RenderedEmail) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = email.subject
    message["Message-ID"] = make_msgid(domain="briefing-engine.local")
    message.set_content(email.text)
    message.add_alternative(email.html, subtype="html")
    return message


class SmtpMailer:
    """Stage 1: SMTP. Mailpit locally; a real mailbox once Q3 is settled."""

    def __init__(self, settings: Settings):
        self._settings = settings

    def send(self, to: str, email: RenderedEmail) -> str:
        s = self._settings
        message = build_message(s.mail_from, to, email)
        smtp_class = smtplib.SMTP_SSL if s.smtp_use_ssl else smtplib.SMTP
        with smtp_class(s.smtp_host, s.smtp_port, timeout=30) as smtp:
            if s.smtp_username and s.smtp_password:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(message)
        return str(message["Message-ID"])
