"""Coldbrew's own sending, against the database: putting leads into a campaign, the scheduler that sends their steps,
and the inbox reader that finds replies, bounces and unsubscribes. The text, the email and the timing are in
services/own_mail.py; the tables are migration 0036.

Safety, in order:
- Nothing is sent unless OWN_SENDING_LIVE is on. Until then the scheduler only logs, once per lead and step, what it
  would send (a "dry run" step run), and nobody moves on.
- Only campaigns marked active send, only from mailboxes whose connection test passed, only inside the campaign's
  window, at most one email per mailbox per tick and never past its daily limit.
- An address is claimed in eo_queued_addresses when it's added, the same record CheapSequencer's queue checks, so
  nobody is ever emailed by both, or twice.
- The do-not-contact list is checked again right before each send.
- A reply, bounce or unsubscribe stops every campaign of that lead in the same transaction that records it (the
  database trigger), and replies email the team (services/notify.py).
"""

import asyncio
import email
import email.policy
import imaplib
import logging
import random
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from crud import ExclusionRepo, exclusion_note, person_keys
from models import (
    Email, Enrollment, InboundEvent, Lead, Persona, QueuedAddress, SenderAccount, SenderUsage, Sequence, SequenceStep, StepRun,
)  # fmt: skip
from services import mail_check, notify
from services.own_settings import DEFAULTS as SETTINGS_DEFAULTS, load_all as load_settings
from services.own_mail import MissingValue, Outgoing, build_message, classify, in_window, next_send_time, render, reply_subject
from services.vault import Vault

log = logging.getLogger("coldbrew.own_sending")
TIMEOUT_SECONDS = 30
RETRY_AFTER = timedelta(hours=1)
MAX_FAILURES = 3  # a step that fails this many times stops the lead's campaign
READ_DAYS = 3  # how far back each inbox read looks (each email is stored once, by its Message-ID)
READ_LIMIT = 300  # emails read per folder per tick


# ---------- One campaign per ICP ----------

# A new ICP campaign: the email Coldbrew wrote and approved, then two follow-ups Coldbrew writes for each lead.
ICP_CAMPAIGN_STEPS = [
    {"step_order": 1, "delay_days": 0, "thread_mode": "new", "subject": "{{email1Subject}}", "body": "{{email1Body}}"},
    {"step_order": 2, "delay_days": 2, "thread_mode": "reply", "subject": "", "body": "{{followUp}}"},
    {"step_order": 3, "delay_days": 3, "thread_mode": "reply", "subject": "", "body": "{{followUp}}"},
]


async def ensure_icp_campaigns(session: AsyncSession, workspace_id: UUID, icps: list[dict[str, Any]]) -> int:
    """Every enabled ICP has a campaign in this workspace, like CheapSequencer's "Coldbrew: <ICP name>" ones: any
    missing is made, as a draft (nothing starts on its own). Matched by the ICP's id, so renaming either keeps them
    together. Returns how many were made. Serialised per workspace, so two pages loading at once never make two."""
    from sqlalchemy import text

    from services.own_mail import DEFAULT_SCHEDULE

    await session.execute(text("select pg_advisory_xact_lock(hashtext(:key))"), {"key": f"coldbrew:icp-campaigns:{workspace_id}"})
    have = set(await session.scalars(select(Sequence.lead_intel_icp_id).where(Sequence.workspace_id == workspace_id,
                                                                               Sequence.lead_intel_icp_id.is_not(None))))  # fmt: skip
    made = 0
    for icp in icps:
        if not icp.get("enabled", True) or icp["id"] in have:
            continue
        row = Sequence(workspace_id=workspace_id, name=(icp.get("name") or "ICP").strip(), status="draft",
                       lead_intel_icp_id=icp["id"], schedule=dict(DEFAULT_SCHEDULE))  # fmt: skip
        session.add(row)
        await session.flush()
        session.add_all([SequenceStep(sequence_id=row.id, **step) for step in ICP_CAMPAIGN_STEPS])
        made += 1
    await session.commit()
    return made


# ---------- Putting leads in ----------


@dataclass
class EnrollOutcome:
    added: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def needs_first_email(steps: list[SequenceStep]) -> bool:
    return any("email1" in (s.subject + s.body) for s in steps)


async def pick_sender(session: AsyncSession, workspace_id: UUID) -> SenderAccount | None:
    """The connected sending mailbox (never a warming one) with the most room: fewest active leads for its daily limit."""
    senders = list(await session.scalars(select(SenderAccount).where(SenderAccount.workspace_id == workspace_id, SenderAccount.status == "ok",
                                                                       SenderAccount.pool == "sending")))  # fmt: skip
    if not senders:
        return None
    load = dict((await session.execute(
        select(Enrollment.sender_account_id, func.count()).where(Enrollment.sender_account_id.in_([s.id for s in senders]),
                                                                Enrollment.status == "active").group_by(Enrollment.sender_account_id)
    )).all())  # fmt: skip
    return min(senders, key=lambda s: (load.get(s.id, 0) / s.daily_limit, random.random()))


async def enroll(session: AsyncSession, workspace_id: UUID, sequence: Sequence, lead_ids: list[UUID], now: datetime | None = None) -> EnrollOutcome:
    """Put these leads into the campaign. Skipped, with the reason counted: no email address, already in it or in
    another running campaign, ever queued before (here or through CheapSequencer), on the do-not-contact list, already
    replied, bounced or unsubscribed, or (when a step uses it) no Coldbrew email written for them yet."""
    now = now or datetime.now(UTC)
    out = EnrollOutcome()
    steps = list(await session.scalars(select(SequenceStep).where(SequenceStep.sequence_id == sequence.id)))
    wants_email = needs_first_email(steps)
    leads = list(await session.scalars(select(Lead).where(Lead.workspace_id == workspace_id, Lead.id.in_(lead_ids))))
    out.skipped["Not in this workspace"] = len(set(lead_ids)) - len(leads)
    if not out.skipped["Not in this workspace"]:
        del out.skipped["Not in this workspace"]
    ids = [lead.id for lead in leads]
    running = set(await session.scalars(select(Enrollment.lead_id).where(Enrollment.lead_id.in_(ids), Enrollment.status.in_(("active", "paused"))))) if ids else set()
    here = set(await session.scalars(select(Enrollment.lead_id).where(Enrollment.sequence_id == sequence.id, Enrollment.lead_id.in_(ids)))) if ids else set()
    answered = set(await session.scalars(select(InboundEvent.lead_id).where(InboundEvent.lead_id.in_(ids), InboundEvent.type.in_(("reply", "bounce", "unsubscribe"))))) if ids else set()
    firsts: dict[UUID, Email] = {}
    if ids:
        for e in await session.scalars(select(Email).where(Email.lead_id.in_(ids), Email.step == 1, Email.status == "approved").order_by(Email.created_at)):
            firsts[e.lead_id] = e
    is_excluded = await ExclusionRepo(session).matcher()

    for lead in leads:
        address = (lead.email or "").strip().lower()
        if not address:
            out.skip("No email address")
        elif lead.id in here:
            out.skip("Already in this campaign")
        elif lead.id in running:
            out.skip("In another running campaign")
        elif lead.id in answered:
            out.skip("Already replied, bounced or unsubscribed")
        elif is_excluded(person_keys(lead.email, lead.domain)):
            out.skip("On the do-not-contact list")
        elif wants_email and lead.id not in firsts:
            out.skip("No approved Coldbrew email for them yet")
        else:
            sender = await pick_sender(session, workspace_id)
            if sender is None:
                out.skip("No connected mailbox to send from")
                continue
            first = firsts.get(lead.id)
            claimed = await session.scalar(
                insert(QueuedAddress).values(email=address, workspace_id=workspace_id, email_id=first.id if first else None,
                                             campaign_id=f"own:{sequence.id}")
                .on_conflict_do_nothing(index_elements=[QueuedAddress.email]).returning(QueuedAddress.email)
            )  # fmt: skip
            if claimed is None:
                out.skip("Queued before (nobody is emailed twice)")
                continue
            session.add(Enrollment(
                workspace_id=workspace_id, sequence_id=sequence.id, lead_id=lead.id, sender_account_id=sender.id,
                email_id=first.id if first else None, status="active", next_step_order=1,
                next_run_at=next_send_time(sequence.schedule, now),
            ))  # fmt: skip
            if first is not None:
                first.status, first.queued_at = "queued", now  # Coldbrew won't write or queue them again
            await session.flush()
            out.added += 1
    await session.commit()
    return out


# ---------- Follow-ups Coldbrew writes ({{followUp}}) ----------

# (session, lead, their first email, step 2 to 5, earlier follow-ups' text by step) -> (the follow-up, or why not)
FollowUpWriter = Callable[[AsyncSession, Lead, Email, int, dict[int, str]], Awaitable[tuple[str | None, str | None]]]


async def stored_follow_up(session: AsyncSession, lead_id: UUID, step: int) -> Email | None:
    """A follow-up already written for this lead and step: written once, then reused (a dry run's text is what goes out
    live)."""
    return await session.scalar(select(Email).where(Email.lead_id == lead_id, Email.step == step).order_by(Email.created_at.desc()).limit(1))


def coldbrew_writer(writer: Any, lead_intel: Any) -> FollowUpWriter:
    """Writes a lead's follow-up the way Coldbrew writes CheapSequencer follow-ups: the workspace's follow-up prompt for
    that step (Prompts page), filled with the lead, their ICP, product, hooks, the first email and earlier follow-ups.
    Logged to Usage, and kept on the lead (eo_emails, step 2 to 5) like every follow-up."""
    from dataclasses import replace

    from crud import ExampleRepo, IcpProfileRepo, ProductRepo, PromptRepo, ProviderCallRepo, SendingRepo
    from services.claude import cost_usd
    from services.generation import PromptVersionSpec, write_follow_up
    from services.outreach import Deps, _variables
    from services.prompts import render as render_template, render_product

    async def write(session: AsyncSession, lead: Lead, first: Email, step: int, earlier: dict[int, str]) -> tuple[str | None, str | None]:
        prompts = PromptRepo(session)
        version = await prompts.for_icp(lead.workspace_id, f"followup{step}", None)
        if version is None:
            return None, f"There's no follow-up {step} prompt. Add one on the Prompts page."
        deps = Deps(lead.workspace_id, SendingRepo(session), None, IcpProfileRepo(session), prompts, ProductRepo(session),
                    ExampleRepo(session), None, None, lead_intel, None, writer)  # fmt: skip
        variables = await _variables(deps, first, lead, step, earlier)
        spec = PromptVersionSpec(version.id, version.system_prompt, version.user_prompt, version.model, version.max_tokens)
        draft, calls, error = await write_follow_up(writer, replace(spec, system=render_template(spec.system, variables)), render_product(spec.user, variables))
        cost = sum(cost_usd(c.model, c.input_tokens, c.output_tokens) for c in calls)
        if calls:
            await ProviderCallRepo(session).record(
                lead.workspace_id, "claude", "messages", "failed" if error else "succeeded", cost_usd=round(cost, 4),
                action="Write follow-ups (own sending)", model=calls[-1].model, lead_id=lead.id,
                input_tokens=sum(c.input_tokens for c in calls), output_tokens=sum(c.output_tokens for c in calls),
            )  # fmt: skip
        if error or draft is None:
            return None, f"Coldbrew couldn't write follow-up {step}: {error or 'no text came back'}"
        await SendingRepo(session).add_follow_up(first, step, draft.body, version.id, draft.confidence, cost)
        return draft.body, None

    return write


# ---------- Sending ----------


@dataclass
class TickSummary:
    sent: int = 0
    dry_run: int = 0
    failed: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)


def smtp_send(account: SenderAccount, password: str, message) -> None:
    """Deliver one email through the mailbox's own SMTP server."""
    context = ssl.create_default_context()
    if account.smtp_security == "ssl":
        with smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=TIMEOUT_SECONDS, context=context) as smtp:
            smtp.login(account.smtp_username, password)
            smtp.send_message(message)
        return
    with smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=TIMEOUT_SECONDS) as smtp:
        smtp.starttls(context=context)
        smtp.login(account.smtp_username, password)
        smtp.send_message(message)


async def _values(session: AsyncSession, lead: Lead, enrollment: Enrollment, account: SenderAccount, persona: Persona | None) -> dict[str, str]:
    first = await session.get(Email, enrollment.email_id) if enrollment.email_id else None
    return {
        "firstName": lead.first_name, "lastName": lead.last_name, "company": lead.company, "title": lead.title,
        "email1Subject": first.subject if first else "", "email1Body": (first.edited_body or first.body) if first else "",
        "senderName": account.from_name or (persona.name if persona else ""), "signature": persona.signature if persona else "",
    }  # fmt: skip


async def _follow_up(session: AsyncSession, enrollment: Enrollment, lead: Lead, step: SequenceStep,
                     write: FollowUpWriter | None) -> tuple[str | None, str | None]:  # fmt: skip
    """This lead's follow-up for a {{followUp}} step: the one already written, else Coldbrew writes it now."""
    if enrollment.email_id is None or (first := await session.get(Email, enrollment.email_id)) is None:
        return None, "{{followUp}} needs the lead's Coldbrew first email, and this lead has none."
    if (done := await stored_follow_up(session, lead.id, step.step_order)) is not None:
        return done.edited_body or done.body, None
    if write is None:
        return None, "ANTHROPIC_API_KEY isn't set, so Coldbrew can't write follow-ups."
    earlier: dict[int, str] = {}
    for order, body in await session.execute(
        select(SequenceStep.step_order, StepRun.body).join(SequenceStep, SequenceStep.id == StepRun.step_id)
        .where(StepRun.enrollment_id == enrollment.id, StepRun.status.in_(("sent", "dry_run")), SequenceStep.step_order < step.step_order)
    ):  # fmt: skip
        earlier.setdefault(order, body)
    return await write(session, lead, first, step.step_order, earlier)


async def _usage_today(session: AsyncSession, account_id: UUID, today) -> int:
    return int(await session.scalar(select(SenderUsage.sent).where(SenderUsage.sender_account_id == account_id, SenderUsage.day == today)) or 0)


async def send_due(
    session: AsyncSession,
    live: bool,
    now: datetime | None = None,
    deliver: Callable[[SenderAccount, str, Any], None] = smtp_send,
    rng: random.Random | None = None,
    write_follow_up: FollowUpWriter | None = None,
) -> TickSummary:
    """One scheduler tick: for each connected mailbox under its daily limit, the next due step of an active campaign,
    inside its window. Live, it's sent; otherwise it's logged once as a dry run."""
    now = now or datetime.now(UTC)
    rng = rng or random.Random()
    out = TickSummary()
    today = now.date()
    vault = Vault(session)
    is_excluded = await ExclusionRepo(session).matcher()
    accounts = list(await session.scalars(select(SenderAccount).where(SenderAccount.status == "ok", SenderAccount.pool == "sending")))
    settings = await load_settings(session)
    sent_by_ws: dict[UUID, int] = {}
    for account in accounts:
        s = settings.get(account.workspace_id, SETTINGS_DEFAULTS)
        if s.sending_paused:
            continue
        if live and await _usage_today(session, account.id, today) >= account.daily_limit:
            continue
        if live and s.workspace_daily_cap is not None:
            if account.workspace_id not in sent_by_ws:
                sent_by_ws[account.workspace_id] = int(await session.scalar(
                    select(func.coalesce(func.sum(SenderUsage.sent), 0)).join(SenderAccount, SenderAccount.id == SenderUsage.sender_account_id)
                    .where(SenderAccount.workspace_id == account.workspace_id, SenderUsage.day == today)) or 0)  # fmt: skip
            if sent_by_ws[account.workspace_id] >= s.workspace_daily_cap:
                continue
        if live:
            # A random gap between one mailbox's sends (the workspace's min to max minutes), so it never sends in bursts.
            last = await session.scalar(select(func.max(StepRun.ran_at)).where(StepRun.sender_account_id == account.id, StepRun.status == "sent"))
            if last is not None and now - last < timedelta(minutes=rng.uniform(s.min_gap_minutes, s.max_gap_minutes)):
                continue
        row = (await session.execute(
            select(Enrollment, Sequence).join(Sequence, Sequence.id == Enrollment.sequence_id)
            .where(Enrollment.sender_account_id == account.id, Enrollment.status == "active", Sequence.status == "active",
                   Enrollment.next_run_at.is_not(None), Enrollment.next_run_at <= now)
            .order_by(Enrollment.next_run_at).limit(1).with_for_update(of=Enrollment, skip_locked=True)
        )).first()  # fmt: skip
        if row is None:
            continue
        enrollment, sequence = row
        if not in_window(sequence.schedule, now):
            enrollment.next_run_at = next_send_time(sequence.schedule, now, rng)
            await session.commit()
            continue
        step = await session.scalar(select(SequenceStep).where(SequenceStep.sequence_id == sequence.id, SequenceStep.step_order == enrollment.next_step_order))
        lead = await session.get(Lead, enrollment.lead_id)
        if step is None or lead is None or not lead.email:
            enrollment.status, enrollment.stop_reason, enrollment.next_run_at = "completed", None, None
            await session.commit()
            continue
        if hit := is_excluded(person_keys(lead.email, lead.domain)):
            enrollment.status, enrollment.stop_reason, enrollment.next_run_at = "stopped", "do_not_contact", None
            session.add(StepRun(workspace_id=enrollment.workspace_id, enrollment_id=enrollment.id, step_id=step.id,
                                sender_account_id=account.id, status="skipped", error=exclusion_note(hit)))  # fmt: skip
            out.skipped += 1
            await session.commit()
            continue
        persona = await session.get(Persona, account.persona_id) if account.persona_id else None
        values = await _values(session, lead, enrollment, account, persona)
        if "followUp" in step.body:
            text, why = await _follow_up(session, enrollment, lead, step, write_follow_up)
            if text is None:
                # Tried again in the next window; noted once per reason, so the activity doesn't fill up.
                said = await session.scalar(select(StepRun.id).where(StepRun.enrollment_id == enrollment.id, StepRun.step_id == step.id,
                                                                     StepRun.status == "skipped", StepRun.error == why))  # fmt: skip
                if said is None:
                    session.add(StepRun(workspace_id=enrollment.workspace_id, enrollment_id=enrollment.id, step_id=step.id,
                                        sender_account_id=account.id, status="skipped", error=why))  # fmt: skip
                enrollment.next_run_at = next_send_time(sequence.schedule, now + timedelta(hours=1), rng)
                out.skipped += 1
                await session.commit()
                continue
            values["followUp"] = text
        first_run = await session.scalar(
            select(StepRun).where(StepRun.enrollment_id == enrollment.id, StepRun.status.in_(("sent", "dry_run")))
            .order_by(StepRun.ran_at).limit(1)
        )  # fmt: skip
        try:
            body = render(step.body, values)
            threaded = step.thread_mode == "reply" and first_run is not None
            subject = reply_subject(first_run.subject) if threaded else render(step.subject, values)
        except MissingValue as missing:
            enrollment.status, enrollment.next_run_at = "paused", None
            session.add(StepRun(workspace_id=enrollment.workspace_id, enrollment_id=enrollment.id, step_id=step.id, sender_account_id=account.id,
                                status="skipped", error=f"Step {step.step_order} uses {{{{{missing.name}}}}}, which is empty for this lead. Add a fallback like {{{{{missing.name}|...}}}}."))  # fmt: skip
            out.skipped += 1
            await session.commit()
            continue
        outgoing = Outgoing(from_name=values["senderName"], from_email=account.from_email, to_email=lead.email, subject=subject, body=body,
                            signature=values["signature"], in_reply_to=enrollment.thread_message_id if threaded else None)  # fmt: skip

        if not live:
            logged = await session.scalar(select(StepRun.id).where(StepRun.enrollment_id == enrollment.id, StepRun.step_id == step.id, StepRun.status == "dry_run"))
            if logged is None:
                session.add(StepRun(workspace_id=enrollment.workspace_id, enrollment_id=enrollment.id, step_id=step.id, sender_account_id=account.id,
                                    status="dry_run", subject=subject, body=body))  # fmt: skip
                out.dry_run += 1
            # Not moved on: it's sent for real once live sending is on. Checked again in the next window.
            enrollment.next_run_at = next_send_time(sequence.schedule, now + timedelta(hours=1), rng)
            await session.commit()
            continue

        message, message_id = build_message(outgoing)
        try:
            password = await vault.read(account.smtp_secret_id)
            if not password:
                raise smtplib.SMTPAuthenticationError(535, b"No saved password")
            await asyncio.to_thread(deliver, account, password, message)
        except Exception as exc:  # noqa: BLE001 - recorded, retried later, never fatal for the tick
            error = mail_check._friendly(exc, "SMTP", mail_check.Endpoint(account.smtp_host, account.smtp_port, account.smtp_security, "", ""))
            session.add(StepRun(workspace_id=enrollment.workspace_id, enrollment_id=enrollment.id, step_id=step.id, sender_account_id=account.id,
                                status="failed", subject=subject, body=body, error=error))  # fmt: skip
            if isinstance(exc, smtplib.SMTPAuthenticationError):
                account.status, account.last_error, account.last_checked_at = "failed", error, now
            failures = int(await session.scalar(select(func.count()).select_from(StepRun).where(
                StepRun.enrollment_id == enrollment.id, StepRun.step_id == step.id, StepRun.status == "failed")) or 0) + 1  # fmt: skip
            if failures >= MAX_FAILURES:
                enrollment.status, enrollment.stop_reason, enrollment.next_run_at = "stopped", "failed", None
            else:
                enrollment.next_run_at = now + RETRY_AFTER
            out.failed += 1
            await session.commit()
            continue

        session.add(StepRun(workspace_id=enrollment.workspace_id, enrollment_id=enrollment.id, step_id=step.id, sender_account_id=account.id,
                            status="sent", message_id=message_id, subject=subject, body=body))  # fmt: skip
        await session.execute(
            insert(SenderUsage).values(sender_account_id=account.id, day=today, sent=1)
            .on_conflict_do_update(index_elements=[SenderUsage.sender_account_id, SenderUsage.day], set_={"sent": SenderUsage.sent + 1})
        )  # fmt: skip
        enrollment.thread_message_id = enrollment.thread_message_id or message_id
        if step.step_order == 1 and enrollment.email_id:
            await session.execute(update(Email).where(Email.id == enrollment.email_id).values(status="sent", sent_at=now))
        following = await session.scalar(
            select(SequenceStep).where(SequenceStep.sequence_id == sequence.id, SequenceStep.step_order > step.step_order)
            .order_by(SequenceStep.step_order).limit(1)
        )  # fmt: skip
        if following is None:
            enrollment.status, enrollment.next_run_at = "completed", None
        else:
            enrollment.next_step_order = following.step_order
            enrollment.next_run_at = next_send_time(sequence.schedule, now + timedelta(days=following.delay_days), rng)
        out.sent += 1
        if account.workspace_id in sent_by_ws:
            sent_by_ws[account.workspace_id] += 1
        await session.commit()
    return out


# ---------- Reading replies ----------

SPAM_NAMES = ("[gmail]/spam", "junk", "junk email", "junk e-mail", "spam", "bulk mail", "inbox.spam", "inbox.junk")


def _folders(imap: imaplib.IMAP4) -> list[str]:
    """INBOX and the spam folder, whatever this provider calls it."""
    found = ["INBOX"]
    status, rows = imap.list()
    if status != "OK":
        return found
    for raw in rows or []:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        name = line.rsplit(' "', 1)[-1].rstrip('"') if '"' in line else line.split()[-1]
        if "\\Junk" in line or name.lower() in SPAM_NAMES:
            found.append(name)
    return list(dict.fromkeys(found))


def fetch_recent(account: SenderAccount, password: str, since: datetime) -> list[tuple[str, bytes]]:
    """Emails in INBOX and spam since a date, read without marking them as read (BODY.PEEK)."""
    if account.imap_security == "ssl":
        imap = imaplib.IMAP4_SSL(account.imap_host, account.imap_port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT_SECONDS)
    else:
        imap = imaplib.IMAP4(account.imap_host, account.imap_port, timeout=TIMEOUT_SECONDS)
        imap.starttls(ssl_context=ssl.create_default_context())
    out: list[tuple[str, bytes]] = []
    try:
        imap.login(account.imap_username, password)
        for folder in _folders(imap):
            status, _ = imap.select(f'"{folder}"', readonly=True)
            if status != "OK":
                continue
            status, data = imap.search(None, "SINCE", since.strftime("%d-%b-%Y"))
            if status != "OK" or not data or not data[0]:
                continue
            for num in data[0].split()[-READ_LIMIT:]:
                status, parts = imap.fetch(num, "(BODY.PEEK[])")
                if status == "OK":
                    for part in parts:
                        if isinstance(part, tuple) and len(part) > 1:
                            out.append((folder, part[1]))
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


@dataclass
class ReadSummary:
    read: int = 0
    recorded: dict[str, int] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)


async def _match(session: AsyncSession, account: SenderAccount, inbound) -> tuple[Enrollment | None, Lead | None]:
    """The campaign and lead an incoming email belongs to: by the email it answers (its Message-ID is ours), else by
    the address (the bounced one, or the sender's). Anything else (warm-up mail, newsletters) belongs to nobody."""
    if inbound.refers_to:
        run = await session.scalar(select(StepRun).where(StepRun.message_id.in_(inbound.refers_to), StepRun.status == "sent").limit(1))
        if run is not None:
            enrollment = await session.get(Enrollment, run.enrollment_id)
            return enrollment, (await session.get(Lead, enrollment.lead_id)) if enrollment else None
    address = inbound.failed_recipient if inbound.type == "bounce" else inbound.from_email
    if not address:
        return None, None
    row = (await session.execute(
        select(Enrollment, Lead).join(Lead, Lead.id == Enrollment.lead_id)
        .where(Enrollment.workspace_id == account.workspace_id, func.lower(Lead.email) == address.lower())
        .order_by(Enrollment.created_at.desc()).limit(1)
    )).first()  # fmt: skip
    return (row[0], row[1]) if row else (None, None)


async def read_inboxes(
    session: AsyncSession,
    now: datetime | None = None,
    fetch: Callable[[SenderAccount, str, datetime], list[tuple[str, bytes]]] = fetch_recent,
    alert: Callable[[Lead], Any] | None = None,
    warmup_markers: list[str] | None = None,
) -> ReadSummary:
    """One inbox tick: every mailbox with IMAP, inbox and spam. Each matched email is stored once (by mailbox and
    Message-ID); the database trigger stops the lead's campaigns on a reply, bounce or unsubscribe, and a new reply
    emails the team."""
    now = now or datetime.now(UTC)
    alert = alert or notify.reply_received
    out = ReadSummary()
    vault = Vault(session)
    accounts = list(await session.scalars(select(SenderAccount).where(SenderAccount.imap_host.is_not(None), SenderAccount.status != "paused")))
    for account in accounts:
        password = await vault.read(account.imap_secret_id)
        if not password:
            continue
        try:
            raw = await asyncio.to_thread(fetch, account, password, now - timedelta(days=READ_DAYS))
        except Exception as exc:  # noqa: BLE001 - one mailbox failing never stops the others
            out.failed.append(f"{account.from_email}: {exc.__class__.__name__}")
            continue
        for folder, data in raw:
            out.read += 1
            msg = email.message_from_bytes(data, policy=email.policy.default)
            inbound = classify(msg, now, warmup_markers)  # warm-up mail comes back as None
            if inbound is None or inbound.from_email == account.from_email.lower():
                continue
            enrollment, lead = await _match(session, account, inbound)
            if lead is None:
                continue
            new = await session.scalar(
                insert(InboundEvent).values(
                    workspace_id=account.workspace_id, sender_account_id=account.id, enrollment_id=enrollment.id if enrollment else None,
                    lead_id=lead.id, type=inbound.type, message_id=inbound.message_id,
                    in_reply_to=inbound.refers_to[0] if inbound.refers_to else None, from_email=inbound.from_email,
                    subject=inbound.subject[:500], snippet=inbound.snippet, folder=folder, received_at=inbound.received_at,
                ).on_conflict_do_nothing(index_elements=[InboundEvent.sender_account_id, InboundEvent.message_id]).returning(InboundEvent.id)
            )  # fmt: skip
            await session.commit()
            if new is None:
                continue
            out.recorded[inbound.type] = out.recorded.get(inbound.type, 0) + 1
            if inbound.type == "reply":
                await alert(lead)
    return out
