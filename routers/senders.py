"""Sender accounts and personas, for Coldbrew's own sending (the sending dashboard).

A sender account is a mailbox: SMTP to send, IMAP to read replies. Its passwords go to Supabase Vault
(services/vault.py) in the same transaction as the account, and no response ever includes them: a page only learns
whether one is saved. Anyone in the workspace can see the accounts; adding, changing, testing and deleting them is for
Management and admins.

Nothing here sends an email: "Test" only signs in (services/mail_check.py).
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from dependencies import get_current_user, require_manager, require_workspace_id
from models import Enrollment, Persona, SenderAccount, SenderUsage
from schemas import ApiModel
from services import mail_check, warmup
from services.auth import CurrentUser
from services.vault import Vault, secret_name

router = APIRouter(tags=["own sending"])

EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
HOST = r"^[A-Za-z0-9.-]+$"
Security = Literal["ssl", "starttls"]


def get_vault(session: AsyncSession = Depends(get_session)) -> Vault:
    return Vault(session)


def get_mail_checker():
    return mail_check.check


# ---------- Shapes ----------


class SenderIn(ApiModel):
    label: str = Field(default="", max_length=80)
    from_name: str = Field(default="", max_length=120)
    from_email: str = Field(pattern=EMAIL, max_length=254)
    provider: str = Field(default="custom", max_length=40)
    persona_id: UUID | None = None
    smtp_host: str = Field(pattern=HOST, max_length=253)
    smtp_port: int = Field(ge=1, le=65535)
    smtp_security: Security
    smtp_username: str = Field(min_length=1, max_length=254)
    # Required when adding; when editing, left out (or empty) keeps the saved one.
    smtp_password: str | None = Field(default=None, max_length=512)
    imap_host: str | None = Field(default=None, pattern=HOST, max_length=253)
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    imap_security: Security | None = None
    imap_username: str | None = Field(default=None, max_length=254)
    imap_password: str | None = Field(default=None, max_length=512)
    daily_limit: int = Field(default=30, ge=1, le=500)
    # Which pool a new mailbox joins (sending: campaigns; warming: the email warmer). Moved later with /pool.
    pool: Literal["sending", "warming"] = "sending"
    warmup_daily_target: int = Field(default=10, ge=1, le=100)
    warmup_reply_percent: int = Field(default=30, ge=0, le=100)

    @field_validator("from_email", "smtp_username", "imap_username", "smtp_host", "imap_host")
    @classmethod
    def trimmed(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("imap_host", "imap_username", "imap_password", "smtp_password", mode="before")
    @classmethod
    def blank_is_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value


class SenderOut(ApiModel):
    id: UUID
    label: str
    from_name: str
    from_email: str
    provider: str
    persona_id: UUID | None
    persona_name: str | None
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str
    smtp_password_set: bool
    imap_host: str | None
    imap_port: int | None
    imap_security: str | None
    imap_username: str | None
    imap_password_set: bool
    daily_limit: int
    sent_today: int
    status: str
    last_checked_at: datetime | None
    last_error: str | None
    created_at: datetime
    pool: str = "sending"
    warmup_daily_target: int = 10
    warmup_reply_percent: int = 30
    warmup_started_at: datetime | None = None
    # Warm-up today and over the last 7 days (services/warmup.stats). Inbox rate: received warm-up emails that landed
    # in the inbox rather than spam; None until any were opened.
    warmup_target_today: int = 0
    warmup_sent_today: int = 0
    warmup_received_7d: int = 0
    warmup_inbox_rate_7d: float | None = None
    warmup_rescued_7d: int = 0
    warmup_replied_7d: int = 0


class SenderStatusIn(ApiModel):
    paused: bool


class PoolIn(ApiModel):
    pool: Literal["sending", "warming"]


class TestOut(ApiModel):
    ok: bool
    smtp_error: str | None
    imap_error: str | None
    imap_checked: bool
    account: SenderOut


class PersonaIn(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    title: str = Field(default="", max_length=120)
    signature: str = Field(default="", max_length=2000)

    @field_validator("name")
    @classmethod
    def name_given(cls, value):
        if not value.strip():
            raise ValueError("Enter a name")
        return value.strip()


class PersonaOut(ApiModel):
    id: UUID
    name: str
    title: str
    signature: str
    senders: int
    created_at: datetime


# ---------- Helpers ----------


async def _account(session: AsyncSession, workspace_id: UUID, account_id: UUID, lock: bool = False) -> SenderAccount:
    query = select(SenderAccount).where(SenderAccount.workspace_id == workspace_id, SenderAccount.id == account_id)
    row = await session.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise HTTPException(404, "Sender account not found")
    return row


async def _persona_ok(session: AsyncSession, workspace_id: UUID, persona_id: UUID | None) -> None:
    if persona_id is not None and await session.scalar(
        select(Persona.id).where(Persona.workspace_id == workspace_id, Persona.id == persona_id)
    ) is None:
        raise HTTPException(422, "That persona isn't in this workspace.")


def _imap_complete(body: SenderIn) -> bool:
    given = [body.imap_host, body.imap_port, body.imap_security, body.imap_username]
    if any(v is not None for v in given) and not all(v is not None for v in given):
        raise HTTPException(422, "To read replies, fill in all of the IMAP host, port, security and username (or none of them).")
    return all(v is not None for v in given)


async def _outs(session: AsyncSession, rows: list[SenderAccount]) -> list[SenderOut]:
    ids = [r.id for r in rows]
    today = datetime.now(UTC).date()
    sent = dict((await session.execute(
        select(SenderUsage.sender_account_id, SenderUsage.sent).where(SenderUsage.sender_account_id.in_(ids), SenderUsage.day == today)
    )).all()) if ids else {}  # fmt: skip
    persona_ids = {r.persona_id for r in rows if r.persona_id}
    names = dict((await session.execute(select(Persona.id, Persona.name).where(Persona.id.in_(persona_ids)))).all()) if persona_ids else {}
    now = datetime.now(UTC)
    warm = await warmup.stats(session, ids, now)

    def warm_fields(r: SenderAccount) -> dict:
        w = warm.get(r.id, {})
        landed = w.get("inbox_7d", 0) + w.get("spam_7d", 0)
        return {"pool": r.pool, "warmup_daily_target": r.warmup_daily_target, "warmup_reply_percent": r.warmup_reply_percent,
                "warmup_started_at": r.warmup_started_at, "warmup_target_today": warmup.target_today(r, now) if r.pool == "warming" else 0,
                "warmup_sent_today": w.get("sent_today", 0), "warmup_received_7d": w.get("received_7d", 0),
                "warmup_inbox_rate_7d": round(w["inbox_7d"] / landed, 3) if landed else None,
                "warmup_rescued_7d": w.get("rescued_7d", 0), "warmup_replied_7d": w.get("replied_7d", 0)}  # fmt: skip

    return [
        SenderOut(
            **warm_fields(r),
            id=r.id, label=r.label, from_name=r.from_name, from_email=r.from_email, provider=r.provider, persona_id=r.persona_id,
            persona_name=names.get(r.persona_id), smtp_host=r.smtp_host, smtp_port=r.smtp_port, smtp_security=r.smtp_security,
            smtp_username=r.smtp_username, smtp_password_set=r.smtp_secret_id is not None, imap_host=r.imap_host,
            imap_port=r.imap_port, imap_security=r.imap_security, imap_username=r.imap_username,
            imap_password_set=r.imap_secret_id is not None, daily_limit=r.daily_limit, sent_today=int(sent.get(r.id, 0)),
            status=r.status, last_checked_at=r.last_checked_at, last_error=r.last_error, created_at=r.created_at,
        )
        for r in rows
    ]  # fmt: skip


# ---------- Sender accounts ----------


@router.get("/senders", response_model=list[SenderOut])
async def list_senders(workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session)):
    rows = list(await session.scalars(select(SenderAccount).where(SenderAccount.workspace_id == workspace_id).order_by(SenderAccount.from_email)))
    return await _outs(session, rows)


@router.post("/senders", response_model=SenderOut, status_code=201)
async def add_sender(
    body: SenderIn,
    workspace_id: UUID = Depends(require_workspace_id),
    user: CurrentUser = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
    vault: Vault = Depends(get_vault),
):
    if not body.smtp_password:
        raise HTTPException(422, "Enter the SMTP password (an app password for Google Workspace or Microsoft 365).")
    has_imap = _imap_complete(body)
    if has_imap and not body.imap_password:
        raise HTTPException(422, "Enter the IMAP password too, or leave IMAP empty.")
    await _persona_ok(session, workspace_id, body.persona_id)
    address = body.from_email.lower()
    if await session.scalar(select(SenderAccount.id).where(SenderAccount.workspace_id == workspace_id, SenderAccount.from_email == address)):
        raise HTTPException(409, f"{address} is already a sender account.")
    row = SenderAccount(
        workspace_id=workspace_id, persona_id=body.persona_id, label=body.label.strip(), from_name=body.from_name.strip(),
        from_email=address, provider=body.provider, smtp_host=body.smtp_host.lower(), smtp_port=body.smtp_port,
        smtp_security=body.smtp_security, smtp_username=body.smtp_username, daily_limit=body.daily_limit, created_by=user.id,
        pool=body.pool, warmup_daily_target=body.warmup_daily_target, warmup_reply_percent=body.warmup_reply_percent,
        warmup_started_at=datetime.now(UTC) if body.pool == "warming" else None,
        imap_host=body.imap_host.lower() if has_imap else None, imap_port=body.imap_port if has_imap else None,
        imap_security=body.imap_security if has_imap else None, imap_username=body.imap_username if has_imap else None,
    )  # fmt: skip
    session.add(row)
    await session.flush()  # its id names the Vault secrets
    row.smtp_secret_id = await vault.create(body.smtp_password, secret_name(row.id, "smtp"), f"SMTP password for {address}")
    if has_imap:
        row.imap_secret_id = await vault.create(body.imap_password, secret_name(row.id, "imap"), f"IMAP password for {address}")
    await session.commit()
    await session.refresh(row)
    return (await _outs(session, [row]))[0]


@router.put("/senders/{account_id}", response_model=SenderOut)
async def edit_sender(
    account_id: UUID,
    body: SenderIn,
    workspace_id: UUID = Depends(require_workspace_id),
    _user: CurrentUser = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
    vault: Vault = Depends(get_vault),
):
    row = await _account(session, workspace_id, account_id, lock=True)
    has_imap = _imap_complete(body)
    if has_imap and not body.imap_password and row.imap_secret_id is None:
        raise HTTPException(422, "Enter the IMAP password too, or leave IMAP empty.")
    await _persona_ok(session, workspace_id, body.persona_id)
    address = body.from_email.lower()
    if address != row.from_email and await session.scalar(
        select(SenderAccount.id).where(SenderAccount.workspace_id == workspace_id, SenderAccount.from_email == address)
    ):
        raise HTTPException(409, f"{address} is already a sender account.")
    changed_login = (body.smtp_host.lower(), body.smtp_port, body.smtp_security, body.smtp_username) != (
        row.smtp_host, row.smtp_port, row.smtp_security, row.smtp_username)  # fmt: skip
    row.label, row.from_name, row.from_email, row.provider = body.label.strip(), body.from_name.strip(), address, body.provider
    row.persona_id, row.daily_limit = body.persona_id, body.daily_limit
    row.warmup_daily_target, row.warmup_reply_percent = body.warmup_daily_target, body.warmup_reply_percent  # its pool moves with /pool
    row.smtp_host, row.smtp_port, row.smtp_security, row.smtp_username = body.smtp_host.lower(), body.smtp_port, body.smtp_security, body.smtp_username
    if body.smtp_password:
        row.smtp_secret_id = await vault.put(row.smtp_secret_id, body.smtp_password, secret_name(row.id, "smtp"), f"SMTP password for {address}")
    if has_imap:
        row.imap_host, row.imap_port, row.imap_security, row.imap_username = body.imap_host.lower(), body.imap_port, body.imap_security, body.imap_username
        if body.imap_password:
            row.imap_secret_id = await vault.put(row.imap_secret_id, body.imap_password, secret_name(row.id, "imap"), f"IMAP password for {address}")
    else:
        await vault.delete(row.imap_secret_id)
        row.imap_host = row.imap_port = row.imap_security = row.imap_username = row.imap_secret_id = None
    if changed_login or body.smtp_password or body.imap_password:
        # New details haven't been tried yet. A paused account stays paused.
        row.status = "paused" if row.status == "paused" else "untested"
        row.last_error = None
    await session.commit()
    await session.refresh(row)
    return (await _outs(session, [row]))[0]


@router.patch("/senders/{account_id}/status", response_model=SenderOut)
async def pause_sender(
    account_id: UUID,
    body: SenderStatusIn,
    workspace_id: UUID = Depends(require_workspace_id),
    _user: CurrentUser = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
):
    """Pause: nothing is sent from it until it's resumed. Resuming leaves it to be tested again."""
    row = await _account(session, workspace_id, account_id, lock=True)
    row.status = "paused" if body.paused else ("untested" if row.status == "paused" else row.status)
    await session.commit()
    await session.refresh(row)
    return (await _outs(session, [row]))[0]


@router.patch("/senders/{account_id}/pool", response_model=SenderOut)
async def move_pool(
    account_id: UUID,
    body: PoolIn,
    workspace_id: UUID = Depends(require_workspace_id),
    _user: CurrentUser = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
):
    """Move a mailbox between the sending pool (campaigns send from it) and the warming pool (it only warms up). Not to
    warming while campaign leads are part-way through with it: their follow-ups must come from the same mailbox."""
    row = await _account(session, workspace_id, account_id, lock=True)
    if body.pool == row.pool:
        return (await _outs(session, [row]))[0]
    if body.pool == "warming":
        busy = int(await session.scalar(select(func.count()).select_from(Enrollment).where(
            Enrollment.sender_account_id == row.id, Enrollment.status.in_(("active", "paused")))) or 0)  # fmt: skip
        if busy:
            raise HTTPException(409, f"{busy} campaign {'lead is' if busy == 1 else 'leads are'} part-way through with this mailbox, and their "
                                     "follow-ups must come from it. Let them finish, or stop them, then move it.")  # fmt: skip
        row.warmup_started_at = datetime.now(UTC)
    row.pool = body.pool
    await session.commit()
    await session.refresh(row)
    return (await _outs(session, [row]))[0]


@router.post("/senders/{account_id}/test", response_model=TestOut)
async def test_sender(
    account_id: UUID,
    workspace_id: UUID = Depends(require_workspace_id),
    _user: CurrentUser = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
    vault: Vault = Depends(get_vault),
    checker=Depends(get_mail_checker),
):
    """Sign in to SMTP (and IMAP, when set up) with the saved details. Nothing is sent."""
    row = await _account(session, workspace_id, account_id)
    smtp_password = await vault.read(row.smtp_secret_id)
    if not smtp_password:
        raise HTTPException(409, "No SMTP password is saved for this account. Edit it and enter one.")
    imap = None
    if row.imap_host and row.imap_secret_id:
        imap = mail_check.Endpoint(row.imap_host, row.imap_port, row.imap_security, row.imap_username, await vault.read(row.imap_secret_id) or "")
    result = await checker(mail_check.Endpoint(row.smtp_host, row.smtp_port, row.smtp_security, row.smtp_username, smtp_password), imap)
    row.last_checked_at = datetime.now(UTC)
    row.last_error = None if result.ok else " ".join(e for e in (result.smtp_error, result.imap_error) if e)
    if row.status != "paused":
        row.status = "ok" if result.ok else "failed"
    await session.commit()
    await session.refresh(row)
    return TestOut(ok=result.ok, smtp_error=result.smtp_error, imap_error=result.imap_error, imap_checked=result.imap_checked,
                   account=(await _outs(session, [row]))[0])  # fmt: skip


@router.delete("/senders/{account_id}", status_code=204)
async def delete_sender(
    account_id: UUID,
    workspace_id: UUID = Depends(require_workspace_id),
    _user: CurrentUser = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
    vault: Vault = Depends(get_vault),
):
    """Removes the account and its passwords from Vault, together."""
    row = await _account(session, workspace_id, account_id, lock=True)
    await vault.delete(row.smtp_secret_id)
    await vault.delete(row.imap_secret_id)
    await session.delete(row)
    await session.commit()


# ---------- Personas ----------


async def _persona_outs(session: AsyncSession, rows: list[Persona]) -> list[PersonaOut]:
    counts: dict[UUID, int] = {}
    ids = [r.id for r in rows]
    for persona_id in (await session.scalars(select(SenderAccount.persona_id).where(SenderAccount.persona_id.in_(ids)))) if ids else []:
        counts[persona_id] = counts.get(persona_id, 0) + 1
    return [PersonaOut(id=r.id, name=r.name, title=r.title, signature=r.signature, senders=counts.get(r.id, 0), created_at=r.created_at) for r in rows]


@router.get("/personas", response_model=list[PersonaOut])
async def list_personas(workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session)):
    rows = list(await session.scalars(select(Persona).where(Persona.workspace_id == workspace_id).order_by(Persona.name)))
    return await _persona_outs(session, rows)


async def _unique_persona(session: AsyncSession, workspace_id: UUID, name: str, other_than: UUID | None = None) -> None:
    rows = await session.scalars(select(Persona).where(Persona.workspace_id == workspace_id))
    if any(p.name.casefold() == name.casefold() and p.id != other_than for p in rows):
        raise HTTPException(409, f"There's already a persona called {name}.")


@router.post("/personas", response_model=PersonaOut, status_code=201)
async def add_persona(body: PersonaIn, workspace_id: UUID = Depends(require_workspace_id),
                      _user: CurrentUser = Depends(require_manager), session: AsyncSession = Depends(get_session)):  # fmt: skip
    await _unique_persona(session, workspace_id, body.name)
    row = Persona(workspace_id=workspace_id, name=body.name, title=body.title.strip(), signature=body.signature.strip())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return (await _persona_outs(session, [row]))[0]


@router.put("/personas/{persona_id}", response_model=PersonaOut)
async def edit_persona(persona_id: UUID, body: PersonaIn, workspace_id: UUID = Depends(require_workspace_id),
                       _user: CurrentUser = Depends(require_manager), session: AsyncSession = Depends(get_session)):  # fmt: skip
    row = await session.scalar(select(Persona).where(Persona.workspace_id == workspace_id, Persona.id == persona_id))
    if row is None:
        raise HTTPException(404, "Persona not found")
    await _unique_persona(session, workspace_id, body.name, other_than=row.id)
    row.name, row.title, row.signature = body.name, body.title.strip(), body.signature.strip()
    await session.commit()
    await session.refresh(row)
    return (await _persona_outs(session, [row]))[0]


@router.delete("/personas/{persona_id}", status_code=204)
async def delete_persona(persona_id: UUID, workspace_id: UUID = Depends(require_workspace_id),
                         _user: CurrentUser = Depends(require_manager), session: AsyncSession = Depends(get_session)):  # fmt: skip
    """Its sender accounts stay, with no persona."""
    result = await session.execute(delete(Persona).where(Persona.workspace_id == workspace_id, Persona.id == persona_id))
    if not result.rowcount:
        raise HTTPException(404, "Persona not found")
    await session.commit()
