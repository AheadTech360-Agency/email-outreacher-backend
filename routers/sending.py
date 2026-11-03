"""Sending through CheapSequencer: settings, campaigns, queueing approved emails, and follow-ups.

Nothing here activates a campaign or sends: emails wait in a campaign until a person activates it in
CheapSequencer. See services/outreach.py for how queueing and follow-ups work.
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from crud import (
    ExampleRepo, ExclusionRepo, IcpCampaignRepo, IcpProfileRepo, ProductRepo, PromptRepo, ProviderCallRepo, SendingRepo, SettingsRepo,
)  # fmt: skip
from dependencies import (
    get_example_repo, get_exclusion_repo, get_icp_campaign_repo, get_icp_profile_repo, get_lead_intel_repo, get_optional_sequencer,
    get_optional_writer, get_product_repo, get_prompt_repo, get_provider_call_repo, get_sending_repo, get_settings_repo,
    require_manager, require_workspace_id,
)  # fmt: skip
from lead_intel import LeadIntelRepo
from schemas import (
    CampaignSyncOut, FollowUpRunOut, OutboxRowOut, OutboxStepOut, QueueIn, QueueOut, QueueOutcomeOut, ReplyMessageOut, ReplyThreadDetailOut,
    ReplyThreadOut, SendingCampaignOut, SendingSettingsIn, SendingSettingsOut, TemplateOutcomeOut,
)  # fmt: skip
from services import outreach
from services.auth import CurrentUser
from services.generation import EmailWriter
from services.sending import SendingError, SequencerClient

router = APIRouter(prefix="/sending", tags=["sending"])


def _sequencer(sequencer: SequencerClient | None) -> SequencerClient:
    if sequencer is None:
        raise HTTPException(status_code=503, detail="CHEAPSEQUENCER_API_KEY is not set")
    return sequencer


async def _deps(
    workspace_id: UUID = Depends(require_workspace_id),
    sending: SendingRepo = Depends(get_sending_repo),
    settings: SettingsRepo = Depends(get_settings_repo),
    profiles: IcpProfileRepo = Depends(get_icp_profile_repo),
    prompts: PromptRepo = Depends(get_prompt_repo),
    products: ProductRepo = Depends(get_product_repo),
    library: ExampleRepo = Depends(get_example_repo),
    exclusions: ExclusionRepo = Depends(get_exclusion_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    sequencer: SequencerClient | None = Depends(get_optional_sequencer),
    writer: EmailWriter | None = Depends(get_optional_writer),
    links: IcpCampaignRepo = Depends(get_icp_campaign_repo),
) -> outreach.Deps:
    return outreach.Deps(workspace_id, sending, settings, profiles, prompts, products, library, exclusions, calls,
                         lead_intel, _sequencer(sequencer), writer, links)  # fmt: skip


@router.get("/settings", response_model=SendingSettingsOut)
async def get_settings_(workspace_id: UUID = Depends(require_workspace_id), settings: SettingsRepo = Depends(get_settings_repo)):
    return SendingSettingsOut(**await settings.sending(workspace_id), templates=[])


@router.put("/settings", response_model=SendingSettingsOut)
async def save_settings(
    body: SendingSettingsIn,
    workspace_id: UUID = Depends(require_workspace_id),
    settings: SettingsRepo = Depends(get_settings_repo),
    deps: outreach.Deps = Depends(_deps),
    _: CurrentUser = Depends(require_manager),
) -> SendingSettingsOut:
    """Save, then write the signature into every Coldbrew campaign that can still be changed."""
    values = body.model_dump()
    values["sender_company"] = values["sender_company"].strip()
    values["sender_website"] = outreach.clean_website(values["sender_website"])
    if values["sender_website"] and not outreach.looks_like_website(values["sender_website"]):
        raise HTTPException(status_code=422, detail="The website should look like aheadtech360.com or https://aheadtech360.com.")
    saved = await settings.save_sending(workspace_id, values)
    try:
        templates = await outreach.sync_templates(deps)
    except SendingError as exc:
        templates = [outreach.TemplateOutcome("CheapSequencer", "skipped", exc.message)]
    return SendingSettingsOut(**saved, templates=[TemplateOutcomeOut(**vars(t)) for t in templates])


@router.get("/campaigns", response_model=list[SendingCampaignOut])
async def campaigns(deps: outreach.Deps = Depends(_deps)) -> list[SendingCampaignOut]:
    """Coldbrew's campaigns in CheapSequencer, with how many leads Coldbrew queued into each."""
    try:
        rows = await deps.sequencer.campaigns()
    except SendingError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    counts = await deps.sending.queued_counts(deps.workspace_id)
    return [
        SendingCampaignOut(id=c["id"], name=c.get("name") or "", status=c.get("status") or "", daily_send_cap=c.get("daily_send_cap"),
                           queued=counts.get(c["id"], 0))  # fmt: skip
        for c in rows
        if (c.get("name") or "").startswith(outreach.CAMPAIGN_PREFIX)
    ]


@router.post("/queue", response_model=QueueOut)
async def queue(body: QueueIn, deps: outreach.Deps = Depends(_deps)) -> QueueOut:
    """Put approved first emails into their ICP's campaign. The campaign isn't activated: nothing sends
    until a person activates it in CheapSequencer."""
    try:
        outcomes = await outreach.queue(deps, list(dict.fromkeys(body.email_ids)), set(body.risky_ok))
    except SendingError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    return QueueOut(
        queued=sum(o.status == "queued" for o in outcomes),
        results=[QueueOutcomeOut(email_id=o.email_id, status=o.status, reason=o.reason, campaign=o.campaign) for o in outcomes],
    )


@router.post("/follow-ups/run", response_model=FollowUpRunOut, dependencies=[Depends(require_manager)])
async def run_follow_ups(deps: outreach.Deps = Depends(_deps)) -> FollowUpRunOut:
    """Check every lead's place in its campaign now and write the follow-ups that are nearly due.
    This also runs on its own every 30 minutes."""
    result = await outreach.run_follow_ups(deps)
    return FollowUpRunOut(checked=result.checked, written=result.written, ended=result.ended,
                          cost_usd=round(result.cost_usd, 4), notes=result.notes)  # fmt: skip



async def _campaign_names(deps: outreach.Deps) -> dict[str, str]:
    """Coldbrew's campaigns by id. Raises SendingError when CheapSequencer can't be reached."""
    return {c["id"]: c.get("name") or "" for c in await deps.sequencer.campaigns() if (c.get("name") or "").startswith(outreach.CAMPAIGN_PREFIX)}


@router.get("/outbox", response_model=list[OutboxRowOut])
async def outbox(deps: outreach.Deps = Depends(_deps)) -> list[OutboxRowOut]:
    """Every lead Coldbrew queued, with its three emails and where the sequence stands, as of the
    last follow-up check. Read from Coldbrew's own records; only the campaign names come from CheapSequencer."""
    pairs = await deps.sending.outbox(deps.workspace_id)
    try:
        names = await _campaign_names(deps)
    except SendingError:
        names = {}
    settings = await deps.settings.sending(deps.workspace_id)
    fallbacks = {2: settings["followup_fallback_2"], 3: settings["followup_fallback_3"]}
    later = await deps.sending.follow_ups([lead.id for _, lead in pairs])
    rows = []
    for email, lead in pairs:
        steps = outreach.outbox_steps(email, lead, {s: later[(lead.id, s)] for s in (2, 3) if (lead.id, s) in later}, fallbacks)
        rows.append(OutboxRowOut(
            email_id=email.id, lead_id=lead.id, name=f"{lead.first_name or ''} {lead.last_name or ''}".strip() or (lead.email or ""),
            email=lead.email or "", title=lead.title or "", company=lead.company or "",
            campaign_id=email.sequencer_campaign_id or "", campaign=names.get(email.sequencer_campaign_id or "", "") or "Campaign",
            mailbox=email.sequencer_inbox_email, state=outreach.lead_state(email), sent=email.sequence_sent_count or 0,
            queued_at=email.queued_at, last_sent_at=email.sequence_last_sent_at, synced_at=email.sequence_synced_at,
            steps=[OutboxStepOut(**vars(st)) for st in steps],
        ))  # fmt: skip
    return rows


async def _with_leads(deps: outreach.Deps, threads: list[dict]) -> list[dict]:
    leads = await deps.sending.leads_by_email(deps.workspace_id, [t["lead_email"] for t in threads if t["lead_email"]])
    for t in threads:
        if lead := leads.get(t["lead_email"]):
            t["lead_id"] = lead.id
            t["lead_name"] = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or t["lead_name"]
    return threads


@router.get("/replies", response_model=list[ReplyThreadOut])
async def replies(deps: outreach.Deps = Depends(_deps)) -> list[ReplyThreadOut]:
    """Conversations in Coldbrew's campaigns where the lead wrote back, newest first, from
    CheapSequencer's unified inbox. Read-only: nothing is marked read or answered from here."""
    try:
        names = await _campaign_names(deps)
        found = await asyncio.gather(*(deps.sequencer.threads(cid) for cid in names))
        threads = [outreach.thread_summary(t, name) for name, rows in zip(names.values(), found, strict=True) for t in rows
                   if int(t.get("inbound_count") or 0) > 0]  # fmt: skip
    except SendingError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    threads.sort(key=lambda t: t["last_message_at"] or datetime.min.replace(tzinfo=UTC), reverse=True)
    return [ReplyThreadOut(**t) for t in await _with_leads(deps, threads)]


@router.get("/replies/{thread_id:path}", response_model=ReplyThreadDetailOut)
async def reply_thread(thread_id: str, deps: outreach.Deps = Depends(_deps)) -> ReplyThreadDetailOut:
    """One conversation with every message, oldest first."""
    try:
        names = await _campaign_names(deps)
        body = await deps.sequencer.thread(thread_id)
    except SendingError as exc:
        raise HTTPException(status_code=404 if exc.status == 404 else 502, detail=exc.message) from exc
    thread = body.get("thread") or {}
    sequence = thread.get("sequence")
    campaign_id = next((sequence.get(k) for k in ("id", "campaign_id", "sequence_id") if sequence.get(k)), None) if isinstance(sequence, dict) else sequence
    if campaign_id not in names:
        raise HTTPException(status_code=404, detail="That conversation isn't in a Coldbrew campaign")
    [summary] = await _with_leads(deps, [outreach.thread_summary(thread, names[campaign_id])])
    messages = sorted((outreach.message_summary(m) for m in body.get("messages") or []),
                      key=lambda m: m["sent_at"] or datetime.min.replace(tzinfo=UTC))  # fmt: skip
    return ReplyThreadDetailOut(**summary, messages=[ReplyMessageOut(**m) for m in messages])


@router.post("/campaigns/sync", response_model=list[CampaignSyncOut])
async def sync_campaigns(deps: outreach.Deps = Depends(_deps), _: CurrentUser = Depends(require_manager)) -> list[CampaignSyncOut]:
    """Give every ICP that's on in Lead Intel its "Coldbrew: <ICP>" campaign: link, rename or create
    (as a draft copy). Runs on its own when an ICP is created or renamed; this catches up the rest."""
    out = []
    for icp in await deps.lead_intel.list_icps():
        if not icp.get("enabled", True):
            continue
        try:
            result = await outreach.ensure_campaign(deps.sequencer, deps.links, icp["id"], icp["name"])
            out.append(CampaignSyncOut(icp=icp["name"], status=result.status, note=result.note))
        except SendingError as exc:
            out.append(CampaignSyncOut(icp=icp["name"], status="skipped", note=f"Campaign not set up: {exc.message}"))
    return out
