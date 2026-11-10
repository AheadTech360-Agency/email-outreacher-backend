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
from dependencies import require_manager, require_workspace_id
from models import Email, Enrollment, InboundEvent, Lead, QueuedAddress, SenderAccount, Sequence, SequenceStep, StepRun
from schemas import ApiModel
from services.auth import CurrentUser
from services.own_mail import VARIABLES, clean_schedule, next_send_time, unknown_variables
from services.own_sending import enroll

router = APIRouter(tags=["own sending"])

Status = Literal["draft", "active", "paused", "archived"]


# ---------- Shapes ----------


class ScheduleIn(ApiModel):
    timezone: str = Field(default="America/New_York", max_length=64)
    start: str = Field(default="09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(default="17:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    days: list[int] = Field(default=[1, 2, 3, 4, 5, 6], min_length=1, max_length=7)


class StepIn(ApiModel):
    delay_days: int = Field(default=0, ge=0, le=90)
    thread_mode: Literal["new", "reply"] = "reply"
    subject: str = Field(default="", max_length=300)
    body: str = Field(min_length=1, max_length=20000)


class CampaignIn(ApiModel):
    name: str = Field(min_length=1, max_length=120)
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
    company: str
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
    variables: dict[str, str]


# ---------- Helpers ----------


def _name(lead: Lead) -> str:
    return f"{lead.first_name} {lead.last_name}".strip() or (lead.email or "Unnamed")


async def _campaign(session: AsyncSession, workspace_id: UUID, campaign_id: UUID, lock: bool = False) -> Sequence:
    query = select(Sequence).where(Sequence.workspace_id == workspace_id, Sequence.id == campaign_id)
    row = await session.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise HTTPException(404, "Campaign not found")
    return row


def _check_steps(steps: list[StepIn]) -> None:
    for i, step in enumerate(steps, 1):
        if i == 1 and not step.subject.strip():
            raise HTTPException(422, "The first email needs a subject.")
        if step.thread_mode == "new" and not step.subject.strip():
            raise HTTPException(422, f"Email {i} starts a new thread, so it needs a subject.")
        if bad := unknown_variables(step.subject + step.body):
            raise HTTPException(422, f"Email {i} uses {', '.join('{{' + b + '}}' for b in bad)}, which isn't a variable. Use one of: "
                                     + ", ".join("{{" + v + "}}" for v in VARIABLES) + ".")  # fmt: skip


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


async def _out(session: AsyncSession, rows: list[Sequence]) -> list[CampaignOut]:
    ids = [r.id for r in rows]
    stats = await _stats(session, ids)
    steps: dict[UUID, list[SequenceStep]] = {i: [] for i in ids}
    for s in (await session.scalars(select(SequenceStep).where(SequenceStep.sequence_id.in_(ids)).order_by(SequenceStep.step_order))) if ids else []:
        steps[s.sequence_id].append(s)
    return [
        CampaignOut(id=r.id, name=r.name, status=r.status, schedule=r.schedule, stats=stats[r.id], created_at=r.created_at, updated_at=r.updated_at,
                    steps=[StepOut(id=s.id, step_order=s.step_order, delay_days=s.delay_days, thread_mode=s.thread_mode, subject=s.subject, body=s.body)
                           for s in steps[r.id]])  # fmt: skip
        for r in rows
    ]


# ---------- Campaigns ----------


@router.get("/own-sending/status", response_model=SendingStatusOut)
async def sending_status():
    """Whether the scheduler really sends (OWN_SENDING_LIVE), and the variables a step can use."""
    return SendingStatusOut(live=get_settings().own_sending_live, variables=VARIABLES)


@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session)):
    rows = list(await session.scalars(select(Sequence).where(Sequence.workspace_id == workspace_id).order_by(Sequence.created_at.desc())))
    return await _out(session, rows)


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
async def get_campaign(campaign_id: UUID, workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session)):
    return (await _out(session, [await _campaign(session, workspace_id, campaign_id)]))[0]


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
async def add_campaign(body: CampaignIn, workspace_id: UUID = Depends(require_workspace_id),
                       user: CurrentUser = Depends(require_manager), session: AsyncSession = Depends(get_session)):  # fmt: skip
    """A new campaign starts as a draft: nothing is sent from it until it's started."""
    _check_steps(body.steps)
    row = Sequence(workspace_id=workspace_id, name=body.name, status="draft", schedule=_schedule(body.schedule), created_by=user.id)
    session.add(row)
    await session.flush()
    for i, s in enumerate(body.steps, 1):
        session.add(SequenceStep(sequence_id=row.id, step_order=i, delay_days=0 if i == 1 else s.delay_days,
                                 thread_mode="new" if i == 1 else s.thread_mode, subject=s.subject.strip(), body=s.body))  # fmt: skip
    await session.commit()
    await session.refresh(row)
    return (await _out(session, [row]))[0]


@router.put("/campaigns/{campaign_id}", response_model=CampaignOut)
async def edit_campaign(campaign_id: UUID, body: CampaignIn, workspace_id: UUID = Depends(require_workspace_id),
                        _user: CurrentUser = Depends(require_manager), session: AsyncSession = Depends(get_session)):  # fmt: skip
    """Name, window and steps. Steps already sent stay as they went; leads part-way through carry on with the new text
    from their next step. A step can't be removed once a lead has received it."""
    row = await _campaign(session, workspace_id, campaign_id, lock=True)
    _check_steps(body.steps)
    current = list(await session.scalars(select(SequenceStep).where(SequenceStep.sequence_id == row.id).order_by(SequenceStep.step_order)))
    used = set(await session.scalars(select(StepRun.step_id).where(StepRun.step_id.in_([s.id for s in current]), StepRun.status == "sent"))) if current else set()
    if any(s.id in used for s in current[len(body.steps):]):
        raise HTTPException(409, "A removed email was already sent to some leads. Keep it, or start a new campaign.")
    row.name, row.schedule = body.name, _schedule(body.schedule)
    for i, s in enumerate(body.steps, 1):
        fields = {"delay_days": 0 if i == 1 else s.delay_days, "thread_mode": "new" if i == 1 else s.thread_mode, "subject": s.subject.strip(), "body": s.body}
        if i <= len(current):
            for k, v in fields.items():
                setattr(current[i - 1], k, v)
        else:
            session.add(SequenceStep(sequence_id=row.id, step_order=i, **fields))
    for s in current[len(body.steps):]:
        await session.delete(s)
    await session.commit()
    await session.refresh(row)
    return (await _out(session, [row]))[0]


@router.patch("/campaigns/{campaign_id}/status", response_model=CampaignOut)
async def set_status(campaign_id: UUID, body: StatusIn, workspace_id: UUID = Depends(require_workspace_id),
                     _user: CurrentUser = Depends(require_manager), session: AsyncSession = Depends(get_session)):  # fmt: skip
    """Start (active), pause, or archive. Starting needs a connected mailbox; each lead's next email is timed again
    inside the window."""
    row = await _campaign(session, workspace_id, campaign_id, lock=True)
    if body.status == "active":
        if not await session.scalar(select(SenderAccount.id).where(SenderAccount.workspace_id == workspace_id, SenderAccount.status == "ok").limit(1)):
            raise HTTPException(409, "No connected mailbox yet. Add one under Sender accounts and test it first.")
        now = datetime.now(UTC)
        for e in await session.scalars(select(Enrollment).where(Enrollment.sequence_id == row.id, Enrollment.status == "active")):
            if e.next_run_at is None or e.next_run_at < now:
                e.next_run_at = next_send_time(row.schedule, now)
    row.status = body.status
    await session.commit()
    await session.refresh(row)
    return (await _out(session, [row]))[0]


@router.delete("/campaigns/{campaign_id}", status_code=204)
async def delete_campaign(campaign_id: UUID, workspace_id: UUID = Depends(require_workspace_id),
                          _user: CurrentUser = Depends(require_manager), session: AsyncSession = Depends(get_session)):  # fmt: skip
    """Only one that has sent nothing: its leads are freed to be added elsewhere. Archive a campaign that has sent."""
    row = await _campaign(session, workspace_id, campaign_id, lock=True)
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
                     workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session)):  # fmt: skip
    """Leads that could be added: an email address, not in a running campaign, never queued. With approvedOnly, only
    those with an approved Coldbrew first email (what step 1's {{email1Body}} sends)."""
    await _campaign(session, workspace_id, campaign_id)
    running = select(Enrollment.lead_id).where(Enrollment.status.in_(("active", "paused")))
    approved = select(Email.lead_id).where(Email.step == 1, Email.status == "approved")
    query = select(Lead).where(Lead.workspace_id == workspace_id, Lead.email.is_not(None), Lead.email != "",
                               Lead.id.not_in(running), func.lower(Lead.email).not_in(select(QueuedAddress.email)))  # fmt: skip
    if approved_only:
        query = query.where(Lead.id.in_(approved))
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(func.concat(Lead.first_name, " ", Lead.last_name).ilike(like), Lead.email.ilike(like), Lead.company.ilike(like)))
    leads = list(await session.scalars(query.order_by(Lead.created_at.desc()).limit(500)))
    with_email = set(await session.scalars(approved.where(Email.lead_id.in_([lead.id for lead in leads])))) if leads else set()
    return [CandidateOut(id=lead.id, name=_name(lead), email=lead.email, company=lead.company, has_first_email=lead.id in with_email) for lead in leads]


@router.post("/campaigns/{campaign_id}/enrollments", response_model=EnrollOut)
async def add_leads(campaign_id: UUID, body: EnrollIn, workspace_id: UUID = Depends(require_workspace_id),
                    _user: CurrentUser = Depends(require_manager), session: AsyncSession = Depends(get_session)):  # fmt: skip
    row = await _campaign(session, workspace_id, campaign_id, lock=True)
    if row.status == "archived":
        raise HTTPException(409, "This campaign is archived.")
    outcome = await enroll(session, workspace_id, row, list(dict.fromkeys(body.lead_ids)))
    return EnrollOut(added=outcome.added, skipped=outcome.skipped)


@router.get("/campaigns/{campaign_id}/enrollments", response_model=list[EnrollmentOut])
async def list_enrollments(campaign_id: UUID, status: str = Query(default="", pattern="^(|active|paused|completed|stopped)$"),
                           workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session)):  # fmt: skip
    await _campaign(session, workspace_id, campaign_id)
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
    return [
        EnrollmentOut(id=e.id, lead_id=lead.id, name=_name(lead), email=lead.email, company=lead.company, sender=senders.get(e.sender_account_id),
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


# ---------- What happened ----------


@router.get("/own-sending/activity", response_model=list[ActivityOut])
async def activity(campaign_id: UUID | None = Query(default=None, alias="campaignId"), kind: str = Query(default="", pattern="^(|sends|replies|problems)$"),
                   limit: int = Query(default=200, ge=1, le=500), workspace_id: UUID = Depends(require_workspace_id),
                   session: AsyncSession = Depends(get_session)):  # fmt: skip
    """Every send and every incoming reply, bounce, unsubscribe or out-of-office, newest first."""
    names = dict((await session.execute(select(Sequence.id, Sequence.name).where(Sequence.workspace_id == workspace_id))).all())
    mailboxes = dict((await session.execute(select(SenderAccount.id, SenderAccount.from_email).where(SenderAccount.workspace_id == workspace_id))).all())
    items: list[ActivityOut] = []
    if kind in ("", "sends", "problems"):
        q = (select(StepRun, Enrollment, Lead).join(Enrollment, Enrollment.id == StepRun.enrollment_id).join(Lead, Lead.id == Enrollment.lead_id)
             .where(StepRun.workspace_id == workspace_id))  # fmt: skip
        if campaign_id:
            q = q.where(Enrollment.sequence_id == campaign_id)
        if kind == "problems":
            q = q.where(StepRun.status.in_(("failed", "skipped")))
        for run, e, lead in await session.execute(q.order_by(StepRun.ran_at.desc()).limit(limit)):
            items.append(ActivityOut(id=run.id, kind=run.status, at=run.ran_at, campaign_id=e.sequence_id, campaign=names.get(e.sequence_id),
                                     lead_id=lead.id, lead_name=_name(lead), lead_email=lead.email or "", mailbox=mailboxes.get(run.sender_account_id),
                                     subject=run.subject, detail=run.error or ""))  # fmt: skip
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

