"""The email warmer: mailboxes in the warming pool trade short, ordinary-looking emails with each other, and each one
treats what it receives the way a real person would: it opens it, takes it out of spam, stars some and replies to
some. That history is what makes mail providers trust a new mailbox. services/own_sending.py is the campaigns side.

Safety, as with campaigns:
- Nothing is sent unless OWN_WARMUP_LIVE is on; until then each warm-up email is only logged ("dry run").
- Only mailboxes in the warming pool take part, only ones whose connection test passed, and only with at least one
  other such mailbox in the workspace (they only ever email each other).
- Every warm-up email carries the X-Coldbrew-Warmup header, so the campaign reply reader never mistakes it for a reply.
"""

import asyncio
import imaplib
import logging
import random
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import SenderAccount, WarmupMessage
from services import mail_check
from services.own_mail import in_window
from services.own_settings import DEFAULTS, Settings, load_all
from services.vault import Vault

log = logging.getLogger("coldbrew.warmup")
HEADER = "X-Coldbrew-Warmup"
TIMEOUT_SECONDS = 30
# The window, the ramp and how often to star come from each workspace's Settings (services/own_settings.py).
ENGAGE_DAYS = 3
SPAM_NAMES = ("[gmail]/spam", "junk", "junk email", "junk e-mail", "spam", "bulk mail", "inbox.spam", "inbox.junk")

# ---------- What the emails say (plain, varied, nothing to trip a filter) ----------

SUBJECTS = [
    "Quick question about next week", "Thursday works", "Notes from today", "Following up on the samples", "Re-checking the schedule",
    "Lunch on Friday?", "Can you look at this when you get a sec", "Small change to the plan", "Thanks for yesterday",
    "The numbers you asked for", "Next steps", "Catching up", "Order timing", "About the shipment", "Agenda for Monday",
    "One more thing", "Draft for review", "Checking in", "Updated list", "Call later today?",
]  # fmt: skip
OPENERS = ["Hi {name},", "Hey {name},", "Hello {name},", "{name},", "Morning {name},"]
LINES = [
    "Just wanted to check whether Thursday afternoon still works for you.",
    "I went through the list again and everything lines up on our side.",
    "Could you send over the latest version when you have a minute?",
    "The samples came in today and they look good.",
    "I moved our call to 3 pm, let me know if that's a problem.",
    "We're on track for the end of the month.",
    "I added a couple of notes to the doc, nothing major.",
    "Happy to go with your suggestion on this one.",
    "The team liked the second option best.",
    "No rush on this, whenever you get to it is fine.",
    "I'll pull the numbers together and send them tomorrow.",
    "Thanks again for getting back so quickly.",
    "Let me know if anything looks off.",
    "I think we can wrap this up by Friday.",
]  # fmt: skip
CLOSERS = ["Thanks!", "Talk soon.", "Cheers,", "Best,", "Thanks,", "Speak later."]
REPLIES = [
    "Thanks, that works for me.", "Got it, appreciate it.", "Sounds good, talk then.", "Perfect, thanks for sending.",
    "Yes, Thursday is fine.", "Makes sense, let's do that.", "Thanks! I'll take a look today.", "Great, that helps a lot.",
    "Will do. Thanks for the heads up.", "All good on my end.",
]  # fmt: skip


def first_name(account: SenderAccount) -> str:
    name = (account.from_name or account.from_email.split("@")[0]).strip()
    return (name.split()[0] if name else "there").replace(".", " ").split()[0].capitalize()


def compose(sender: SenderAccount, to: SenderAccount, rng: random.Random) -> tuple[str, str]:
    body = "\n\n".join([
        rng.choice(OPENERS).format(name=first_name(to)),
        " ".join(rng.sample(LINES, rng.randint(1, 3))),
        f"{rng.choice(CLOSERS)}\n{first_name(sender)}",
    ])  # fmt: skip
    return rng.choice(SUBJECTS), body


def build(sender: SenderAccount, to: SenderAccount, subject: str, body: str, in_reply_to: str | None = None) -> tuple[EmailMessage, str]:
    msg = EmailMessage()
    message_id = make_msgid(domain=sender.from_email.rsplit("@", 1)[-1])
    msg["Message-ID"] = message_id
    msg["From"] = formataddr((sender.from_name, sender.from_email)) if sender.from_name else sender.from_email
    msg["To"] = to.from_email
    msg["Subject"] = subject
    msg[HEADER] = message_id.strip("<>").split("@")[0]
    if in_reply_to:
        msg["In-Reply-To"] = msg["References"] = in_reply_to
    msg.set_content(body + "\n")
    return msg, message_id


# ---------- How many, and when ----------


def target_today(account: SenderAccount, now: datetime, s: Settings = DEFAULTS) -> int:
    """The day's warm-up sends: a new mailbox starts at the workspace's start-per-day and adds its ramp each day, up to
    the mailbox's own target."""
    started = account.warmup_started_at or now
    days = max(0, (now - started).days)
    return min(account.warmup_daily_target, s.warmup_start_per_day + s.warmup_ramp_per_day * days)


def send_now(sent_today: int, target: int, now: datetime, tick_minutes: int, rng: random.Random, window: dict | None = None) -> bool:
    """Whether this tick sends one: inside the warm-up window, the day's remaining sends spread evenly over the
    window's remaining ticks."""
    from zoneinfo import ZoneInfo

    window = window or DEFAULTS.warmup_window
    if sent_today >= target or not in_window(window, now):
        return False
    local = now.astimezone(ZoneInfo(window["timezone"]))
    end_h, end_m = (int(x) for x in window["end"].split(":"))
    minutes_left = (end_h * 60 + end_m) - (local.hour * 60 + local.minute)
    ticks_left = max(1, minutes_left // tick_minutes)
    return rng.random() < (target - sent_today) / ticks_left


# ---------- Sending ----------


@dataclass
class WarmSummary:
    sent: int = 0
    dry_run: int = 0
    failed: int = 0
    opened: int = 0
    rescued: int = 0
    replied: int = 0
    notes: list[str] = field(default_factory=list)


def _deliver(account: SenderAccount, password: str, message: EmailMessage) -> None:
    from services.own_sending import smtp_send

    smtp_send(account, password, message)


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def warm_send(
    session: AsyncSession,
    live: bool,
    now: datetime | None = None,
    tick_minutes: int = 5,
    deliver: Callable[[SenderAccount, str, EmailMessage], None] = _deliver,
    rng: random.Random | None = None,
) -> WarmSummary:
    """One warm-up tick's sending: each connected warming mailbox may send one email to another in its workspace."""
    now = now or datetime.now(UTC)
    rng = rng or random.Random()
    out = WarmSummary()
    vault = Vault(session)
    accounts = list(await session.scalars(select(SenderAccount).where(SenderAccount.pool == "warming", SenderAccount.status == "ok")))
    settings = await load_all(session)
    by_ws: dict[UUID, list[SenderAccount]] = {}
    for a in accounts:
        by_ws.setdefault(a.workspace_id, []).append(a)
    for ws, pool in by_ws.items():
        s = settings.get(ws, DEFAULTS)
        if not s.warmup_enabled:
            continue
        if len(pool) < 2:
            out.notes.append(f"workspace {ws}: warming needs at least two connected warming mailboxes")
            continue
        counted = WarmupMessage.status.in_(("sent", "dry_run"))
        ws_today = int(await session.scalar(select(func.count()).select_from(WarmupMessage).where(
            WarmupMessage.workspace_id == ws, WarmupMessage.sent_at >= _day_start(now), counted)) or 0)  # fmt: skip
        for account in pool:
            if s.warmup_workspace_daily_cap is not None and ws_today >= s.warmup_workspace_daily_cap:
                out.notes.append(f"workspace {ws}: warm-up daily cap ({s.warmup_workspace_daily_cap}) reached")
                break
            sent_today = int(await session.scalar(select(func.count()).select_from(WarmupMessage).where(
                WarmupMessage.from_account_id == account.id, WarmupMessage.sent_at >= _day_start(now), counted)) or 0)  # fmt: skip
            last = await session.scalar(select(func.max(WarmupMessage.sent_at)).where(WarmupMessage.from_account_id == account.id, counted))
            if last is not None and now - last < timedelta(minutes=s.warmup_min_gap_minutes):
                continue  # this mailbox's own gap between warm-up emails
            if not send_now(sent_today, target_today(account, now, s), now, tick_minutes, rng, s.warmup_window):
                continue
            ws_today += 1
            to = rng.choice([a for a in pool if a.id != account.id])
            subject, body = compose(account, to, rng)
            message, message_id = build(account, to, subject, body)
            row = WarmupMessage(workspace_id=ws, from_account_id=account.id, to_account_id=to.id, message_id=message_id, subject=subject,
                                status="dry_run" if not live else "sent", sent_at=now)  # fmt: skip
            if live:
                try:
                    password = await vault.read(account.smtp_secret_id)
                    if not password:
                        raise smtplib.SMTPAuthenticationError(535, b"No saved password")
                    await asyncio.to_thread(deliver, account, password, message)
                except Exception as exc:  # noqa: BLE001 - recorded; never stops the tick
                    row.status = "failed"
                    row.error = mail_check._friendly(exc, "SMTP", mail_check.Endpoint(account.smtp_host, account.smtp_port, account.smtp_security, "", ""))
                    if isinstance(exc, smtplib.SMTPAuthenticationError):
                        account.status, account.last_error, account.last_checked_at = "failed", row.error, now
                    out.failed += 1
            session.add(row)
            if row.status == "sent":
                out.sent += 1
            elif row.status == "dry_run":
                out.dry_run += 1
            await session.commit()
    return out


# ---------- Engaging: what the receiving mailbox does ----------


@dataclass
class Found:
    message_id: str
    folder: str
    rescued: bool = False
    starred: bool = False


def _folders(imap: imaplib.IMAP4) -> tuple[str, list[str]]:
    """INBOX and the spam folder(s), whatever this provider calls them."""
    spam: list[str] = []
    status, rows = imap.list()
    for raw in (rows or []) if status == "OK" else []:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        name = line.rsplit(' "', 1)[-1].rstrip('"') if '"' in line else line.split()[-1]
        if "\\Junk" in line or name.lower() in SPAM_NAMES:
            spam.append(name)
    return "INBOX", list(dict.fromkeys(spam))


def engage_mailbox(account: SenderAccount, password: str, pending: set[str], star_chance: float, rng: random.Random) -> list[Found]:
    """Open the pending warm-up emails in this mailbox: any in spam is moved to the inbox, each is marked read, some are
    starred. Returns what was done, by Message-ID. Nothing else in the mailbox is touched."""
    if account.imap_security == "ssl":
        imap = imaplib.IMAP4_SSL(account.imap_host, account.imap_port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT_SECONDS)
    else:
        imap = imaplib.IMAP4(account.imap_host, account.imap_port, timeout=TIMEOUT_SECONDS)
        imap.starttls(ssl_context=ssl.create_default_context())
    done: list[Found] = []
    try:
        imap.login(account.imap_username, password)
        inbox, spam = _folders(imap)
        since = (datetime.now(UTC) - timedelta(days=ENGAGE_DAYS)).strftime("%d-%b-%Y")
        for folder in [*spam, inbox]:
            if imap.select(f'"{folder}"')[0] != "OK":
                continue
            status, data = imap.uid("SEARCH", None, "SINCE", since, "HEADER", HEADER, '""')
            if status != "OK" or not data or not data[0]:
                continue
            for uid in data[0].split():
                status, parts = imap.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                header = next((p[1] for p in parts or [] if isinstance(p, tuple)), b"").decode(errors="replace")
                mid = header.split(":", 1)[-1].strip() if ":" in header else ""
                if mid not in pending:
                    continue
                found = Found(mid, "spam" if folder in spam else "inbox")
                imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")
                if rng.random() < star_chance:
                    imap.uid("STORE", uid, "+FLAGS", "(\\Flagged)")
                    found.starred = True
                if found.folder == "spam":
                    # Out of spam: MOVE where the server has it, else copy and remove.
                    moved = "MOVE" in imap.capabilities and imap.uid("MOVE", uid, inbox)[0] == "OK"
                    if not moved and imap.uid("COPY", uid, inbox)[0] == "OK":
                        imap.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                        imap.expunge()
                        moved = True
                    found.rescued = moved
                done.append(found)
                pending.discard(mid)
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
    return done


async def warm_engage(
    session: AsyncSession,
    live: bool,
    now: datetime | None = None,
    engage: Callable[[SenderAccount, str, set[str], float, random.Random], list[Found]] = engage_mailbox,
    deliver: Callable[[SenderAccount, str, EmailMessage], None] = _deliver,
    rng: random.Random | None = None,
) -> WarmSummary:
    """One warm-up tick's engaging: each warming mailbox opens what it received, rescues it from spam, stars some and
    replies to its share (warmup_reply_percent) in the same thread. Only live: in a dry run nothing was sent."""
    now = now or datetime.now(UTC)
    rng = rng or random.Random()
    out = WarmSummary()
    if not live:
        return out
    vault = Vault(session)
    accounts = list(await session.scalars(select(SenderAccount).where(SenderAccount.pool == "warming", SenderAccount.status == "ok",
                                                                      SenderAccount.imap_host.is_not(None))))  # fmt: skip
    settings = await load_all(session)
    for account in accounts:
        s = settings.get(account.workspace_id, DEFAULTS)
        if not s.warmup_enabled:
            continue
        rows = {r.message_id: r for r in await session.scalars(select(WarmupMessage).where(
            WarmupMessage.to_account_id == account.id, WarmupMessage.status == "sent", WarmupMessage.opened_at.is_(None),
            WarmupMessage.sent_at >= now - timedelta(days=ENGAGE_DAYS)))}  # fmt: skip
        if not rows:
            continue
        password = await vault.read(account.imap_secret_id)
        if not password:
            continue
        try:
            found = await asyncio.to_thread(engage, account, password, set(rows), s.warmup_star_percent / 100, rng)
        except Exception as exc:  # noqa: BLE001 - one mailbox failing never stops the others
            out.notes.append(f"{account.from_email}: {exc.__class__.__name__}")
            continue
        smtp_password = await vault.read(account.smtp_secret_id)
        for f in found:
            row = rows[f.message_id]
            row.opened_at, row.landed_in, row.starred, row.rescued_from_spam = now, f.folder, f.starred, f.rescued
            out.opened += 1
            out.rescued += int(f.rescued)
            # A reply to its share, never to a reply (threads stay two emails long).
            if smtp_password and row.reply_to_id is None and rng.random() * 100 < account.warmup_reply_percent:
                sender = await session.get(SenderAccount, row.from_account_id)
                if sender is None:
                    continue
                subject = row.subject if row.subject.lower().startswith("re:") else f"Re: {row.subject}"
                body = f"{rng.choice(REPLIES)}\n\n{first_name(account)}"
                message, message_id = build(account, sender, subject, body, in_reply_to=row.message_id)
                reply = WarmupMessage(workspace_id=account.workspace_id, from_account_id=account.id, to_account_id=sender.id, message_id=message_id,
                                      reply_to_id=row.id, subject=subject, status="sent", sent_at=now)  # fmt: skip
                try:
                    await asyncio.to_thread(deliver, account, smtp_password, message)
                    row.replied_at = now
                    out.replied += 1
                except Exception as exc:  # noqa: BLE001
                    reply.status, reply.error = "failed", exc.__class__.__name__
                session.add(reply)
        await session.commit()
    return out


# ---------- Stats for the dashboard ----------


async def stats(session: AsyncSession, account_ids: list[UUID], now: datetime | None = None) -> dict[UUID, dict[str, Any]]:
    """Per mailbox: warm-up emails sent today, and over the last 7 days received, landed in the inbox vs spam,
    rescued and replied to. Inbox rate is inbox / (inbox + spam) of what it received and opened."""
    now = now or datetime.now(UTC)
    out = {i: {"sent_today": 0, "received_7d": 0, "inbox_7d": 0, "spam_7d": 0, "rescued_7d": 0, "replied_7d": 0} for i in account_ids}
    if not account_ids:
        return out
    for account_id, n in await session.execute(
        select(WarmupMessage.from_account_id, func.count()).where(WarmupMessage.from_account_id.in_(account_ids), WarmupMessage.sent_at >= _day_start(now),
                                                                  WarmupMessage.status.in_(("sent", "dry_run"))).group_by(WarmupMessage.from_account_id)
    ):  # fmt: skip
        out[account_id]["sent_today"] = n
    week = now - timedelta(days=7)
    rows = await session.execute(
        select(WarmupMessage.to_account_id, func.count(), func.count().filter(WarmupMessage.landed_in == "inbox"),
               func.count().filter(WarmupMessage.landed_in == "spam"), func.count().filter(WarmupMessage.rescued_from_spam),
               func.count().filter(WarmupMessage.replied_at.is_not(None)))
        .where(WarmupMessage.to_account_id.in_(account_ids), WarmupMessage.sent_at >= week, WarmupMessage.status == "sent")
        .group_by(WarmupMessage.to_account_id)
    )  # fmt: skip
    for account_id, received, inbox, spam, rescued, replied in rows:
        out[account_id].update(received_7d=received, inbox_7d=inbox, spam_7d=spam, rescued_7d=rescued, replied_7d=replied)
    return out
