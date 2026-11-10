"""Campaigns for Coldbrew's own sending (the sending dashboard): their steps and window, the leads in each, and what
happened (sends, replies, bounces). services/own_sending.py does the sending and reading; this is the dashboard's API.

Anyone in the workspace can see them; creating, editing, starting and adding leads is for Management and admins.
Nothing here sends: the scheduler does, and only once OWN_SENDING_LIVE is on.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from database import get_session
from dependencies import get_lead_intel_repo, get_optional_writer, require_manager, require_workspace_id
from lead_intel import LeadIntelRepo
from models import Email, Enrollment, InboundEvent, Lead, Persona, QueuedAddress, SenderAccount, Sequence, SequenceStep, SocialAccount, StepRun
from schemas import ApiModel
from services.auth import CurrentUser
from services.generation import EmailWriter
from services.own_mail import FOLLOW_UP_STEPS, VARIABLES, MissingValue, clean_schedule, next_send_time, render, reply_subject, unknown_variables
from services.own_sending import coldbrew_writer, enroll, ensure_icp_campaigns, stored_follow_up

router = APIRouter(tags=["own sending"])

Status = Literal["draft", "active", "paused", "archived"]
Channel = Literal["email", "linkedin", "instagram"]
NETWORK = {"linkedin": "LinkedIn", "instagram": "Instagram"}


# ---------- Shapes ----------


class ScheduleIn(ApiModel):
    timezone: str = Field(default="America/New_York", max_length=64)
    start: str = Field(default="09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(default="17:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    days: list[int] = Field(default=[1, 2, 3, 4, 5, 6], min_length=1, max_length=7)


class StepIn(ApiModel):
    delay_days: int = Field(default=0, ge=0, le=90)
    # email (email campaigns), invite (a LinkedIn connection request, first step only) or message (LinkedIn, Instagram).
    action: Literal["email", "invite", "message"] = "email"
    thread_mode: Literal["new", "reply"] = "reply"
    subject: str = Field(default="", max_length=300)
    body: str = Field(min_length=1, max_length=20000)


class CampaignIn(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    # Set when it's made; editing keeps it.
    channel: Channel = "email"
    schedule: ScheduleIn = ScheduleIn()
    steps: list[StepIn] = Field(min_length=1, max_length=10)

    @field_validator("name")
    @classmethod
    def named(cls, value):
        if not value.strip():
            raise ValueError("Name the campaign")
        return value.strip()


class StatusIn(ApiModel):
    status: Status


class StepOut(ApiModel):
    id: UUID
    step_order: int
    delay_days: int
    action: str
    thread_mode: str
    subject: str
    body: str


class Stats(ApiModel):
    leads: int = 0
    active: int = 0
    completed: int = 0
    stopped: int = 0
    sent: int = 0
    dry_runs: int = 0
    failed: int = 0
    replies: int = 0
    bounces: int = 0
    unsubscribes: int = 0


class CampaignOut(ApiModel):
    id: UUID
    name: str
    # The ICP this is the campaign of (one per ICP, made for every enabled ICP), or None for one made by hand.
    icp_id: UUID | None = None
    icp_name: str | None = None
    channel: str
    status: str
    schedule: dict
    steps: list[StepOut]
    stats: Stats
    created_at: datetime
    updated_at: datetime


class EnrollIn(ApiModel):
    lead_ids: list[UUID] = Field(min_length=1, max_length=2000)


class EnrollOut(ApiModel):
    added: int
    skipped: dict[str, int]


class EnrollmentOut(ApiModel):
    id: UUID
    lead_id: UUID
    name: str
    email: str | None
    # LinkedIn and Instagram campaigns: their profile there.
    profile_url: str | None = None
    company: str
    # The mailbox (email) or the LinkedIn or Instagram account it sends from.
    sender: str | None
    status: str
    stop_reason: str | None
    next_step_order: int
    next_run_at: datetime | None
    sent: int
    last_activity_at: datetime | None
    created_at: datetime


class EnrollmentActionIn(ApiModel):
    action: Literal["pause", "resume", "stop"]


class CandidateOut(ApiModel):
    id: UUID
    name: str
    email: str
    company: str
    has_first_email: bool
    profile_url: str | None = None


class ActivityOut(ApiModel):
    id: UUID
    kind: str  # sent | dry_run | failed | skipped | reply | bounce | unsubscribe | auto_reply
    at: datetime
    campaign_id: UUID | None
    campaign: str | None
    lead_id: UUID | None
    lead_name: str
    lead_email: str
    mailbox: str | None
    subject: str
    detail: str


class SendingStatusOut(ApiModel):
    live: bool
    warmup_live: bool = False
    # LinkedIn and Instagram (OWN_SOCIAL_LIVE): off, their steps are only logged.
    social_live: bool = False
    variables: dict[str, str]
    social_variables: dict[str, str] = {}
    # A new LinkedIn or Instagram campaign's steps.
    social_steps: dict[str, list[dict]] = {}


# ---------- Helpers ----------


def _name(lead: Lead) -> str:
    return f"{lead.first_name} {lead.last_name}".strip() or (lead.email or "Unnamed")


async def _campaign(session: AsyncSession, workspace_id: UUID, campaign_id: UUID, lock: bool = False) -> Sequence:
    query = select(Sequence).where(Sequence.workspace_id == workspace_id, Sequence.id == campaign_id)
    row = await session.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise HTTPException(404, "Campaign not found")
    return row


def _check_steps(steps: list[StepIn], channel: str = "email") -> None:
    if channel != "email":
        return _check_social_steps(steps, channel)
    for i, step in enumerate(steps, 1):
        if step.action != "email":
            raise HTTPException(422, "An email campaign's steps are emails.")
        if i == 1 and not step.subject.strip():
            raise HTTPException(422, "The first email needs a subject.")
        if step.thread_mode == "new" and not step.subject.strip():
            raise HTTPException(422, f"Email {i} starts a new thread, so it needs a subject.")
        if "followUp" in step.subject or ("followUp" in step.body and i not in FOLLOW_UP_STEPS):
            raise HTTPException(422, f"{{{{followUp}}}} goes in the body of emails 2 to 5 only (Coldbrew has a follow-up prompt for each). Email {i} can't use it.")
        if bad := unknown_variables(step.subject + step.body):
            raise HTTPException(422, f"Email {i} uses {', '.join('{{' + b + '}}' for b in bad)}, which isn't a variable. Use one of: "
                                     + ", ".join("{{" + v + "}}" for v in VARIABLES) + ".")  # fmt: skip


LINKEDIN_NOTE_MAX = 300  # LinkedIn's longest connection note (200 on free accounts)


def _check_social_steps(steps: list[StepIn], channel: str) -> None:
    """LinkedIn: a connection request can only be the first step, then messages. Instagram: messages only. No
    subjects, and only the variables a direct message can use."""
    from services.own_mail import SOCIAL_VARIABLES

    name = NETWORK[channel]
    for i, step in enumerate(steps, 1):
        if step.action == "email":
            raise HTTPException(422, f"A {name} campaign's steps are {'connection requests and ' if channel == 'linkedin' else ''}messages, not emails.")
        if step.action == "invite" and (channel != "linkedin" or i != 1):
            raise HTTPException(422, "A connection request is LinkedIn only, and only as the first step." if channel == "linkedin" else "Instagram has no connection requests: its steps are messages.")
        if bad := unknown_variables(step.body, SOCIAL_VARIABLES):
            raise HTTPException(422, f"Step {i} uses {', '.join('{{' + b + '}}' for b in bad)}, which a {name} step can't use. Use one of: "
                                     + ", ".join("{{" + v + "}}" for v in SOCIAL_VARIABLES) + ".")  # fmt: skip
        if step.action == "invite" and "aiMessage" not in step.body and len(step.body.strip()) > LINKEDIN_NOTE_MAX:
            raise HTTPException(422, f"A connection note can be at most {LINKEDIN_NOTE_MAX} characters (LinkedIn cuts it off).")


def _step_fields(i: int, s: StepIn, channel: str) -> dict:
    if channel == "email":
        return {"delay_days": 0 if i == 1 else s.delay_days, "action": "email", "thread_mode": "new" if i == 1 else s.thread_mode,
                "subject": s.subject.strip(), "body": s.body}  # fmt: skip
    return {"delay_days": 0 if i == 1 else s.delay_days, "action": s.action, "thread_mode": "reply", "subject": "", "body": s.body}


def _schedule(body: ScheduleIn) -> dict:
    try:
        return clean_schedule(body.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, str(exc) if isinstance(exc, ValueError) else "That timezone isn't recognised.") from exc


async def _stats(session: AsyncSession, ids: list[UUID]) -> dict[UUID, Stats]:
    out = {i: Stats() for i in ids}
    if not ids:
        return out
    rows = await session.execute(
        select(Enrollment.sequence_id, func.count(),
               func.count().filter(Enrollment.status == "active"), func.count().filter(Enrollment.status == "completed"),
               func.count().filter(Enrollment.status == "stopped"))
        .where(Enrollment.sequence_id.in_(ids)).group_by(Enrollment.sequence_id)
    )  # fmt: skip
    for sid, leads, active, completed, stopped in rows:
        out[sid].leads, out[sid].active, out[sid].completed, out[sid].stopped = leads, active, completed, stopped
    runs = await session.execute(
        select(Enrollment.sequence_id, StepRun.status, func.count()).join(Enrollment, Enrollment.id == StepRun.enrollment_id)
        .where(Enrollment.sequence_id.in_(ids)).group_by(Enrollment.sequence_id, StepRun.status)
    )  # fmt: skip
    for sid, status, n in runs:
        if status == "sent":
            out[sid].sent = n
        elif status == "dry_run":
            out[sid].dry_runs = n
        elif status == "failed":
            out[sid].failed = n
    events = await session.execute(
        select(Enrollment.sequence_id, InboundEvent.type, func.count(func.distinct(InboundEvent.lead_id)))
        .join(Enrollment, Enrollment.id == InboundEvent.enrollment_id)
        .where(Enrollment.sequence_id.in_(ids)).group_by(Enrollment.sequence_id, InboundEvent.type)
    )  # fmt: skip
    for sid, kind, n in events:
        if kind == "reply":
            out[sid].replies = n
        elif kind == "bounce":
            out[sid].bounces = n
        elif kind == "unsubscribe":
            out[sid].unsubscribes = n
    return out


async def _social_names(session: AsyncSession, ids: set) -> dict[UUID, str]:
    """A LinkedIn or Instagram account as the dashboard shows it: its name, or @username."""
    if not ids:
        return {}
    rows = await session.execute(select(SocialAccount.id, SocialAccount.name, SocialAccount.username).where(SocialAccount.id.in_(ids)))
    return {i: name or (f"@{username}" if username else "Social account") for i, name, username in rows}


async def _icp_names(lead_intel: LeadIntelRepo) -> dict[UUID, str]:
    return {i["id"]: i["name"] for i in await lead_intel.icp_names()}


async def _out(session: AsyncSession, rows: list[Sequence], icp_names: dict[UUID, str] | None = None) -> list[CampaignOut]:
    icp_names = icp_names or {}
    ids = [r.id for r in rows]
    stats = await _stats(session, ids)
    steps: dict[UUID, list[SequenceStep]] = {i: [] for i in ids}
    for s in (await session.scalars(select(SequenceStep).where(SequenceStep.sequence_id.in_(ids)).order_by(SequenceStep.step_order))) if ids else []:
        steps[s.sequence_id].append(s)
    return [
        CampaignOut(id=r.id, name=r.name, icp_id=r.lead_intel_icp_id, icp_name=icp_names.get(r.lead_intel_icp_id), channel=r.channel, status=r.status, schedule=r.schedule, stats=stats[r.id], created_at=r.created_at, updated_at=r.updated_at,
                    steps=[StepOut(id=s.id, step_order=s.step_order, delay_days=s.delay_days, action=s.action, thread_mode=s.thread_mode, subject=s.subject, body=s.body)
                           for s in steps[r.id]])  # fmt: skip
        for r in rows
    ]


# ---------- Campaigns ----------


@router.get("/own-sending/status", response_model=SendingStatusOut)
async def sending_status():
    """Whether the scheduler really sends (OWN_SENDING_LIVE, OWN_SOCIAL_LIVE), and the variables a step can use."""
    from services.own_mail import SOCIAL_VARIABLES
    from services.own_social import DEFAULT_STEPS

    s = get_settings()
    return SendingStatusOut(live=s.own_sending_live, warmup_live=s.own_warmup_live, social_live=s.own_social_live, variables=VARIABLES,
                            social_variables=SOCIAL_VARIABLES, social_steps=DEFAULT_STEPS)  # fmt: skip


@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session),
                         lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo)):  # fmt: skip
    """The workspace's campaigns: one for every enabled ICP (any missing is made now, as a draft), then the ones made
    by hand. ICP campaigns come first, in the ICPs' order."""
    icps = await lead_intel.icp_names()
    await ensure_icp_campaigns(session, workspace_id, icps)
    order = {i["id"]: n for n, i in enumerate(icps)}
    rows = list(await session.scalars(select(Sequence).where(Sequence.workspace_id == workspace_id).order_by(Sequence.created_at.desc())))
    rows.sort(key=lambda r: (r.lead_intel_icp_id is None, order.get(r.lead_intel_icp_id, len(order))))
    return await _out(session, rows, {i["id"]: i["name"] for i in icps})


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
async def get_campaign(campaign_id: UUID, workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session),
                       lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo)):  # fmt: skip
    return (await _out(session, [await _campaign(session, workspace_id, campaign_id)], await _icp_names(lead_intel)))[0]


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
async def add_campaign(body: CampaignIn, workspace_id: UUID = Depends(require_workspace_id),
                       user: CurrentUser = Depends(require_manager), session: AsyncSession = Depends(get_session)):  # fmt: skip
    """A new campaign starts as a draft: nothing is sent from it until it's started."""
    _check_steps(body.steps, body.channel)
    row = Sequence(workspace_id=workspace_id, name=body.name, channel=body.channel, status="draft", schedule=_schedule(body.schedule), created_by=user.id)
    session.add(row)
    await session.flush()
    for i, s in enumerate(body.steps, 1):
        session.add(SequenceStep(sequence_id=row.id, step_order=i, **_step_fields(i, s, body.channel)))
    await session.commit()
    await session.refresh(row)
    return (await _out(session, [row]))[0]


@router.put("/campaigns/{campaign_id}", response_model=CampaignOut)
async def edit_campaign(campaign_id: UUID, body: CampaignIn, workspace_id: UUID = Depends(require_workspace_id),
                        _user: CurrentUser = Depends(require_manager), session: AsyncSession = Depends(get_session),
                        lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo)):  # fmt: skip
    """Name, window and steps. Steps already sent stay as they went; leads part-way through carry on with the new text
    from their next step. A step can't be removed once a lead has received it."""
    row = await _campaign(session, workspace_id, campaign_id, lock=True)
    if body.channel != row.channel:
        raise HTTPException(409, "A campaign's channel can't change. Make a new campaign for another channel.")
    _check_steps(body.steps, row.channel)
    current = list(await session.scalars(select(SequenceStep).where(SequenceStep.sequence_id == row.id).order_by(SequenceStep.step_order)))
    used = set(await session.scalars(select(StepRun.step_id).where(StepRun.step_id.in_([s.id for s in current]), StepRun.status == "sent"))) if current else set()
    if any(s.id in used for s in current[len(body.steps):]):
        raise HTTPException(409, "A removed step was already sent to some leads. Keep it, or start a new campaign.")
    row.name, row.schedule = body.name, _schedule(body.schedule)
    for i, s in enumerate(body.steps, 1):
        fields = _step_fields(i, s, row.channel)
        if i <= len(current):
            for k, v in fields.items():
                setattr(current[i - 1], k, v)
        else:
            session.add(SequenceStep(sequence_id=row.id, step_order=i, **fields))
    for s in current[len(body.steps):]:
        await session.delete(s)
    await session.commit()
    await session.refresh(row)
    return (await _out(session, [row], await _icp_names(lead_intel)))[0]


@router.patch("/campaigns/{campaign_id}/status", response_model=CampaignOut)
async def set_status(campaign_id: UUID, body: StatusIn, workspace_id: UUID = Depends(require_workspace_id),
                     _user: CurrentUser = Depends(require_manager), session: AsyncSession = Depends(get_session),
                     lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo)):  # fmt: skip
    """Start (active), pause, or archive. Starting needs a connected mailbox; each lead's next email is timed again
    inside the window."""
    row = await _campaign(session, workspace_id, campaign_id, lock=True)
    if body.status == "active":
        if row.channel != "email":
            if not await session.scalar(select(SocialAccount.id).where(SocialAccount.workspace_id == workspace_id, SocialAccount.provider == row.channel,
                                                                       SocialAccount.status == "ok").limit(1)):  # fmt: skip
                raise HTTPException(409, f"No connected {NETWORK[row.channel]} account yet. Connect one under Social accounts first.")
        elif not await session.scalar(select(SenderAccount.id).where(SenderAccount.workspace_id == workspace_id, SenderAccount.status == "ok", SenderAccount.pool == "sending").limit(1)):
            raise HTTPException(409, "No connected sending mailbox yet. Add one under Sending accounts and test it first.")
        now = datetime.now(UTC)
        for e in await session.scalars(select(Enrollment).where(Enrollment.sequence_id == row.id, Enrollment.status == "active")):
            if e.next_run_at is None or e.next_run_at < now:
                e.next_run_at = next_send_time(row.schedule, now)
    row.status = body.status
    await session.commit()
    await session.refresh(row)
    return (await _out(session, [row], await _icp_names(lead_intel)))[0]


@router.delete("/campaigns/{campaign_id}", status_code=204)
async def delete_campaign(campaign_id: UUID, workspace_id: UUID = Depends(require_workspace_id),
                          _user: CurrentUser = Depends(require_manager), session: AsyncSession = Depends(get_session)):  # fmt: skip
    """Only one that has sent nothing: its leads are freed to be added elsewhere. Archive a campaign that has sent."""
    row = await _campaign(session, workspace_id, campaign_id, lock=True)
    if row.lead_intel_icp_id is not None:
        raise HTTPException(409, "Every ICP keeps its campaign, so it can't be deleted. Archive it to stop it for good.")
    sent = await session.scalar(select(StepRun.id).join(Enrollment, Enrollment.id == StepRun.enrollment_id)
                                .where(Enrollment.sequence_id == row.id, StepRun.status == "sent").limit(1))  # fmt: skip
    if sent is not None:
        raise HTTPException(409, "It has sent emails, so it's kept for its history. Archive it instead.")
    await _release(session, row.id, None)
    await session.delete(row)
    await session.commit()


async def _release(session: AsyncSession, campaign_id: UUID, lead_ids: list[UUID] | None) -> None:
    """Give back the address claims of leads who were never sent anything, so they can be queued again."""
    query = select(Enrollment).where(Enrollment.sequence_id == campaign_id)
    if lead_ids is not None:
        query = query.where(Enrollment.lead_id.in_(lead_ids))
    for e in await session.scalars(query):
        if await session.scalar(select(StepRun.id).where(StepRun.enrollment_id == e.id, StepRun.status == "sent").limit(1)) is None:
            lead = await session.get(Lead, e.lead_id)
            if lead and lead.email:
                await session.execute(delete(QueuedAddress).where(QueuedAddress.email == lead.email.strip().lower(),
                                                                  QueuedAddress.campaign_id == f"own:{campaign_id}"))  # fmt: skip
            if e.email_id:
                email = await session.get(Email, e.email_id)
                if email is not None and email.status == "queued" and email.sent_at is None:
                    email.status, email.queued_at = "approved", None


# ---------- Leads in a campaign ----------


@router.get("/campaigns/{campaign_id}/candidates", response_model=list[CandidateOut])
async def candidates(campaign_id: UUID, q: str = Query(default="", max_length=200), approved_only: bool = Query(default=True, alias="approvedOnly"),
                     icp_only: bool = Query(default=True, alias="icpOnly"),
                     workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session)):  # fmt: skip
    """Leads that could be added: an email address, not in a running campaign, never queued. With approvedOnly, only
    those with an approved Coldbrew first email (what step 1's {{email1Body}} sends). With icpOnly, an ICP's campaign
    offers that ICP's leads."""
    campaign = await _campaign(session, workspace_id, campaign_id)
    if campaign.channel != "email":
        return await _social_candidates(session, workspace_id, campaign, q, icp_only)
    running = select(Enrollment.lead_id).where(Enrollment.status.in_(("active", "paused")),
                                               Enrollment.sequence_id.in_(select(Sequence.id).where(Sequence.channel == "email")))  # fmt: skip
    approved = select(Email.lead_id).where(Email.step == 1, Email.status == "approved")
    query = select(Lead).where(Lead.workspace_id == workspace_id, Lead.email.is_not(None), Lead.email != "",
                               Lead.id.not_in(running), func.lower(Lead.email).not_in(select(QueuedAddress.email)))  # fmt: skip
    if approved_only:
        query = query.where(Lead.id.in_(approved))
    if icp_only and campaign.lead_intel_icp_id is not None:
        query = query.where(Lead.lead_intel_icp_id == campaign.lead_intel_icp_id)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(func.concat(Lead.first_name, " ", Lead.last_name).ilike(like), Lead.email.ilike(like), Lead.company.ilike(like)))
    leads = list(await session.scalars(query.order_by(Lead.created_at.desc()).limit(500)))
    with_email = set(await session.scalars(approved.where(Email.lead_id.in_([lead.id for lead in leads])))) if leads else set()
    return [CandidateOut(id=lead.id, name=_name(lead), email=lead.email, company=lead.company, has_first_email=lead.id in with_email) for lead in leads]


async def _social_candidates(session: AsyncSession, workspace_id: UUID, campaign: Sequence, q: str, icp_only: bool) -> list[CandidateOut]:
    """Leads with a profile on the campaign's network, not in a running campaign on it and never messaged there."""
    from services.own_social import profile_url
    from services.unipile import profile_identifier

    column = Lead.linkedin_url if campaign.channel == "linkedin" else Lead.instagram_url
    same = select(Sequence.id).where(Sequence.workspace_id == workspace_id, Sequence.channel == campaign.channel)
    taken = select(Enrollment.lead_id).where(Enrollment.sequence_id.in_(same), or_(Enrollment.status.in_(("active", "paused")),
                                             Enrollment.id.in_(select(StepRun.enrollment_id).where(StepRun.status == "sent"))))  # fmt: skip
    query = select(Lead).where(Lead.workspace_id == workspace_id, column.is_not(None), column != "", Lead.id.not_in(taken))
    if icp_only and campaign.lead_intel_icp_id is not None:
        query = query.where(Lead.lead_intel_icp_id == campaign.lead_intel_icp_id)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(func.concat(Lead.first_name, " ", Lead.last_name).ilike(like), Lead.email.ilike(like), Lead.company.ilike(like), column.ilike(like)))
    leads = [lead for lead in await session.scalars(query.order_by(Lead.created_at.desc()).limit(500))
             if profile_identifier(campaign.channel, profile_url(lead, campaign.channel))]  # fmt: skip
    return [CandidateOut(id=lead.id, name=_name(lead), email=lead.email or "", company=lead.company, has_first_email=False,
                         profile_url=profile_url(lead, campaign.channel)) for lead in leads]  # fmt: skip


@router.post("/campaigns/{campaign_id}/enrollments", response_model=EnrollOut)
async def add_leads(campaign_id: UUID, body: EnrollIn, workspace_id: UUID = Depends(require_workspace_id),
                    _user: CurrentUser = Depends(require_manager), session: AsyncSession = Depends(get_session)):  # fmt: skip
    from services.own_social import enroll_social

    row = await _campaign(session, workspace_id, campaign_id, lock=True)
    if row.status == "archived":
        raise HTTPException(409, "This campaign is archived.")
    put_in = enroll if row.channel == "email" else enroll_social
    outcome = await put_in(session, workspace_id, row, list(dict.fromkeys(body.lead_ids)))
    return EnrollOut(added=outcome.added, skipped=outcome.skipped)


@router.get("/campaigns/{campaign_id}/enrollments", response_model=list[EnrollmentOut])
async def list_enrollments(campaign_id: UUID, status: str = Query(default="", pattern="^(|active|paused|completed|stopped)$"),
                           workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session)):  # fmt: skip
    campaign = await _campaign(session, workspace_id, campaign_id)
    query = select(Enrollment, Lead).join(Lead, Lead.id == Enrollment.lead_id).where(Enrollment.sequence_id == campaign_id)
    if status:
        query = query.where(Enrollment.status == status)
    rows = (await session.execute(query.order_by(Enrollment.created_at.desc()).limit(2000))).all()
    ids = [e.id for e, _ in rows]
    sent = dict((await session.execute(select(StepRun.enrollment_id, func.count()).where(StepRun.enrollment_id.in_(ids), StepRun.status == "sent")
                                       .group_by(StepRun.enrollment_id))).all()) if ids else {}  # fmt: skip
    last_run = dict((await session.execute(select(StepRun.enrollment_id, func.max(StepRun.ran_at)).where(StepRun.enrollment_id.in_(ids))
                                           .group_by(StepRun.enrollment_id))).all()) if ids else {}  # fmt: skip
    last_in = dict((await session.execute(select(InboundEvent.enrollment_id, func.max(InboundEvent.received_at)).where(InboundEvent.enrollment_id.in_(ids))
                                          .group_by(InboundEvent.enrollment_id))).all()) if ids else {}  # fmt: skip
    senders = dict((await session.execute(select(SenderAccount.id, SenderAccount.from_email).where(
        SenderAccount.id.in_({e.sender_account_id for e, _ in rows if e.sender_account_id})))).all()) if rows else {}  # fmt: skip
    socials = await _social_names(session, {e.social_account_id for e, _ in rows if e.social_account_id})
    social = campaign.channel != "email"
    return [
        EnrollmentOut(id=e.id, lead_id=lead.id, name=_name(lead), email=lead.email, company=lead.company,
                      profile_url=(lead.linkedin_url if campaign.channel == "linkedin" else lead.instagram_url) if social else None,
                      sender=socials.get(e.social_account_id) if social else senders.get(e.sender_account_id),
                      status=e.status, stop_reason=e.stop_reason, next_step_order=e.next_step_order, next_run_at=e.next_run_at,
                      sent=int(sent.get(e.id, 0)), last_activity_at=max(filter(None, (last_run.get(e.id), last_in.get(e.id))), default=None),
                      created_at=e.created_at)  # fmt: skip
        for e, lead in rows
    ]


@router.patch("/enrollments/{enrollment_id}", response_model=EnrollmentOut)
async def act_on_enrollment(enrollment_id: UUID, body: EnrollmentActionIn, workspace_id: UUID = Depends(require_workspace_id),
                            _user: CurrentUser = Depends(require_manager), session: AsyncSession = Depends(get_session)):  # fmt: skip
    """Pause, resume or stop one lead. A lead stopped before anything was sent to them is freed to be queued again."""
    e = await session.scalar(select(Enrollment).where(Enrollment.workspace_id == workspace_id, Enrollment.id == enrollment_id).with_for_update())
    if e is None:
        raise HTTPException(404, "Lead not found in a campaign")
    if e.status in ("completed", "stopped"):
        raise HTTPException(409, "This lead's campaign has ended.")
    sequence = await session.get(Sequence, e.sequence_id)
    if body.action == "pause":
        e.status, e.next_run_at = "paused", None
    elif body.action == "resume":
        e.status, e.next_run_at = "active", next_send_time(sequence.schedule, datetime.now(UTC))
    else:
        e.status, e.stop_reason, e.next_run_at = "stopped", "manual", None
        await _release(session, e.sequence_id, [e.lead_id])
    await session.commit()
    rows = await list_enrollments(e.sequence_id, "", workspace_id, session)
    return next(r for r in rows if r.id == e.id)


class PreviewEmail(ApiModel):
    step_order: int
    delay_days: int
    thread_mode: str
    subject: str
    body: str
    state: Literal["ready", "to_write", "problem"]
    note: str | None = None


class PreviewOut(ApiModel):
    lead_name: str
    lead_email: str | None
    sender: str | None
    emails: list[PreviewEmail]


@router.post("/enrollments/{enrollment_id}/preview", response_model=PreviewOut)
async def preview(
    enrollment_id: UUID,
    write: bool = Query(default=False),
    workspace_id: UUID = Depends(require_workspace_id),
    _user: CurrentUser = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    writer: EmailWriter | None = Depends(get_optional_writer),
):
    """Every email of the campaign as this lead would get it: variables filled, the signature under it, follow-ups
    threaded as "Re:". A {{followUp}} already written is shown as written; with write=true, Coldbrew writes the missing
    ones now (Claude, logged to Usage) and they're kept, so the same text goes out later. Nothing is sent."""
    e = await session.scalar(select(Enrollment).where(Enrollment.workspace_id == workspace_id, Enrollment.id == enrollment_id))
    if e is None:
        raise HTTPException(404, "Lead not found in a campaign")
    sequence = await session.get(Sequence, e.sequence_id)
    if sequence.channel != "email":
        return await _social_preview(session, e, sequence, write, writer, lead_intel)
    lead = await session.get(Lead, e.lead_id)
    account = await session.get(SenderAccount, e.sender_account_id) if e.sender_account_id else None
    persona = await session.get(Persona, account.persona_id) if account and account.persona_id else None
    steps = list(await session.scalars(select(SequenceStep).where(SequenceStep.sequence_id == e.sequence_id).order_by(SequenceStep.step_order)))
    first = await session.get(Email, e.email_id) if e.email_id else None
    values = {
        "firstName": lead.first_name, "lastName": lead.last_name, "company": lead.company, "title": lead.title,
        "email1Subject": first.subject if first else "", "email1Body": (first.edited_body or first.body) if first else "",
        "senderName": (account.from_name if account else "") or (persona.name if persona else ""), "signature": persona.signature if persona else "",
    }  # fmt: skip
    if write and writer is None:
        raise HTTPException(503, "ANTHROPIC_API_KEY isn't set, so Coldbrew can't write follow-ups.")
    write_one = coldbrew_writer(writer, lead_intel) if write and writer else None
    out: list[PreviewEmail] = []
    first_subject = ""
    earlier: dict[int, str] = {}
    for step in steps:
        state, note = "ready", None
        if "followUp" in step.body:
            done = await stored_follow_up(session, lead.id, step.step_order)
            if done is not None:
                values["followUp"] = done.edited_body or done.body
            elif first is None:
                state, note = "problem", "This lead has no Coldbrew first email, which a follow-up is written from."
            elif write_one is not None:
                text, why = await write_one(session, lead, first, step.step_order, earlier)
                values["followUp"], state, note = (text or ""), ("ready" if text else "problem"), why
            else:
                values["followUp"] = ""
                state, note = "to_write", "Coldbrew writes this follow-up for the lead when it's due. Write it now to read it first."
        try:
            body = render(step.body, values) if state != "to_write" else step.body
            subject = render(step.subject, values) if (step.step_order == 1 or step.thread_mode == "new") else reply_subject(first_subject)
        except MissingValue as missing:
            state, note = "problem", f"{{{{{missing.name}}}}} is empty for this lead. Add a fallback, like {{{{{missing.name}|...}}}}."
            body, subject = step.body, step.subject
        if step.step_order == 1 or step.thread_mode == "new":
            first_subject = subject
        if state == "ready":
            earlier[step.step_order] = body
            if values["signature"].strip():
                body = f"{body.rstrip()}\n\n{values['signature'].strip()}"
        out.append(PreviewEmail(step_order=step.step_order, delay_days=step.delay_days, thread_mode=step.thread_mode,
                                subject=subject, body=body, state=state, note=note))  # fmt: skip
    return PreviewOut(lead_name=_name(lead), lead_email=lead.email, sender=account.from_email if account else None, emails=out)


async def _social_preview(session: AsyncSession, e: Enrollment, sequence: Sequence, write: bool, writer: EmailWriter | None,
                          lead_intel: LeadIntelRepo) -> PreviewOut:  # fmt: skip
    """Every LinkedIn or Instagram step as this lead would get it. An {{aiMessage}} already written is shown; with
    write=true Coldbrew writes the missing ones now (Claude, logged to Usage) and keeps them, so the same text goes
    out. Nothing is sent."""
    from services.own_social import ai_message, social_writer, step_values, written_text

    lead = await session.get(Lead, e.lead_id)
    account = await session.get(SocialAccount, e.social_account_id) if e.social_account_id else None
    if write and writer is None:
        raise HTTPException(503, "ANTHROPIC_API_KEY isn't set, so Coldbrew can't write LinkedIn and Instagram messages.")
    write_one = social_writer(writer, lead_intel) if write and writer else None
    values = await step_values(session, lead, account)
    out: list[PreviewEmail] = []
    for step in await session.scalars(select(SequenceStep).where(SequenceStep.sequence_id == sequence.id).order_by(SequenceStep.step_order)):
        state, note = "ready", None
        if "aiMessage" in step.body:
            done = await written_text(session, e.id, step.id)
            if done is not None:
                values["aiMessage"] = done
            elif write_one is not None:
                text, note = await ai_message(session, e, lead, sequence.channel, step, write_one, keep=True)
                values["aiMessage"], state = (text or ""), ("ready" if text else "problem")
            else:
                values["aiMessage"] = ""
                state, note = "to_write", "Coldbrew writes this for the lead when it's due. Write it now to read it first."
        try:
            body = render(step.body, values) if state != "to_write" else step.body
        except MissingValue as missing:
            state, note, body = "problem", f"{{{{{missing.name}}}}} is empty for this lead. Add a fallback, like {{{{{missing.name}|...}}}}.", step.body
        if step.action == "invite" and state == "ready" and len(body) > LINKEDIN_NOTE_MAX:
            state, note = "problem", f"It's {len(body)} characters; LinkedIn allows {LINKEDIN_NOTE_MAX} (200 on free accounts). Write it again or shorten the prompt's limit."
        label = "Connection request" if step.action == "invite" else f"{NETWORK[sequence.channel]} message"
        out.append(PreviewEmail(step_order=step.step_order, delay_days=step.delay_days, thread_mode=step.action, subject=label, body=body, state=state, note=note))
    await session.commit()  # keeps what was written
    sender = (account.name or (f"@{account.username}" if account.username else None)) if account else None
    return PreviewOut(lead_name=_name(lead), lead_email=lead.linkedin_url if sequence.channel == "linkedin" else lead.instagram_url, sender=sender, emails=out)


# ---------- What happened ----------


@router.get("/own-sending/activity", response_model=list[ActivityOut])
async def activity(campaign_id: UUID | None = Query(default=None, alias="campaignId"), kind: str = Query(default="", pattern="^(|sends|replies|problems)$"),
                   limit: int = Query(default=200, ge=1, le=500), workspace_id: UUID = Depends(require_workspace_id),
                   session: AsyncSession = Depends(get_session)):  # fmt: skip
    """Every send and every incoming reply, bounce, unsubscribe or out-of-office, newest first."""
    names = dict((await session.execute(select(Sequence.id, Sequence.name).where(Sequence.workspace_id == workspace_id))).all())
    mailboxes = dict((await session.execute(select(SenderAccount.id, SenderAccount.from_email).where(SenderAccount.workspace_id == workspace_id))).all())
    socials = await _social_names(session, set(await session.scalars(select(SocialAccount.id).where(SocialAccount.workspace_id == workspace_id))))
    items: list[ActivityOut] = []
    if kind in ("", "sends", "problems"):
        q = (select(StepRun, Enrollment, Lead).join(Enrollment, Enrollment.id == StepRun.enrollment_id).join(Lead, Lead.id == Enrollment.lead_id)
             .where(StepRun.workspace_id == workspace_id, StepRun.status != "written"))  # fmt: skip
        if campaign_id:
            q = q.where(Enrollment.sequence_id == campaign_id)
        if kind == "problems":
            q = q.where(StepRun.status.in_(("failed", "skipped")))
        for run, e, lead in await session.execute(q.order_by(StepRun.ran_at.desc()).limit(limit)):
            items.append(ActivityOut(id=run.id, kind=run.status, at=run.ran_at, campaign_id=e.sequence_id, campaign=names.get(e.sequence_id),
                                     lead_id=lead.id, lead_name=_name(lead), lead_email=lead.email or "",
                                     mailbox=socials.get(run.social_account_id) if run.social_account_id else mailboxes.get(run.sender_account_id),
                                     subject=run.subject or {"invite": "Connection request", "message": "Message"}.get(run.action or "", ""),
                                     detail=run.error or ""))  # fmt: skip
    if kind in ("", "replies", "problems"):
        q = (select(InboundEvent, Lead, Enrollment).join(Lead, Lead.id == InboundEvent.lead_id, isouter=True)
             .join(Enrollment, Enrollment.id == InboundEvent.enrollment_id, isouter=True).where(InboundEvent.workspace_id == workspace_id))  # fmt: skip
        if campaign_id:
            q = q.where(Enrollment.sequence_id == campaign_id)
        if kind == "problems":
            q = q.where(InboundEvent.type.in_(("bounce", "unsubscribe")))
        for ev, lead, e in await session.execute(q.order_by(InboundEvent.received_at.desc()).limit(limit)):
            items.append(ActivityOut(id=ev.id, kind=ev.type, at=ev.received_at, campaign_id=e.sequence_id if e else None,
                                     campaign=names.get(e.sequence_id) if e else None, lead_id=lead.id if lead else None,
                                     lead_name=_name(lead) if lead else ev.from_email, lead_email=lead.email if lead and lead.email else ev.from_email,
                                     mailbox=mailboxes.get(ev.sender_account_id), subject=ev.subject, detail=ev.snippet))  # fmt: skip
    items.sort(key=lambda a: a.at, reverse=True)
    return items[:limit]

