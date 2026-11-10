"""Social accounts (the sending dashboard): LinkedIn and Instagram accounts connected through Unipile
(services/unipile.py), and which mailbox each belongs to. Anyone in the workspace can see them; connecting, linking
and disconnecting is for Management and admins. Nothing here messages anyone.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from database import get_session
from dependencies import require_manager, require_workspace_id
from models import SenderAccount, SocialAccount
from schemas import ApiModel
from services.auth import CurrentUser
from services.unipile import Account, UnipileClient, UnipileError, notify_token

router = APIRouter(tags=["social accounts"])

NOT_SET = "Unipile isn't connected yet: add UNIPILE_DSN and UNIPILE_API_KEY to the backend on Railway."


async def get_unipile() -> AsyncIterator[UnipileClient | None]:
    s = get_settings()
    client = UnipileClient(s.unipile_dsn, s.unipile_api_key) if s.unipile_dsn and s.unipile_api_key else None
    try:
        yield client
    finally:
        if client:
            await client.aclose()


class SocialOut(ApiModel):
    id: UUID
    provider: str
    name: str
    username: str
    status: str
    sender_account_id: UUID | None
    sender_email: str | None
    last_synced_at: datetime | None
    created_at: datetime


class SocialListOut(ApiModel):
    configured: bool
    # Whether Unipile can tell us right away which account was connected (PUBLIC_API_URL); otherwise sync picks it up.
    notified: bool
    accounts: list[SocialOut]


class ConnectIn(ApiModel):
    provider: Literal["linkedin", "instagram"]
    # Where Unipile sends the person back to afterwards: this dashboard page.
    return_url: str | None = Field(default=None, max_length=500)


class LinkOut(ApiModel):
    url: str


class SocialEditIn(ApiModel):
    sender_account_id: UUID | None = None


async def _outs(session: AsyncSession, rows: list[SocialAccount]) -> list[SocialOut]:
    ids = {r.sender_account_id for r in rows if r.sender_account_id}
    emails = dict((await session.execute(select(SenderAccount.id, SenderAccount.from_email).where(SenderAccount.id.in_(ids))))) if ids else {}
    return [SocialOut(id=r.id, provider=r.provider, name=r.name, username=r.username, status=r.status, sender_account_id=r.sender_account_id,
                      sender_email=emails.get(r.sender_account_id), last_synced_at=r.last_synced_at, created_at=r.created_at) for r in rows]  # fmt: skip


async def upsert(session: AsyncSession, workspace_id: UUID, account: Account, now: datetime) -> SocialAccount:
    """Keep Unipile's account: update the one we have (in whichever workspace it was connected), or add it here."""
    row = await session.scalar(select(SocialAccount).where(SocialAccount.unipile_account_id == account.id))
    if row is None:
        row = SocialAccount(workspace_id=workspace_id, provider=account.provider, unipile_account_id=account.id)
        session.add(row)
    row.name, row.username, row.status, row.last_synced_at = account.name, account.username, account.status, now
    return row


def _return_url(raw: str | None) -> str | None:
    """Only back to one of our own addresses (AUTHORIZED_URLS): never an address elsewhere."""
    from config import origin_of

    if raw and origin_of(raw) in get_settings().authorized_url_list:
        return raw
    return None


def _notify_url(name: str) -> str | None:
    s = get_settings()
    if not s.public_api_url or not s.unipile_api_key:
        return None
    return f"{s.public_api_url.rstrip('/')}/api/v1/webhooks/unipile?token={notify_token(s.unipile_api_key, name)}"


@router.get("/social-accounts", response_model=SocialListOut)
async def list_social(workspace_id: UUID = Depends(require_workspace_id), session: AsyncSession = Depends(get_session)):
    rows = list(await session.scalars(select(SocialAccount).where(SocialAccount.workspace_id == workspace_id)
                                      .order_by(SocialAccount.provider, SocialAccount.name)))  # fmt: skip
    s = get_settings()
    return SocialListOut(configured=bool(s.unipile_dsn and s.unipile_api_key), notified=bool(s.public_api_url), accounts=await _outs(session, rows))


@router.post("/social-accounts/connect", response_model=LinkOut)
async def connect(body: ConnectIn, workspace_id: UUID = Depends(require_workspace_id), _user: CurrentUser = Depends(require_manager),
                  unipile: UnipileClient | None = Depends(get_unipile)):  # fmt: skip
    """A Unipile sign-in page for a new LinkedIn or Instagram account. Opened in a new tab; the account shows here once
    Unipile reports it (or after Sync)."""
    if unipile is None:
        raise HTTPException(503, NOT_SET)
    name = f"{workspace_id}:{body.provider}"
    try:
        return LinkOut(url=await unipile.sign_in_link(body.provider, name, _notify_url(name), _return_url(body.return_url)))
    except UnipileError as exc:
        raise HTTPException(502, exc.message) from exc


@router.post("/social-accounts/{social_id}/reconnect", response_model=LinkOut)
async def reconnect(social_id: UUID, return_url: str | None = Query(default=None, alias="returnUrl", max_length=500),
                    workspace_id: UUID = Depends(require_workspace_id), _user: CurrentUser = Depends(require_manager),
                    session: AsyncSession = Depends(get_session), unipile: UnipileClient | None = Depends(get_unipile)):  # fmt: skip
    """Sign an account in again, when LinkedIn or Instagram logged it out."""
    if unipile is None:
        raise HTTPException(503, NOT_SET)
    row = await _social(session, workspace_id, social_id)
    name = f"{workspace_id}:{row.provider}"
    try:
        return LinkOut(url=await unipile.sign_in_link(row.provider, name, _notify_url(name), _return_url(return_url), reconnect_account=row.unipile_account_id))
    except UnipileError as exc:
        raise HTTPException(502, exc.message) from exc


@router.post("/social-accounts/sync", response_model=SocialListOut)
async def sync(workspace_id: UUID = Depends(require_workspace_id), _user: CurrentUser = Depends(require_manager),
               session: AsyncSession = Depends(get_session), unipile: UnipileClient | None = Depends(get_unipile)):  # fmt: skip
    """Read every LinkedIn and Instagram account from Unipile: statuses are refreshed, and any not here yet is added to
    this workspace. Free (a read)."""
    if unipile is None:
        raise HTTPException(503, NOT_SET)
    try:
        accounts = await unipile.accounts()
    except UnipileError as exc:
        raise HTTPException(502, exc.message) from exc
    now = datetime.now(UTC)
    for account in accounts:
        await upsert(session, workspace_id, account, now)
    await session.commit()
    return await list_social(workspace_id, session)


async def _social(session: AsyncSession, workspace_id: UUID, social_id: UUID) -> SocialAccount:
    row = await session.scalar(select(SocialAccount).where(SocialAccount.workspace_id == workspace_id, SocialAccount.id == social_id))
    if row is None:
        raise HTTPException(404, "Social account not found")
    return row


@router.patch("/social-accounts/{social_id}", response_model=SocialOut)
async def edit(social_id: UUID, body: SocialEditIn, workspace_id: UUID = Depends(require_workspace_id),
               _user: CurrentUser = Depends(require_manager), session: AsyncSession = Depends(get_session)):  # fmt: skip
    """Which mailbox it belongs to (or none)."""
    row = await _social(session, workspace_id, social_id)
    if body.sender_account_id is not None and await session.scalar(
        select(SenderAccount.id).where(SenderAccount.workspace_id == workspace_id, SenderAccount.id == body.sender_account_id)
    ) is None:
        raise HTTPException(422, "That mailbox isn't in this workspace.")
    row.sender_account_id = body.sender_account_id
    await session.commit()
    await session.refresh(row)
    return (await _outs(session, [row]))[0]


@router.delete("/social-accounts/{social_id}", status_code=204)
async def disconnect(social_id: UUID, workspace_id: UUID = Depends(require_workspace_id), _user: CurrentUser = Depends(require_manager),
                     session: AsyncSession = Depends(get_session), unipile: UnipileClient | None = Depends(get_unipile)):  # fmt: skip
    """Disconnect it from Unipile too (it stops counting on the Unipile plan), then forget it here."""
    row = await _social(session, workspace_id, social_id)
    if unipile is not None:
        try:
            await unipile.disconnect(row.unipile_account_id)
        except UnipileError as exc:
            if exc.status != 404:  # already gone there: fine
                raise HTTPException(502, exc.message) from exc
    await session.delete(row)
    await session.commit()
