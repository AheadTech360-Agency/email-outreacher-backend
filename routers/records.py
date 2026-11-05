"""Permanent workspace record deletion. All local changes commit together."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from dependencies import require_manager, require_workspace_id
from models import Company, Email, EmailFeedback, Lead, Post, Product, SendEvent, SmartListMember, Stakeholder, WorkspaceCompany
from schemas import ApiModel
from services.outreach import ENDED

router = APIRouter(prefix="/records", tags=["records"])


class DeleteRecordsIn(ApiModel):
    ids: list[UUID] = Field(min_length=1, max_length=5000)
    no_email_only: bool = False


async def delete_records(session: AsyncSession, workspace_id: UUID, kind: str, ids: list[UUID], no_email_only: bool = False) -> int:
    model = {"company": Company, "person": Stakeholder, "email": Email}[kind]
    if no_email_only and kind != "person":
        raise HTTPException(status_code=422, detail="The no-email filter is only valid for people.")
    try:
        query = select(model).where(model.workspace_id == workspace_id, model.id.in_(ids))
        if no_email_only:
            query = query.where(or_(Stakeholder.email.is_(None), func.trim(Stakeholder.email) == ""))
        rows = list(await session.scalars(query.with_for_update()))
        selected = [row.id for row in rows]
        if not selected:
            return 0
        people_ids, lead_ids, business_ids = [], [], []
        if kind == "company":
            business_ids = [row.lead_intel_business_id for row in rows if row.lead_intel_business_id]
            people = list(await session.scalars(select(Stakeholder).where(
                Stakeholder.workspace_id == workspace_id, Stakeholder.company_id.in_(selected)).with_for_update()))
            people_ids = [person.id for person in people]
            linked = [person.lead_id for person in people if person.lead_id]
            lead_ids = list(await session.scalars(select(Lead.id).where(Lead.workspace_id == workspace_id,
                or_(Lead.id.in_(linked), Lead.lead_intel_business_id.in_(business_ids))).with_for_update()))
        elif kind == "person":
            people_ids = selected
            linked = [row.lead_id for row in rows if row.lead_id]
            lead_ids = list(await session.scalars(select(Lead.id).where(
                Lead.workspace_id == workspace_id, Lead.id.in_(linked)).with_for_update()))
        email_condition = Email.id.in_(selected) if kind == "email" else Email.lead_id.in_(lead_ids)
        # Queued, or sent with the sequence still running: deleting would lose their replies and leave their
        # follow-ups to go out as the generic text. Finished, replied or stopped sequences can be deleted.
        in_sequence = await session.scalar(select(Email.id).where(
            Email.workspace_id == workspace_id, email_condition, Email.status.in_(("queued", "sent")),
            Email.sequence_stop_reason.is_(None), or_(Email.sequence_status.is_(None), Email.sequence_status.not_in(ENDED)),
        ).limit(1))  # fmt: skip
        if in_sequence:
            raise HTTPException(status_code=409, detail="Some of them are still in a sending sequence. Stop them in CheapSequencer first, or wait for their emails to finish.")
        if people_ids:
            await session.execute(delete(SmartListMember).where(SmartListMember.workspace_id == workspace_id,
                SmartListMember.kind == "person", SmartListMember.member_id.in_(people_ids)))
            await session.execute(delete(Stakeholder).where(Stakeholder.workspace_id == workspace_id, Stakeholder.id.in_(people_ids)))
        if lead_ids:
            # Database foreign keys cascade to posts, insights, emails, and email ratings.
            await session.execute(delete(Lead).where(Lead.workspace_id == workspace_id, Lead.id.in_(lead_ids)))
        if kind == "company":
            await session.execute(delete(SmartListMember).where(SmartListMember.workspace_id == workspace_id,
                SmartListMember.kind == "company", SmartListMember.member_id.in_(selected)))
            await session.execute(delete(WorkspaceCompany).where(WorkspaceCompany.workspace_id == workspace_id,
                WorkspaceCompany.lead_intel_business_id.in_(business_ids)))
            # Company pages and company-owned posts cascade in the database.
            await session.execute(delete(Company).where(Company.workspace_id == workspace_id, Company.id.in_(selected)))
        elif kind == "email":
            await session.execute(delete(Email).where(Email.workspace_id == workspace_id, Email.id.in_(selected)))
        await session.commit()
        return len(selected)
    except Exception:
        await session.rollback()
        raise


@router.post("/{kind}/delete", dependencies=[Depends(require_manager)])
async def remove_records(kind: Literal["company", "person", "email"], body: DeleteRecordsIn,
    workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session)) -> dict[str, int]:
    return {"deleted": await delete_records(session, workspace_id, kind, list(dict.fromkeys(body.ids)), body.no_email_only)}


class ArchiveRecordsIn(ApiModel):
    ids: list[UUID] = Field(min_length=1, max_length=5000)
    archived: bool = True


@router.post("/{kind}/archive", dependencies=[Depends(require_manager)])
async def archive_records(kind: Literal["company", "person", "product"], body: ArchiveRecordsIn,
    workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session)) -> dict[str, int]:
    model = {"company": Company, "person": Stakeholder, "product": Product}[kind]
    try:
        result = await session.execute(update(model).where(model.workspace_id == workspace_id,
            model.id.in_(list(dict.fromkeys(body.ids)))).values(archived=body.archived))
        await session.commit()
        return {"changed": result.rowcount}
    except Exception:
        await session.rollback()
        raise


@router.get("/person/{person_id}/timeline")
async def person_timeline(person_id: UUID, workspace_id: UUID = Depends(require_workspace_id),
    session: AsyncSession = Depends(get_session)) -> dict:
    from services.record_timeline import build_timeline
    person = await session.scalar(select(Stakeholder).where(Stakeholder.workspace_id == workspace_id, Stakeholder.id == person_id))
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    company = await session.scalar(select(Company).where(Company.workspace_id == workspace_id, Company.id == person.company_id)) if person.company_id else None
    owners = [Post.lead_id == person.lead_id] if person.lead_id else []
    if person.company_id: owners.append(Post.company_id == person.company_id)
    posts = list(await session.scalars(select(Post).where(Post.workspace_id == workspace_id, or_(*owners)).order_by(Post.fetched_at.desc()).limit(100))) if owners else []
    emails = list(await session.scalars(select(Email).where(Email.workspace_id == workspace_id, Email.lead_id == person.lead_id))) if person.lead_id else []
    email_ids = [email.id for email in emails]
    feedbacks = list(await session.scalars(select(EmailFeedback).where(EmailFeedback.workspace_id == workspace_id, EmailFeedback.email_id.in_(email_ids)))) if email_ids else []
    events = list(await session.scalars(select(SendEvent).where(SendEvent.workspace_id == workspace_id, SendEvent.lead_id == person.lead_id).order_by(SendEvent.occurred_at.desc()).limit(100))) if person.lead_id else []
    return {"events": build_timeline(person, company, posts, emails, feedbacks, events)}
