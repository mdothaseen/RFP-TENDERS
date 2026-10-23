"""Email the daily digest over SMTP (Gmail needs an App Password)."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from ..config import env


MAX_ATTACH_BYTES = 20 * 1024 * 1024  # Gmail rejects mails over 25 MB


def send(digest, attachments: list[Path | None], recipients: list[str] | None = None,
         message_id: str | None = None) -> str:
    host = env("SMTP_HOST", "smtp.gmail.com")
    port = int(env("SMTP_PORT", "587") or 587)
    user = env("SMTP_USER")
    password = env("SMTP_PASSWORD")
    to = recipients if recipients is not None else [a.strip() for a in env("EMAIL_TO").split(",") if a.strip()]
    if not (user and password and to):
        return "skipped (SMTP_USER / SMTP_PASSWORD / EMAIL_TO not set)"

    msg = EmailMessage()
    msg["Subject"] = digest.subject
    msg["From"] = env("EMAIL_FROM") or user
    # Each address gets its own delivery (for retries), but the header shows everyone the alert goes to.
    everyone = [a.strip() for a in env("EMAIL_TO").split(",") if a.strip()]
    msg["To"] = ", ".join(dict.fromkeys(everyone + to))
    msg["Date"] = formatdate(localtime=True)
    if message_id:
        msg["Message-ID"] = message_id
    msg.set_content(digest.text)
    body = digest.html if digest.html.lstrip().startswith("<html") else f"<html><body>{digest.html}</body></html>"
    msg.add_alternative(body, subtype="html")
    total = 0
    for attachment in attachments:
        if not (attachment and Path(attachment).exists()):
            continue
        data = Path(attachment).read_bytes()
        if total + len(data) > MAX_ATTACH_BYTES:
            continue
        total += len(data)
        msg.add_attachment(data, maintype="application",
                           subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           filename=Path(attachment).name)

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as server:
            server.login(user, password)
            refused = server.send_message(msg, to_addrs=to)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
    else:
        with smtplib.SMTP(host, port, timeout=60) as server:
            server.starttls(context=context)
            server.login(user, password)
            refused = server.send_message(msg, to_addrs=to)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
    return f"sent to {len(to)} address(es)"
