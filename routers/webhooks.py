"""Webhooks from CheapSequencer: replies, bounces and unsubscribes as they happen (services/webhooks.py).

Public on purpose: CheapSequencer can't sign in. A delivery is trusted only when its signature checks out
against CHEAPSEQUENCER_WEBHOOK_SECRET; anything else is refused or ignored, never acted on.
"""

import json
import logging
import time

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from config import get_settings
from crud import SendingRepo
from database import get_session
from dependencies import get_optional_sequencer, get_sending_repo
from services import notify
from services.sending import SendingError, SequencerClient
from services.webhooks import stop_of, verify

log = logging.getLogger("coldbrew.webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_BYTES = 256 * 1024


@router.post("/unipile")
async def unipile(request: Request, token: str = "", session=Depends(get_session)) -> dict[str, str]:
    """Unipile's hosted sign-in reporting an account: {"status": "CREATION_SUCCESS" | "RECONNECTED", "account_id",
    "name"}. Trusted only with the token our notify_url carried for that name (services/unipile.notify_token); the
    name says which workspace started it. The account itself is read from Unipile, not taken from the call."""
    import hmac as _hmac
    from datetime import UTC, datetime
    from uuid import UUID

    from routers.social import upsert
    from services.unipile import UnipileClient, UnipileError, notify_token

    settings = get_settings()
    if not settings.unipile_api_key or not settings.unipile_dsn:
        return {"status": "ignored"}
    try:
        event = json.loads(await request.body())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Not JSON") from exc
    name = str((event or {}).get("name") or "")
    if not name or not _hmac.compare_digest(token, notify_token(settings.unipile_api_key, name)):
        raise HTTPException(status_code=401, detail="Bad token")
    if event.get("status") not in ("CREATION_SUCCESS", "RECONNECTED") or not event.get("account_id"):
        return {"status": "ignored"}
    try:
        workspace_id = UUID(name.split(":", 1)[0])
    except ValueError:
        return {"status": "ignored"}
    client = UnipileClient(settings.unipile_dsn, settings.unipile_api_key)
    try:
        account = next((a for a in await client.accounts() if a.id == str(event["account_id"])), None)
    except UnipileError as exc:
        log.warning("couldn't read Unipile account %s: %s", event.get("account_id"), exc.message)
        return {"status": "retry later"}
    finally:
        await client.aclose()
    if account is None:
        return {"status": "ignored"}  # not LinkedIn or Instagram
    from sqlalchemy import delete

    from models import SocialConnect

    if await upsert(session, workspace_id, account, datetime.now(UTC)) is not None:
        # This sign-in is settled: it can't make Sync adopt some other account later.
        await session.execute(delete(SocialConnect).where(SocialConnect.workspace_id == workspace_id, SocialConnect.provider == account.provider))
    await session.commit()
    return {"status": "recorded"}


@router.post("/cheapsequencer")
async def cheapsequencer(
    request: Request,
    background: BackgroundTasks,
    sending: SendingRepo = Depends(get_sending_repo),
    sequencer: SequencerClient | None = Depends(get_optional_sequencer),
) -> dict[str, str]:
    body = await request.body()
    if len(body) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Too large")
    header = request.headers.get("X-CheapSequencer-Signature")
    secret = get_settings().cheapsequencer_webhook_secret
    if not header:
        # CheapSequencer's check that the address answers, when a subscription is made. Nothing in it is used.
        return {"status": "ok"}
    if not secret:
        # Signed, but the secret isn't set yet (it's only shown when the subscription is made): accept so the
        # subscription can be created, act on nothing. Set CHEAPSEQUENCER_WEBHOOK_SECRET to start using them.
        log.warning("CheapSequencer webhook received, but CHEAPSEQUENCER_WEBHOOK_SECRET isn't set: ignored")
        return {"status": "ignored"}
    if not verify(secret, header, body, time.time()):
        raise HTTPException(status_code=401, detail="Bad signature")
    try:
        event = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Not JSON") from exc
    stop = stop_of(event) if isinstance(event, dict) else None
    if stop is None:
        return {"status": "ignored"}

    email = await sending.first_email_for(stop.campaign_id, lead_id=stop.lead_id)
    if email is None and stop.lead_id and sequencer is not None:
        # Before the first follow-up check, the lead's CheapSequencer id isn't stored yet: match its address.
        try:
            lead = (await sequencer.get(f"/leads/{stop.lead_id}")).get("data") or {}
        except SendingError as exc:
            log.warning("couldn't read CheapSequencer lead %s for event %s: %s", stop.lead_id, stop.event_id, exc.message)
            lead = {}
        if lead.get("email"):
            email = await sending.first_email_for(stop.campaign_id, address=str(lead["email"]))
    if email is None:
        return {"status": "ignored"}  # not one of Coldbrew's leads
    # A reply the follow-up check saw first (marked replied with no reply event yet) was already emailed by it.
    seen_by_check = stop.type == "replied" and email.sequence_stop_reason == "replied" and not await sending.has_event(email.id, "replied")
    if await sending.record_stop(email, stop, event):
        log.info("%s for email %s (event %s)", stop.type, email.id, stop.event_id)
        if stop.type == "replied" and not seen_by_check and (lead := await sending.lead(email.lead_id)) is not None:
            background.add_task(notify.reply_received, lead)  # after the answer: CheapSequencer isn't kept waiting
        return {"status": "recorded"}
    return {"status": "duplicate"}  # a delivery seen before: stored once, emailed once
