"""Checking a sender account's SMTP and IMAP details by signing in: nothing is ever sent or changed in the mailbox.

The host comes from a person, so it's checked first: it must resolve to a public address. A private, loopback or
link-local one (this server's own network, a cloud metadata address) is refused, so the check can't be used to probe
what's behind the server.
"""

import asyncio
import imaplib
import ipaddress
import smtplib
import socket
import ssl
from dataclasses import dataclass

TIMEOUT_SECONDS = 15


@dataclass
class Endpoint:
    host: str
    port: int
    security: str  # ssl | starttls
    username: str
    password: str


@dataclass
class CheckResult:
    smtp_error: str | None
    imap_error: str | None  # None when it worked, or when IMAP isn't set up (imap_checked says which)
    imap_checked: bool

    @property
    def ok(self) -> bool:
        return self.smtp_error is None and self.imap_error is None


def public_host(host: str) -> str | None:
    """Why the host can't be used, or None when every address it resolves to is public."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return f"Couldn't find {host}. Check the host name."
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return f"{host} points to a private address, which isn't allowed."
    return None


def _friendly(exc: Exception, kind: str, e: Endpoint) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError) or (isinstance(exc, imaplib.IMAP4.error) and "auth" in str(exc).lower()):
        return f"{kind}: the server refused the username or password. Google Workspace and Microsoft 365 need an app password."
    if isinstance(exc, imaplib.IMAP4.error):
        return f"{kind}: the server refused the sign-in ({str(exc)[:120]})."
    if isinstance(exc, ssl.SSLError):
        return f"{kind}: couldn't start a secure connection on port {e.port}. Port 465 and 993 use SSL; 587 uses STARTTLS."
    if isinstance(exc, socket.timeout | TimeoutError):
        return f"{kind}: no answer from {e.host}:{e.port}. Check the host and port."
    if isinstance(exc, ConnectionRefusedError):
        return f"{kind}: {e.host} refused the connection on port {e.port}."
    if isinstance(exc, smtplib.SMTPException):
        return f"{kind}: the server answered with an error ({str(exc)[:120]})."
    return f"{kind}: couldn't connect ({exc.__class__.__name__})."


def check_smtp(e: Endpoint) -> str | None:
    if reason := public_host(e.host):
        return f"SMTP: {reason}"
    try:
        if e.security == "ssl":
            with smtplib.SMTP_SSL(e.host, e.port, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context()) as smtp:
                smtp.login(e.username, e.password)
        else:
            with smtplib.SMTP(e.host, e.port, timeout=TIMEOUT_SECONDS) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(e.username, e.password)
    except Exception as exc:  # noqa: BLE001 - every failure becomes a message for the person
        return _friendly(exc, "SMTP", e)
    return None


def check_imap(e: Endpoint) -> str | None:
    if reason := public_host(e.host):
        return f"IMAP: {reason}"
    try:
        if e.security == "ssl":
            imap = imaplib.IMAP4_SSL(e.host, e.port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT_SECONDS)
        else:
            imap = imaplib.IMAP4(e.host, e.port, timeout=TIMEOUT_SECONDS)
            imap.starttls(ssl_context=ssl.create_default_context())
        try:
            imap.login(e.username, e.password)
        finally:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        return _friendly(exc, "IMAP", e)
    return None


async def check(smtp: Endpoint, imap: Endpoint | None) -> CheckResult:
    """Sign in to both, side by side. Nothing is sent."""
    smtp_error, imap_error = await asyncio.gather(
        asyncio.to_thread(check_smtp, smtp), asyncio.to_thread(check_imap, imap) if imap else asyncio.sleep(0, None)
    )
    return CheckResult(smtp_error, imap_error, imap is not None)
