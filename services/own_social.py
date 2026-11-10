"""LinkedIn and Instagram campaigns (migration 0041): putting leads in, writing each message with the channel's prompt
({{aiMessage}}), and the scheduler that sends connection requests and messages through Unipile
(services/unipile.py) from the connected social accounts.

Safety, in order:
- Nothing goes out unless OWN_SOCIAL_LIVE is on. Until then each step is only logged once ("dry run").
- Only active campaigns, only accounts whose status is ok, only inside the campaign's window, one action per account
  per tick, never past the account's limits (connection requests a day and a week, messages a day) and never sooner
  than the workspace's gap after its last one. Sending paused in Settings holds these too.
- The do-not-contact list is checked again right before each step.
- Before each message, the chat is read: if they wrote anything, a reply is recorded, which stops every campaign of
  that lead (the same database trigger as email replies) and emails the team.
- A lead is never put in two LinkedIn (or two Instagram) campaigns, or one they were already messaged in.
"""

import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from crud import ExclusionRepo, exclusion_note, person_keys
from models import Enrollment, InboundEvent, Lead, Persona, Sequence, SequenceStep, SenderAccount, SocialAccount, StepRun
from services.own_mail import MissingValue, in_window, next_send_time, render
from services.own_sending import EnrollOutcome
from services.own_settings import DEFAULTS as SETTINGS_DEFAULTS, load_all as load_settings
from services.unipile import UnipileClient, UnipileError, profile_identifier

log = logging.getLogger("coldbrew.own_social")

CHANNELS = ("linkedin", "instagram")
# The prompt (Prompts page) that writes each kind of step's {{aiMessage}}.
PROMPT_KIND = {("linkedin", "invite"): "linkedin_invite", ("linkedin", "message"): "linkedin_message", ("instagram", "message"): "instagram_message"}
RETRY_AFTER = timedelta(days=1)
MAX_FAILURES = 4  # a step that fails this many times stops the lead's campaign
ACCEPT_DAYS = 14  # how long a LinkedIn message waits for the connection request to be accepted
WAITING = "Waiting for them to accept the connection request."

# A new campaign's steps on each channel.
DEFAULT_STEPS = {
    "linkedin": [
        {"action": "invite", "delay_days": 0, "body": "{{aiMessage}}"},
        {"action": "message", "delay_days": 2, "body": "{{aiMessage}}"},
        {"action": "message", "delay_days": 4, "body": "{{aiMessage}}"},
    ],
    "instagram": [
        {"action": "message", "delay_days": 0, "body": "{{aiMessage}}"},
        {"action": "message", "delay_days": 3, "body": "{{aiMessage}}"},
        {"action": "message", "delay_days": 4, "body": "{{aiMessage}}"},
    ],
}


def profile_url(lead: Lead, channel: str) -> str | None:
    return lead.linkedin_url if channel == "linkedin" else lead.instagram_url


# ---------- Putting leads in ----------


async def pick_account(session: AsyncSession, workspace_id: UUID, channel: str) -> SocialAccount | None:
    """The connected account on this network with the most room: fewest active leads for its messages a day."""
    accounts = list(await session.scalars(select(SocialAccount).where(SocialAccount.workspace_id == workspace_id, SocialAccount.provider == channel,
                                                                        SocialAccount.status == "ok")))  # fmt: skip
    if not accounts:
        return None
    s = (await load_settings(session)).get(workspace_id, SETTINGS_DEFAULTS)
    load = dict((await session.execute(
        select(Enrollment.social_account_id, func.count()).where(Enrollment.social_account_id.in_([a.id for a in accounts]),
                                                                Enrollment.status == "active").group_by(Enrollment.social_account_id)
    )).all())  # fmt: skip
    per_day = {a.id: s.social_limits(a.provider, a.daily_invite_limit, a.daily_message_limit)["messages_per_day"] for a in accounts}
    return min(accounts, key=lambda a: (load.get(a.id, 0) / max(1, per_day[a.id]), random.random()))


async def enroll_social(session: AsyncSession, workspace_id: UUID, sequence: Sequence, lead_ids: list[UUID], now: datetime | None = None) -> EnrollOutcome:
    """Put these leads into a LinkedIn or Instagram campaign. Skipped, with the reason counted: no profile on that
    network, already in it or another running campaign on that network, messaged there before, already replied,
    bounced or unsubscribed, or on the do-not-contact list."""
    now = now or datetime.now(UTC)
    channel = sequence.channel
    out = EnrollOutcome()
    leads = list(await session.scalars(select(Lead).where(Lead.workspace_id == workspace_id, Lead.id.in_(lead_ids))))
    if missing := len(set(lead_ids)) - len(leads):
        out.skipped["Not in this workspace"] = missing
    ids = [lead.id for lead in leads]
    same_channel = select(Sequence.id).where(Sequence.workspace_id == workspace_id, Sequence.channel == channel)
    here = set(await session.scalars(select(Enrollment.lead_id).where(Enrollment.sequence_id == sequence.id, Enrollment.lead_id.in_(ids)))) if ids else set()
    running = set(await session.scalars(select(Enrollment.lead_id).where(Enrollment.lead_id.in_(ids), Enrollment.sequence_id.in_(same_channel),
                                                                           Enrollment.status.in_(("active", "paused"))))) if ids else set()  # fmt: skip
    reached = set(await session.scalars(select(Enrollment.lead_id).join(StepRun, StepRun.enrollment_id == Enrollment.id).where(
        Enrollment.lead_id.in_(ids), Enrollment.sequence_id.in_(same_channel), StepRun.status == "sent"))) if ids else set()  # fmt: skip
    answered = set(await session.scalars(select(InboundEvent.lead_id).where(InboundEvent.lead_id.in_(ids), InboundEvent.type.in_(("reply", "bounce", "unsubscribe"))))) if ids else set()
    is_excluded = await ExclusionRepo(session).matcher()
    name = "LinkedIn" if channel == "linkedin" else "Instagram"
    for lead in leads:
        if not profile_identifier(channel, profile_url(lead, channel)):
            out.skip(f"No {name} profile")
        elif lead.id in here:
            out.skip("Already in this campaign")
        elif lead.id in running:
            out.skip(f"In another running {name} campaign")
        elif lead.id in reached:
            out.skip(f"Messaged on {name} before")
        elif lead.id in answered:
            out.skip("Already replied, bounced or unsubscribed")
        elif is_excluded(person_keys(lead.email, lead.domain)):
            out.skip("On the do-not-contact list")
        else:
            account = await pick_account(session, workspace_id, channel)
            if account is None:
                out.skip(f"No connected {name} account to send from")
                continue
            session.add(Enrollment(workspace_id=workspace_id, sequence_id=sequence.id, lead_id=lead.id, social_account_id=account.id,
                                   status="active", next_step_order=1, next_run_at=next_send_time(sequence.schedule, now)))  # fmt: skip
            await session.flush()
            out.added += 1
    await session.commit()
    return out


# ---------- Writing {{aiMessage}} ----------

# (session, lead, channel, action, step number, earlier messages by step) -> (the message, or why not)
SocialWriter = Callable[[AsyncSession, Lead, str, str, int, dict[int, str]], Awaitable[tuple[str | None, str | None]]]


def social_writer(writer: Any, lead_intel: Any) -> SocialWriter:
    """Writes a LinkedIn or Instagram step the way follow-ups are written: the workspace's prompt for that kind of
    step (Prompts page), filled with the lead, their ICP, product, hooks and what this campaign already sent them.
    Logged to Usage."""
    from dataclasses import replace

    from crud import ExampleRepo, IcpProfileRepo, ProductRepo, PromptRepo, ProviderCallRepo, SendingRepo
    from services.claude import cost_usd
    from services.generation import PromptVersionSpec, write_follow_up
    from services.outreach import Deps, _variables
    from services.prompts import render as render_template, render_product

    async def write(session: AsyncSession, lead: Lead, channel: str, action: str, step: int, earlier: dict[int, str]) -> tuple[str | None, str | None]:
        kind = PROMPT_KIND[(channel, action)]
        prompts = PromptRepo(session)
        version = await prompts.for_icp(lead.workspace_id, kind, None)
        label = {"linkedin_invite": "LinkedIn connection note", "linkedin_message": "LinkedIn message", "instagram_message": "Instagram message"}[kind]
        if version is None:
            return None, f"There's no {label} prompt. Add one on the Prompts page."
        deps = Deps(lead.workspace_id, SendingRepo(session), None, IcpProfileRepo(session), prompts, ProductRepo(session),
                    ExampleRepo(session), None, None, lead_intel, None, writer)  # fmt: skip
        variables = await _variables(deps, None, lead, step, earlier)
        spec = PromptVersionSpec(version.id, version.system_prompt, version.user_prompt, version.model, version.max_tokens)
        draft, calls, error = await write_follow_up(writer, replace(spec, system=render_template(spec.system, variables)), render_product(spec.user, variables))
        cost = sum(cost_usd(c.model, c.input_tokens, c.output_tokens) for c in calls)
        if calls:
            await ProviderCallRepo(session).record(
                lead.workspace_id, "claude", "messages", "failed" if error else "succeeded", cost_usd=round(cost, 4),
                action=f"Write {label}s", model=calls[-1].model, lead_id=lead.id,
                input_tokens=sum(c.input_tokens for c in calls), output_tokens=sum(c.output_tokens for c in calls),
            )  # fmt: skip
        if error or draft is None or not draft.body.strip():
            return None, f"Coldbrew couldn't write the {label}: {error or 'no text came back'}"
        return draft.body.strip(), None

    return write


async def written_text(session: AsyncSession, enrollment_id: UUID, step_id: UUID) -> str | None:
    """This lead's {{aiMessage}} for a step, once written (ahead, in a dry run, or sent): the same text every time."""
    return await session.scalar(select(StepRun.body).where(StepRun.enrollment_id == enrollment_id, StepRun.step_id == step_id,
                                                           StepRun.status.in_(("written", "dry_run", "sent", "failed")), StepRun.body != "")
                                .order_by(StepRun.ran_at.desc()).limit(1))  # fmt: skip


async def earlier_messages(session: AsyncSession, enrollment_id: UUID, before: int) -> dict[int, str]:
    out: dict[int, str] = {}
    for order, body in await session.execute(
        select(SequenceStep.step_order, StepRun.body).join(SequenceStep, SequenceStep.id == StepRun.step_id)
        .where(StepRun.enrollment_id == enrollment_id, StepRun.status.in_(("sent", "dry_run")), SequenceStep.step_order < before)
        .order_by(StepRun.ran_at)
    ):  # fmt: skip
        out.setdefault(order, body)
    return out


async def ai_message(session: AsyncSession, enrollment: Enrollment, lead: Lead, channel: str, step: SequenceStep,
                     write: SocialWriter | None, keep: bool = False) -> tuple[str | None, str | None]:  # fmt: skip
    """The step's {{aiMessage}}: the one already written, else Coldbrew writes it now. keep: store it ahead (a
    "written" run), as a preview does, so the same text goes out later."""
    if (done := await written_text(session, enrollment.id, step.id)) is not None:
        return done, None
    if write is None:
        return None, "ANTHROPIC_API_KEY isn't set, so Coldbrew can't write LinkedIn and Instagram messages."
    text, why = await write(session, lead, channel, step.action, step.step_order, await earlier_messages(session, enrollment.id, step.step_order))
    if text and keep:
        session.add(StepRun(workspace_id=enrollment.workspace_id, enrollment_id=enrollment.id, step_id=step.id, social_account_id=enrollment.social_account_id,
                            action=step.action, status="written", body=text))  # fmt: skip
        await session.flush()
    return text, why


async def step_values(session: AsyncSession, lead: Lead, account: SocialAccount | None) -> dict[str, str]:
    """What a social step's text can use. The sender is the mailbox the account belongs to (its persona)."""
    mailbox = await session.get(SenderAccount, account.sender_account_id) if account and account.sender_account_id else None
    persona = await session.get(Persona, mailbox.persona_id) if mailbox and mailbox.persona_id else None
    sender = (mailbox.from_name if mailbox else "") or (persona.name if persona else "") or (account.name if account else "")
    return {"firstName": lead.first_name, "lastName": lead.last_name, "company": lead.company, "title": lead.title, "senderName": sender}


# ---------- Sending ----------


@dataclass
class SocialTick:
    sent: int = 0
    dry_run: int = 0
    failed: int = 0
    skipped: int = 0
    replies: int = 0
    notes: list[str] = field(default_factory=list)


async def _done_since(session: AsyncSession, account_id: UUID, action: str, since: datetime) -> int:
    return int(await session.scalar(select(func.count()).select_from(StepRun).where(
        StepRun.social_account_id == account_id, StepRun.action == action, StepRun.status == "sent", StepRun.ran_at >= since)) or 0)  # fmt: skip


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def _note_once(session: AsyncSession, enrollment: Enrollment, step: SequenceStep, account: SocialAccount, why: str) -> None:
    said = await session.scalar(select(StepRun.id).where(StepRun.enrollment_id == enrollment.id, StepRun.step_id == step.id,
                                                         StepRun.status == "skipped", StepRun.error == why))  # fmt: skip
    if said is None:
        session.add(StepRun(workspace_id=enrollment.workspace_id, enrollment_id=enrollment.id, step_id=step.id, social_account_id=account.id,
                            action=step.action, status="skipped", error=why))  # fmt: skip


async def record_reply(session: AsyncSession, enrollment: Enrollment, lead: Lead, channel: str, now: datetime) -> None:
    """They wrote back on LinkedIn or Instagram: kept like an email reply, which stops every campaign of the lead (the
    database trigger), and the team is emailed."""
    from sqlalchemy.dialects.postgresql import insert

    from services import notify

    name = "LinkedIn" if channel == "linkedin" else "Instagram"
    await session.execute(insert(InboundEvent).values(
        workspace_id=enrollment.workspace_id, enrollment_id=enrollment.id, lead_id=lead.id, type="reply",
        message_id=f"unipile:{enrollment.chat_id}", from_email=lead.email or "", subject=f"Replied on {name}",
        snippet=f"Replied on {name}. Read it there.", folder=channel, received_at=now,
    ).on_conflict_do_nothing())  # fmt: skip
    await session.commit()
    sequence = await session.get(Sequence, enrollment.sequence_id)
    await notify.reply_received(lead, f"{sequence.name} ({name})" if sequence else name)  # never raises; needs the lead's email


async def send_social_due(
    session: AsyncSession,
    live: bool,
    unipile: UnipileClient | None,
    now: datetime | None = None,
    rng: random.Random | None = None,
    write: SocialWriter | None = None,
) -> SocialTick:
    """One tick: for each connected LinkedIn and Instagram account with room under its limits, the next due step of an
    active campaign, inside its window. Live, it's sent through Unipile; otherwise it's logged once as a dry run."""
    now = now or datetime.now(UTC)
    rng = rng or random.Random()
    out = SocialTick()
    if live and unipile is None:
        out.notes.append("OWN_SOCIAL_LIVE is on but Unipile isn't connected (UNIPILE_DSN, UNIPILE_API_KEY): nothing sent")
        return out
    is_excluded = await ExclusionRepo(session).matcher()
    settings = await load_settings(session)
    for account in list(await session.scalars(select(SocialAccount).where(SocialAccount.status == "ok"))):
        s = settings.get(account.workspace_id, SETTINGS_DEFAULTS)
        if s.sending_paused:
            continue
        limits = s.social_limits(account.provider, account.daily_invite_limit, account.daily_message_limit)
        if live:
            last = await session.scalar(select(func.max(StepRun.ran_at)).where(StepRun.social_account_id == account.id, StepRun.status == "sent"))
            if last is not None and now - last < timedelta(minutes=rng.uniform(s.social_min_gap_minutes, s.social_max_gap_minutes)):
                continue
        row = (await session.execute(
            select(Enrollment, Sequence).join(Sequence, Sequence.id == Enrollment.sequence_id)
            .where(Enrollment.social_account_id == account.id, Enrollment.status == "active", Sequence.status == "active",
                   Sequence.channel == account.provider, Enrollment.next_run_at.is_not(None), Enrollment.next_run_at <= now)
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
        identifier = profile_identifier(account.provider, profile_url(lead, account.provider)) if lead else None
        if step is None or lead is None:
            enrollment.status, enrollment.next_run_at = "completed", None
            await session.commit()
            continue
        if identifier is None:
            enrollment.status, enrollment.stop_reason, enrollment.next_run_at = "stopped", "no_profile", None
            await session.commit()
            continue
        if hit := is_excluded(person_keys(lead.email, lead.domain)):
            enrollment.status, enrollment.stop_reason, enrollment.next_run_at = "stopped", "do_not_contact", None
            session.add(StepRun(workspace_id=enrollment.workspace_id, enrollment_id=enrollment.id, step_id=step.id, social_account_id=account.id,
                                action=step.action, status="skipped", error=exclusion_note(hit)))  # fmt: skip
            out.skipped += 1
            await session.commit()
            continue
        # Room under this account's limits for this kind of step; otherwise the next window.
        if live:
            if step.action == "invite":
                full = (await _done_since(session, account.id, "invite", _day_start(now)) >= limits["invites_per_day"]
                        or await _done_since(session, account.id, "invite", now - timedelta(days=7)) >= limits["invites_per_week"])  # fmt: skip
            else:
                full = await _done_since(session, account.id, "message", _day_start(now)) >= limits["messages_per_day"]
            if full:
                enrollment.next_run_at = next_send_time(sequence.schedule, _day_start(now) + timedelta(days=1), rng)
                await session.commit()
                continue
        # Did they write back? Then nothing more is sent to them, on any channel.
        if live and step.action == "message" and enrollment.chat_id:
            try:
                if await unipile.they_replied(enrollment.chat_id, enrollment.provider_id or ""):
                    await record_reply(session, enrollment, lead, account.provider, now)
                    out.replies += 1
                    continue
            except UnipileError as exc:
                log.warning("couldn't read chat %s: %s", enrollment.chat_id, exc.message)
        values = await step_values(session, lead, account)
        if "aiMessage" in step.body:
            text, why = await ai_message(session, enrollment, lead, account.provider, step, write)
            if text is None:
                await _note_once(session, enrollment, step, account, why)
                enrollment.next_run_at = next_send_time(sequence.schedule, now + timedelta(hours=1), rng)
                out.skipped += 1
                await session.commit()
                continue
            values["aiMessage"] = text
        try:
            body = render(step.body, values).strip()
        except MissingValue as missing:
            enrollment.status, enrollment.next_run_at = "paused", None
            session.add(StepRun(workspace_id=enrollment.workspace_id, enrollment_id=enrollment.id, step_id=step.id, social_account_id=account.id, action=step.action,
                                status="skipped", error=f"Step {step.step_order} uses {{{{{missing.name}}}}}, which is empty for this lead. Add a fallback like {{{{{missing.name}|...}}}}."))  # fmt: skip
            out.skipped += 1
            await session.commit()
            continue

        if not live:
            logged = await session.scalar(select(StepRun.id).where(StepRun.enrollment_id == enrollment.id, StepRun.step_id == step.id, StepRun.status == "dry_run"))
            if logged is None:
                session.add(StepRun(workspace_id=enrollment.workspace_id, enrollment_id=enrollment.id, step_id=step.id, social_account_id=account.id,
                                    action=step.action, status="dry_run", body=body))  # fmt: skip
                out.dry_run += 1
            enrollment.next_run_at = next_send_time(sequence.schedule, now + timedelta(hours=1), rng)
            await session.commit()
            continue

        try:
            if enrollment.provider_id is None:
                enrollment.provider_id = await unipile.provider_id(account.unipile_account_id, identifier)
            message_id = None
            if step.action == "invite":
                await unipile.invite(account.unipile_account_id, enrollment.provider_id, body)
            elif enrollment.chat_id:
                message_id = await unipile.send_in_chat(enrollment.chat_id, body)
            else:
                enrollment.chat_id, message_id = await unipile.start_chat(account.unipile_account_id, enrollment.provider_id, body)
        except UnipileError as exc:
            if exc.status == 404 and enrollment.provider_id is None:
                enrollment.status, enrollment.stop_reason, enrollment.next_run_at = "stopped", "no_profile", None
                session.add(StepRun(workspace_id=enrollment.workspace_id, enrollment_id=enrollment.id, step_id=step.id, social_account_id=account.id,
                                    action=step.action, status="failed", body=body, error=f"Their profile wasn't found: {exc.message}"))  # fmt: skip
                out.failed += 1
                await session.commit()
                continue
            invited = await session.scalar(select(func.min(StepRun.ran_at)).join(SequenceStep, SequenceStep.id == StepRun.step_id).where(
                StepRun.enrollment_id == enrollment.id, SequenceStep.action == "invite", StepRun.status == "sent"))  # fmt: skip
            if account.provider == "linkedin" and step.action == "message" and not enrollment.chat_id and invited is not None:
                # Not connected yet: LinkedIn only lets us message connections. Waits for them to accept, then gives up.
                if now - invited < timedelta(days=ACCEPT_DAYS):
                    await _note_once(session, enrollment, step, account, WAITING)
                    enrollment.next_run_at = next_send_time(sequence.schedule, now + RETRY_AFTER, rng)
                else:
                    enrollment.status, enrollment.stop_reason, enrollment.next_run_at = "stopped", "not_connected", None
                out.skipped += 1
                await session.commit()
                continue
            session.add(StepRun(workspace_id=enrollment.workspace_id, enrollment_id=enrollment.id, step_id=step.id, social_account_id=account.id,
                                action=step.action, status="failed", body=body, error=exc.message))  # fmt: skip
            failures = int(await session.scalar(select(func.count()).select_from(StepRun).where(
                StepRun.enrollment_id == enrollment.id, StepRun.step_id == step.id, StepRun.status == "failed")) or 0) + 1  # fmt: skip
            if failures >= MAX_FAILURES:
                enrollment.status, enrollment.stop_reason, enrollment.next_run_at = "stopped", "failed", None
            else:
                enrollment.next_run_at = next_send_time(sequence.schedule, now + RETRY_AFTER, rng)
            out.failed += 1
            await session.commit()
            continue

        session.add(StepRun(workspace_id=enrollment.workspace_id, enrollment_id=enrollment.id, step_id=step.id, social_account_id=account.id,
                            action=step.action, status="sent", message_id=message_id, body=body))  # fmt: skip
        following = await session.scalar(select(SequenceStep).where(SequenceStep.sequence_id == sequence.id, SequenceStep.step_order > step.step_order)
                                         .order_by(SequenceStep.step_order).limit(1))  # fmt: skip
        if following is None:
            enrollment.status, enrollment.next_run_at = "completed", None
        else:
            enrollment.next_step_order = following.step_order
            enrollment.next_run_at = next_send_time(sequence.schedule, now + timedelta(days=following.delay_days), rng)
        out.sent += 1
        await session.commit()
    return out
