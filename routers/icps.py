from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from config import get_settings
from crud import IcpCampaignRepo, IcpProfileRepo, ProductRepo, PromptRepo
from services.access import AccessRepo, Scope
from dependencies import (
    get_access_repo, get_scope, require_runner,
    get_icp_campaign_repo,
    get_icp_profile_repo,
    get_lead_intel_icp_writer,
    get_lead_intel_repo,
    get_optional_sequencer,
    get_product_repo,
    get_prompt_repo,
    get_workspace_id,
    require_manager,
    require_workspace_id,
)
from lead_intel import LeadIntelRepo
from lead_intel_writes import LeadIntelIcpWriter
from schemas import ICP_SOURCES, Icp, IcpCreateIn, IcpProfileIn, IcpProfileOut
from services import outreach
from services.sending import SendingError, SequencerClient


router = APIRouter(prefix="/icps", tags=["icps"])


@router.get("", response_model=list[Icp])
async def list_icps(
    workspace_id: UUID | None = Depends(get_workspace_id),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    profiles: IcpProfileRepo = Depends(get_icp_profile_repo),
    scope: Scope = Depends(get_scope),
) -> list[Icp]:
    """Lead Intel's ICPs. With X-Workspace-Id, each carries that workspace's outreach profile. A Prospector gets only
    theirs and those shared with them, each saying whether they may edit it."""
    rows = await lead_intel.list_icps()
    if (only := await scope.ids("icp")) is not None:
        rows = [r for r in rows if r["id"] in only]
        editable = {r["id"] for r in rows if await scope.can_edit("icp", r["id"])}
        rows = [{**r, "can_edit": r["id"] in editable} for r in rows]
    by_id = await profiles.get_many(workspace_id, [row["id"] for row in rows]) if workspace_id else {}
    return [Icp.model_validate({**row, "profile": by_id.get(row["id"])}) for row in rows]


def _clean(terms: list[str]) -> list[str]:
    """Trimmed, blanks dropped, duplicates removed (ignoring case), order kept."""
    seen: set[str] = set()
    out = []
    for term in (t.strip() for t in terms):
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out


def _validated(body: IcpCreateIn) -> dict:
    """The ICP's values, cleaned the same way Lead Intel's own form does; 422 for anything invalid."""
    name, industry = body.name.strip(), body.industry.strip()
    if not name or not industry:
        raise HTTPException(status_code=422, detail="An ICP needs a name and an industry")
    if bad := [x for x in body.sources if x not in ICP_SOURCES]:
        raise HTTPException(status_code=422, detail=f"Unknown sources: {', '.join(bad)}")
    if body.min_followers is not None and body.max_followers is not None and body.min_followers > body.max_followers:
        raise HTTPException(status_code=422, detail="Fewest followers can't be more than most followers")
    values = body.model_dump()
    return values | {
        "name": name,
        "industry": industry,
        "sources": list(dict.fromkeys(body.sources)),
        **{key: _clean(values[key]) for key in ("any_of", "none_of", "website_any_of", "instagram_any_of", "search_terms", "places")},
    }


async def _campaign_note(sequencer: SequencerClient | None, links: IcpCampaignRepo, icp_id: UUID, name: str) -> str | None:
    """Give the ICP its CheapSequencer campaign. A failure never undoes the ICP; it's reported instead."""
    if sequencer is None:
        return None
    try:
        return (await outreach.ensure_campaign(sequencer, links, icp_id, name)).note
    except SendingError as exc:
        return f"Campaign not set up: {exc.message} Use \"Set up campaigns\" on the Mailboxes page to try again."


@router.post("", response_model=Icp, status_code=201)
async def create_icp(
    body: IcpCreateIn,
    writer: LeadIntelIcpWriter = Depends(get_lead_intel_icp_writer),
    sequencer: SequencerClient | None = Depends(get_optional_sequencer),
    links: IcpCampaignRepo = Depends(get_icp_campaign_repo),
    _: object = Depends(require_runner),
    scope: Scope = Depends(get_scope),
) -> Icp:
    """Add a new ICP to Lead Intel's icps table, which this app keeps using after Lead Intel shuts down.
    Companies for it come from Google Maps or Apollo searches, not from Lead Intel's matching. A Prospector's ICP is
    theirs, and gets no CheapSequencer campaign: they don't send (Management sets one up later if it's used)."""
    created_by = get_settings().lead_intel_icp_created_by
    if not created_by:
        raise HTTPException(status_code=503, detail="LEAD_INTEL_ICP_CREATED_BY is not set")
    values = _validated(body)
    if await writer.name_taken(values["name"]):
        raise HTTPException(status_code=409, detail="An ICP with this name already exists in Lead Intel")
    row = await writer.add_icp(created_by, values)
    await scope.created("icp", row["id"])
    note = None if scope.limited else await _campaign_note(sequencer, links, row["id"], row["name"])
    return Icp.model_validate({**row, "company_count": 0, "profile": None, "campaign_note": note, "can_edit": True})


@router.put("/{icp_id}", response_model=Icp)
async def update_icp(
    icp_id: UUID,
    body: IcpCreateIn,
    workspace_id: UUID | None = Depends(get_workspace_id),
    writer: LeadIntelIcpWriter = Depends(get_lead_intel_icp_writer),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    profiles: IcpProfileRepo = Depends(get_icp_profile_repo),
    sequencer: SequencerClient | None = Depends(get_optional_sequencer),
    links: IcpCampaignRepo = Depends(get_icp_campaign_repo),
    _: object = Depends(require_runner),
    scope: Scope = Depends(get_scope),
) -> Icp:
    """Edit an ICP's own fields in Lead Intel's icps table (shared: Lead Intel sees the change too).
    Only that ICP's row is updated. The next searches and research use the new definition."""
    await scope.check("icp", icp_id, edit=True)
    values = _validated(body)
    if await writer.name_taken(values["name"], except_id=icp_id):
        raise HTTPException(status_code=409, detail="Another ICP in Lead Intel already has this name")
    if await writer.update_icp(icp_id, values) is None:
        raise HTTPException(status_code=404, detail="ICP not found")
    # Read it back the usual way, so the answer matches GET /icps/{id} (company count, profile).
    row = await lead_intel.get_icp(icp_id)
    by_id = await profiles.get_many(workspace_id, [icp_id]) if workspace_id else {}
    note = None if scope.limited else await _campaign_note(sequencer, links, icp_id, row["name"])
    return Icp.model_validate({**row, "profile": by_id.get(icp_id), "campaign_note": note, "can_edit": True})


@router.get("/{icp_id}", response_model=Icp)
async def get_icp(
    icp_id: UUID,
    workspace_id: UUID | None = Depends(get_workspace_id),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    profiles: IcpProfileRepo = Depends(get_icp_profile_repo),
    scope: Scope = Depends(get_scope),
) -> Icp:
    await scope.check("icp", icp_id)
    row = await lead_intel.get_icp(icp_id)
    if not row:
        raise HTTPException(status_code=404, detail="ICP not found")
    by_id = await profiles.get_many(workspace_id, [icp_id]) if workspace_id else {}
    return Icp.model_validate({**row, "profile": by_id.get(icp_id), "can_edit": await scope.can_edit("icp", icp_id)})


@router.put("/{icp_id}/profile", response_model=IcpProfileOut, dependencies=[Depends(require_runner)])
async def save_icp_profile(
    icp_id: UUID,
    body: IcpProfileIn,
    workspace_id: UUID = Depends(require_workspace_id),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    profiles: IcpProfileRepo = Depends(get_icp_profile_repo),
    prompts: PromptRepo = Depends(get_prompt_repo),
    products: ProductRepo = Depends(get_product_repo),
    scope: Scope = Depends(get_scope),
) -> IcpProfileOut:
    """Save a workspace's outreach settings for a Lead Intel ICP. Writes `outreach.eo_icp_profiles` only. A Prospector
    changes an ICP they may edit, picks only a product they can see, and leaves its prompts as they are (prompts
    aren't theirs)."""
    if not await profiles.workspace_exists(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    await scope.check("icp", icp_id, edit=True)
    if not await lead_intel.get_icp(icp_id):
        raise HTTPException(status_code=404, detail="ICP not found")
    if scope.limited:
        current = (await profiles.get_many(workspace_id, [icp_id])).get(icp_id)
        body = body.model_copy(update={k: getattr(current, k, None) for k in ("verify_prompt_id", "hook_prompt_id", "email_prompt_id")})
        await scope.check("product", body.product_id)
    # A chosen prompt must be this workspace's, and of the right kind.
    for kind, prompt_id in (("verify", body.verify_prompt_id), ("hook", body.hook_prompt_id), ("email", body.email_prompt_id)):
        if prompt_id is None:
            continue
        prompt = await prompts.get(workspace_id, prompt_id)
        if prompt is None or prompt.kind != kind:
            raise HTTPException(status_code=422, detail=f"That {kind} prompt isn't one of this workspace's")
    if body.product_id and not await products.get(workspace_id, body.product_id):
        raise HTTPException(status_code=422, detail="That product isn't one of this workspace's")
    profile = await profiles.upsert(workspace_id, icp_id, body.model_dump())
    return IcpProfileOut.model_validate(profile)


async def _icp_out(icp_id: UUID, workspace_id: UUID | None, lead_intel: LeadIntelRepo, profiles: IcpProfileRepo, note: str | None) -> Icp:
    row = await lead_intel.get_icp(icp_id)
    by_id = await profiles.get_many(workspace_id, [icp_id]) if workspace_id else {}
    return Icp.model_validate({**row, "profile": by_id.get(icp_id), "campaign_note": note})


async def _retire(sequencer: SequencerClient | None, links: IcpCampaignRepo, icp_id: UUID) -> str | None:
    """Archive the ICP's campaign first, so an ICP is never archived or deleted while its campaign still has leads."""
    if sequencer is None:
        return "CHEAPSEQUENCER_API_KEY isn't set, so its campaign wasn't touched."
    try:
        name = await outreach.retire_campaign(sequencer, links, icp_id)
    except outreach.CampaignBusy as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except SendingError as exc:
        raise HTTPException(status_code=502, detail=f"Nothing was changed. {exc.message}") from exc
    return f'Its campaign "{name}" was archived in CheapSequencer.' if name else None


@router.post("/{icp_id}/archive", response_model=Icp)
async def archive_icp(
    icp_id: UUID,
    workspace_id: UUID | None = Depends(get_workspace_id),
    writer: LeadIntelIcpWriter = Depends(get_lead_intel_icp_writer),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    profiles: IcpProfileRepo = Depends(get_icp_profile_repo),
    sequencer: SequencerClient | None = Depends(get_optional_sequencer),
    links: IcpCampaignRepo = Depends(get_icp_campaign_repo),
    _: object = Depends(require_runner),
    scope: Scope = Depends(get_scope),
) -> Icp:
    """Turn the ICP off (its `enabled` flag in Lead Intel) and archive its campaign. Its leads, companies
    and history stay; Restore brings it back. Refused while its campaign still has leads in a sequence. A Prospector
    archives only an ICP they created (one shared with them may be in use elsewhere)."""
    await scope.check("icp", icp_id, owner=True)
    if not await lead_intel.get_icp(icp_id):
        raise HTTPException(status_code=404, detail="ICP not found")
    note = await _retire(sequencer, links, icp_id)
    await writer.update_icp(icp_id, {"enabled": False})
    return await _icp_out(icp_id, workspace_id, lead_intel, profiles, note)


@router.post("/{icp_id}/restore", response_model=Icp)
async def restore_icp(
    icp_id: UUID,
    workspace_id: UUID | None = Depends(get_workspace_id),
    writer: LeadIntelIcpWriter = Depends(get_lead_intel_icp_writer),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    profiles: IcpProfileRepo = Depends(get_icp_profile_repo),
    sequencer: SequencerClient | None = Depends(get_optional_sequencer),
    links: IcpCampaignRepo = Depends(get_icp_campaign_repo),
    _: object = Depends(require_runner),
    scope: Scope = Depends(get_scope),
) -> Icp:
    """Turn an archived ICP back on and give it a campaign again (a new draft copy; not for a Prospector's own)."""
    await scope.check("icp", icp_id, owner=True)
    row = await writer.update_icp(icp_id, {"enabled": True})
    if row is None:
        raise HTTPException(status_code=404, detail="ICP not found")
    note = None if scope.limited else await _campaign_note(sequencer, links, icp_id, row["name"])
    return await _icp_out(icp_id, workspace_id, lead_intel, profiles, note)


@router.delete("/{icp_id}", status_code=204)
async def delete_icp(
    icp_id: UUID,
    writer: LeadIntelIcpWriter = Depends(get_lead_intel_icp_writer),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    sequencer: SequencerClient | None = Depends(get_optional_sequencer),
    links: IcpCampaignRepo = Depends(get_icp_campaign_repo),
    access: AccessRepo = Depends(get_access_repo),
    _: object = Depends(require_manager),
) -> None:
    """Delete the ICP from Lead Intel's icps table and archive its campaign. Its companies, people and leads in
    Coldbrew are kept with no ICP (they show under "No ICP" until given another). Refused while Lead Intel
    businesses are in it (deleting would change Lead Intel's rows) or emails are queued or sent for it (its
    campaign is sending); archive it instead."""
    icp = await lead_intel.get_icp(icp_id)
    if not icp:
        raise HTTPException(status_code=404, detail="ICP not found")
    if businesses := await writer.businesses_in(icp_id):
        raise HTTPException(status_code=409, detail=f"{icp['name']} has {businesses} Lead Intel businesses in it, so it can't be deleted. Archive it instead.")
    if sending := await links.sending(icp_id):
        raise HTTPException(status_code=409, detail=f"{sending} emails for {icp['name']} are queued or sent, so it can't be deleted. Archive it instead.")
    await _retire(sequencer, links, icp_id)
    if not await writer.delete_icp(icp_id):
        raise HTTPException(status_code=409, detail=f"{icp['name']} couldn't be deleted: Lead Intel has businesses in it now. Archive it instead.")
    await links.detach(icp_id)
    await access.forget("icp", icp_id)
    await links.forget(icp_id)
