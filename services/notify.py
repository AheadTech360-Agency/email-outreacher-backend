"""An email to the team whenever a lead replies, whatever the reply, with a link to the conversation.

Sent through the SMTP account in SMTP_HOST / SMTP_PORT / SMTP_USERNAME / SMTP_PASSWORD, from EMAIL_FROM_NAME, to
every address in NOTIFY_EMAILS (comma-separated). The link opens the Replies page on that person's conversation;
the app's address is APP_URL, else the first of AUTHORIZED_URLS. Nothing set: nothing is sent.

It never raises: a mail server that's down is logged, and the reply is still recorded as usual.
"""

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any
from urllib.parse import quote

from config import Settings, get_settings

log = logging.getLogger("coldbrew.notify")
TIMEOUT_SECONDS = 20


def recipients(settings: Settings) -> list[str]:
    return [a.strip() for a in (settings.notify_emails or "").split(",") if a.strip()]


def configured(settings: Settings) -> bool:
    return bool(settings.smtp_host and settings.smtp_username and settings.smtp_password and recipients(settings))


def app_url(settings: Settings) -> str:
    return (settings.app_url or next(iter(settings.authorized_url_list), "")).rstrip("/")


def reply_link(settings: Settings, address: str) -> str:
    """The Replies page, opened on this person's conversation."""
    return f"{app_url(settings)}/replies?open={quote(address.strip().lower())}"


def reply_message(settings: Settings, lead: Any, campaign: str | None) -> EmailMessage:
    name = " ".join(p for p in (lead.first_name, lead.last_name) if p).strip() or lead.email
    who = f"{name} ({lead.email})" if name != lead.email else lead.email
    at = f" at {lead.company}" if lead.company else ""
    link = reply_link(settings, lead.email)
    msg = EmailMessage()
    msg["Subject"] = f"New reply from {name}{at}"
    msg["From"] = formataddr((settings.email_from_name or "Coldbrew", settings.smtp_username))
    msg["To"] = ", ".join(recipients(settings))
    lines = [f"{who}{at} replied.", ""]
    if campaign:
        lines += [f"Campaign: {campaign}", ""]
    lines += [f"Read the conversation: {link}", ""]
    msg.set_content("\n".join(lines))
    msg.add_alternative(
        f"<p>{_html(who)}{_html(at)} replied.</p>"
        + (f"<p>Campaign: {_html(campaign)}</p>" if campaign else "")
        + f'<p><a href="{_html(link)}">Read the conversation</a></p>',
        subtype="html",
    )
    return msg


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _send(settings: Settings, msg: EmailMessage) -> None:
    port = settings.smtp_port or 587
    if port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, port, timeout=TIMEOUT_SECONDS) as smtp:
            smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(msg)
        return
    with smtplib.SMTP(settings.smtp_host, port, timeout=TIMEOUT_SECONDS) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)


async def reply_received(lead: Any, campaign: str | None = None, settings: Settings | None = None) -> bool:
    """Email the team that this lead replied. True when it was sent; False when SMTP isn't set up or it failed."""
    settings = settings or get_settings()
    if not configured(settings) or not getattr(lead, "email", None):
        return False
    try:
        await asyncio.to_thread(_send, settings, reply_message(settings, lead, campaign))
    except Exception as exc:  # noqa: BLE001 - a mail problem must never stop the reply being recorded
        log.warning("couldn't send the reply email for %s: %s", lead.email, exc.__class__.__name__)
        return False
    log.info("reply email sent for lead %s", getattr(lead, "id", None))
    return True
