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
from dependencies import get_optional_sequencer, get_sending_repo
from services import notify
from services.sending import SendingError, SequencerClient
from services.webhooks import stop_of, verify

log = logging.getLogger("coldbrew.webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_BYTES = 256 * 1024


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
