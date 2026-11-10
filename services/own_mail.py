"""Coldbrew's own sending, the parts with no database: filling in a step's text, building the email (threaded under the
first one), the sending window, and telling what an incoming email is (a reply, a bounce, an unsubscribe, an
out-of-office). services/own_sending.py runs them against the database; routers/campaigns.py serves the dashboard.
"""

import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from email.message import EmailMessage, Message
from email.utils import formataddr, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from zoneinfo import ZoneInfo

# ---------- Text ----------

VARIABLE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*(?:\|([^}]*))?\}\}")
VARIABLES = {
    "firstName": "Their first name",
    "lastName": "Their last name",
    "company": "Their company",
    "title": "Their job title",
    "email1Subject": "The subject of the first email Coldbrew wrote for them",
    "email1Body": "The body of the first email Coldbrew wrote for them",
    "senderName": "The sending mailbox's name (or its persona's)",
    "signature": "The persona's signature",
    "followUp": "A follow-up Coldbrew writes for this lead with your follow-up prompts (emails 2 to 5 only)",
}
# Steps {{followUp}} can be used in: Coldbrew has a follow-up prompt for each (follow-up 2 to 5).
FOLLOW_UP_STEPS = range(2, 6)


class MissingValue(Exception):
    """A step uses a value this lead doesn't have, with no fallback ({{firstName|there}} gives one)."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def render(template: str, values: dict[str, str]) -> str:
    """{{name}} is replaced by its value; {{name|fallback}} uses the fallback when the value is empty. A value that's
    empty with no fallback raises MissingValue, so an email never goes out with a hole in it."""

    def one(m: re.Match) -> str:
        name, fallback = m.group(1), m.group(2)
        value = (values.get(name) or "").strip()
        if value:
            return value
        if fallback is not None:
            return fallback.strip()
        raise MissingValue(name)

    return VARIABLE.sub(one, template)


def unknown_variables(template: str) -> list[str]:
    """Names in {{...}} that aren't variables, so a typo is caught when the step is saved, not when it sends."""
    return sorted({m.group(1) for m in VARIABLE.finditer(template)} - set(VARIABLES))


# ---------- The email ----------


@dataclass
class Outgoing:
    from_name: str
    from_email: str
    to_email: str
    subject: str
    body: str
    signature: str = ""
    # The first email's Message-ID, for a follow-up in the same thread.
    in_reply_to: str | None = None


def build_message(o: Outgoing) -> tuple[EmailMessage, str]:
    """The email, plain text, with its own Message-ID (returned: replies point back at it). A follow-up carries
    In-Reply-To and References so it lands in the same thread. Every email can be unsubscribed from in one click."""
    msg = EmailMessage()
    domain = o.from_email.rsplit("@", 1)[-1]
    message_id = make_msgid(domain=domain)
    msg["Message-ID"] = message_id
    msg["From"] = formataddr((o.from_name, o.from_email)) if o.from_name else o.from_email
    msg["To"] = o.to_email
    msg["Subject"] = o.subject
    if o.in_reply_to:
        msg["In-Reply-To"] = o.in_reply_to
        msg["References"] = o.in_reply_to
    msg["List-Unsubscribe"] = f"<mailto:{o.from_email}?subject=unsubscribe>"
    body = o.body.rstrip()
    if o.signature.strip():
        body = f"{body}\n\n{o.signature.strip()}"
    msg.set_content(body + "\n")
    return msg, message_id


def reply_subject(first_subject: str) -> str:
    return first_subject if first_subject.lower().startswith("re:") else f"Re: {first_subject}"


# ---------- When ----------

DEFAULT_SCHEDULE = {"timezone": "America/New_York", "start": "09:00", "end": "17:00", "days": [1, 2, 3, 4, 5, 6]}


def _clock(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def clean_schedule(raw: dict | None) -> dict:
    """A sending window that can be used: a real timezone, start before end, days 1 (Monday) to 7 (Sunday)."""
    s = {**DEFAULT_SCHEDULE, **(raw or {})}
    ZoneInfo(s["timezone"])  # raises for an unknown zone
    if _clock(s["start"]) >= _clock(s["end"]):
        raise ValueError("The sending window must start before it ends.")
    days = sorted({int(d) for d in s["days"]})
    if not days or any(d < 1 or d > 7 for d in days):
        raise ValueError("Pick at least one day to send on.")
    return {"timezone": s["timezone"], "start": s["start"], "end": s["end"], "days": days}


def in_window(schedule: dict, now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(schedule["timezone"]))
    return local.isoweekday() in schedule["days"] and _clock(schedule["start"]) <= local.time() < _clock(schedule["end"])


def next_send_time(schedule: dict, after: datetime, rng: random.Random | None = None) -> datetime:
    """A random moment inside the window, on or after `after`: the first sending day it fits in. Spreading the
    times keeps a mailbox from sending in bursts."""
    rng = rng or random.Random()
    zone = ZoneInfo(schedule["timezone"])
    start, end = _clock(schedule["start"]), _clock(schedule["end"])
    local = after.astimezone(zone)
    for offset in range(0, 15):
        day = (local + timedelta(days=offset)).date()
        if day.isoweekday() not in schedule["days"]:
            continue
        open_at = datetime.combine(day, start, zone)
        close_at = datetime.combine(day, end, zone)
        earliest = max(open_at, local) if offset == 0 else open_at
        if earliest >= close_at:
            continue
        span = (close_at - earliest).total_seconds()
        # Most of the day's sends land in its first part, so a day's work isn't pushed past closing.
        return (earliest + timedelta(seconds=rng.uniform(0, span * 0.6))).astimezone(UTC)
    raise ValueError("No sending day in the next two weeks.")


# ---------- What came in ----------

BOUNCE_FROM = re.compile(r"^(mailer-daemon|postmaster|mail-daemon|mailerdaemon)@", re.I)
AUTO_SUBJECT = re.compile(r"^(automatic reply|auto(matic)?[- ]?(reply|response)|out of (the )?office|ooo\b|away from|abwesenheit|réponse automatique)", re.I)
UNSUBSCRIBE = re.compile(r"\b(unsubscribe|remove me|take me off|stop (emailing|contacting|sending)|do not (email|contact)|don'?t (email|contact) me|opt[ -]?out)\b", re.I)
QUOTE_START = re.compile(r"^(on .{0,200}wrote:|-{2,}\s*original message|from:\s)", re.I | re.M)
MESSAGE_ID = re.compile(r"<[^<>\s]+>")


@dataclass
class Inbound:
    type: str  # reply | bounce | unsubscribe | auto_reply
    message_id: str
    from_email: str
    subject: str
    snippet: str
    received_at: datetime
    # Message-IDs it points at: ours, for matching it to the email it answers.
    refers_to: list[str] = field(default_factory=list)
    # For a bounce: whose address failed.
    failed_recipient: str | None = None


def _text_of(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    return part.get_content() if hasattr(part, "get_content") else part.get_payload(decode=True).decode(errors="replace")
                except Exception:  # noqa: BLE001
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(errors="replace")
        return ""
    try:
        return msg.get_content() if hasattr(msg, "get_content") else (msg.get_payload(decode=True) or b"").decode(errors="replace")
    except Exception:  # noqa: BLE001
        return (msg.get_payload(decode=True) or b"").decode(errors="replace")


def own_words(text: str) -> str:
    """What they wrote, without the quoted email under it."""
    m = QUOTE_START.search(text)
    kept = text[: m.start()] if m else text
    return "\n".join(line for line in kept.splitlines() if not line.lstrip().startswith(">")).strip()


def _failed_recipient(msg: Message) -> str | None:
    if header := msg.get("X-Failed-Recipients"):
        return header.split(",")[0].strip().lower()
    for part in msg.walk():
        if part.get_content_type() == "message/delivery-status":
            for block in part.get_payload() if isinstance(part.get_payload(), list) else []:
                for key in ("Final-Recipient", "Original-Recipient"):
                    if value := block.get(key):
                        return value.split(";")[-1].strip().lower()
    return None


def _references(msg: Message) -> list[str]:
    ids: list[str] = []
    for header in ("In-Reply-To", "References"):
        ids += MESSAGE_ID.findall(msg.get(header) or "")
    # A bounce carries the original email inside it: its Message-ID is the one we sent.
    for part in msg.walk():
        if part.get_content_type() in ("message/rfc822", "text/rfc822-headers"):
            inner = part.get_payload()
            inner = inner[0] if isinstance(inner, list) and inner else inner
            if isinstance(inner, Message):
                ids += MESSAGE_ID.findall(inner.get("Message-ID") or "")
            elif isinstance(inner, str):
                found = re.search(r"^Message-ID:\s*(<[^>]+>)", inner, re.I | re.M)
                ids += [found.group(1)] if found else []
    return list(dict.fromkeys(ids))


def is_warmup(msg: Message, markers: list[str]) -> bool:
    """Warm-up mail (mailboxes trading emails to build their reputation): a warm-up header ("X-Warmup", "X-Lemwarm"...)
    or one of the workspace's markers (OWN_WARMUP_MARKERS) in the subject or body. Never a reply, whoever sent it."""
    if any("warmup" in name.lower().replace("-", "") or name.lower().startswith(("x-lemwarm", "x-mailwarm")) for name in msg.keys()):
        return True
    if not markers:
        return False
    haystack = f"{msg.get('Subject') or ''}\n{_text_of(msg)}".lower()
    return any(m.lower() in haystack for m in markers if m.strip())


def classify(msg: Message, fallback_time: datetime | None = None, warmup_markers: list[str] | None = None) -> Inbound | None:
    """What an incoming email is. None when it has no Message-ID (it can't be stored once), or when it's warm-up mail."""
    if is_warmup(msg, warmup_markers or []):
        return None
    message_id = (MESSAGE_ID.findall(msg.get("Message-ID") or "") or [None])[0]
    if not message_id:
        return None
    from_email = parseaddr(msg.get("From") or "")[1].lower()
    subject = (msg.get("Subject") or "").strip()
    try:
        received = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
    except (TypeError, ValueError):
        received = None
    received = (received or fallback_time or datetime.now(UTC)).astimezone(UTC)
    text = own_words(_text_of(msg))
    snippet = re.sub(r"\s+", " ", text)[:280]
    refs = _references(msg)

    report = msg.get_content_type() == "multipart/report" and "delivery-status" in (msg.get_param("report-type") or "")
    if report or BOUNCE_FROM.match(from_email):
        return Inbound("bounce", message_id, from_email, subject, snippet, received, refs, _failed_recipient(msg))
    auto = (msg.get("Auto-Submitted") or "no").lower() not in ("", "no") or msg.get("X-Autoreply") or msg.get("X-Autorespond")
    if auto or (msg.get("Precedence") or "").lower() in ("auto_reply", "bulk", "junk") or AUTO_SUBJECT.match(subject):
        return Inbound("auto_reply", message_id, from_email, subject, snippet, received, refs)
    if UNSUBSCRIBE.search(text[:600]) or subject.lower().strip() in ("unsubscribe", "remove", "stop"):
        return Inbound("unsubscribe", message_id, from_email, subject, snippet, received, refs)
    return Inbound("reply", message_id, from_email, subject, snippet, received, refs)


def addresses(value: str | None) -> list[str]:
    return [a.lower() for _, a in getaddresses([value or ""]) if a]
