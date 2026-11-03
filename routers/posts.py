"""Leads' recent posts, fetched through Apify only until there's enough to write with (services/smart_posts.py):
the company's LinkedIn page, the person's LinkedIn, the company's Instagram. One Apify run per source per batch.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from background import report
from crud import CompanyRepo, PostRepo, ProviderCallRepo, StakeholderRepo
from database import get_session
from dependencies import (
    get_apify, get_company_repo, get_lead_intel_repo, get_optional_anthropic, get_post_repo, get_provider_call_repo,
    get_stakeholder_repo, require_manager, require_workspace_id,
)  # fmt: skip
from lead_intel import LeadIntelRepo
from routers.stakeholders import add_as_leads
from schemas import AddAsLeadsIn, GatheredFor, LeadPostsResult, PostFetchIn, PostFetchResult, PostGatherIn, PostGatherResult, PostOut
from services.apify import ApifyClient, RunResult
from services.smart_posts import JUDGE_MODEL, STAGES, Target, gather

router = APIRouter(prefix="/posts", tags=["posts"])

# Statuses a fetch may move a lead out of. Leads already drafted, approved or sent keep theirs.
REFRESHABLE = {"imported", "enriching", "enriched", "no_content", "failed"}


@router.get("", response_model=list[PostOut])
async def list_posts(
    lead_id: UUID | None = Query(default=None, alias="leadId"),
    workspace_id: UUID = Depends(require_workspace_id),
    posts: PostRepo = Depends(get_post_repo),
) -> list[PostOut]:
    return [PostOut.model_validate(p) for p in await posts.list(workspace_id, lead_id)]


@router.post("/fetch", response_model=PostFetchResult, dependencies=[Depends(require_manager)])
async def fetch_posts(
    body: PostFetchIn,
    workspace_id: UUID = Depends(require_workspace_id),
    posts: PostRepo = Depends(get_post_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    apify: ApifyClient = Depends(get_apify),
    claude: anthropic.AsyncAnthropic | None = Depends(get_optional_anthropic),
) -> PostFetchResult:
    """Posts for these leads, only as many as it takes (services/smart_posts.py): the company's LinkedIn, then
    their own LinkedIn, then the company's Instagram, each read only for the leads Claude still finds short of
    material. Apify is paid per post (about $0.002), Claude's check is a fraction of a cent a lead."""
    leads = await posts.leads(workspace_id, list(dict.fromkeys(body.lead_ids)))
    since = datetime.now(UTC) - timedelta(days=body.max_age_days)
    targets = [Target(lead, await posts.company_for(workspace_id, lead)) for lead in leads]
    stages = [stage for stage, network, _ in STAGES if network in body.sources]

    async def stored(t: Target) -> list:
        return await posts.stored(workspace_id, t.lead.id, t.company.id if t.company else None)

    async def record_apify(actor: str, run: RunResult | None, status: str) -> None:
        key = f"apify:run:{run.run_id}" if run and run.run_id else None
        await calls.record(workspace_id, "apify", actor, status, key=key, cost_usd=run.cost_usd if run else None, action="Fetch posts")

    async def record_claude(cost: float) -> None:
        await calls.record(workspace_id, "claude", "messages", "succeeded", model=JUDGE_MODEL, cost_usd=cost, action="Fetch posts")

    outcome = await gather(
        targets=targets, stages=stages, max_posts=body.max_posts, since=since, apify=apify, claude=claude, stored=stored,
        stored_count=lambda owner, network: posts.count_source(workspace_id, owner, network),
        save=lambda owner, found: posts.save_for(workspace_id, owner, found), record_apify=record_apify,
        record_claude=record_claude, progress=report,
    )  # fmt: skip

    results = []
    by_lead = {o.lead_id: o for o in outcome.leads}
    for t in targets:
        o = by_lead[t.lead.id]
        if not outcome.error and t.lead.status in REFRESHABLE:
            await posts.set_status(t.lead, "enriched" if await stored(t) else "no_content")
        results.append(LeadPostsResult(
            lead_id=t.lead.id, company_linkedin=o.counts.get("company_linkedin"), linkedin=o.counts.get("person_linkedin"),
            instagram=o.counts.get("company_instagram"), new=o.new, enough=o.enough, stopped_at=o.stopped_at, reason=o.reason,
        ))  # fmt: skip
    return PostFetchResult(results=results, cost_usd=outcome.apify_cost_usd, claude_cost_usd=outcome.claude_cost_usd, error=outcome.error)


@router.post("/gather", response_model=PostGatherResult, dependencies=[Depends(require_manager)])
async def gather_posts(
    body: PostGatherIn,
    workspace_id: UUID = Depends(require_workspace_id),
    posts: PostRepo = Depends(get_post_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
    companies: CompanyRepo = Depends(get_company_repo),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    session: AsyncSession = Depends(get_session),
    apify: ApifyClient = Depends(get_apify),
    claude: anthropic.AsyncAnthropic | None = Depends(get_optional_anthropic),
) -> PostGatherResult:
    """Posts for people (from the People page) and companies (from Companies), from the sources ticked: the
    company's LinkedIn, the person's LinkedIn, the company's Instagram, in that order. For each one it stops at the
    first source Claude finds enough in. A person's posts are kept on their working record (made here if they have
    none; they're not a lead until their email is queued)."""
    if not body.stakeholder_ids and not body.company_ids:
        raise HTTPException(status_code=422, detail="Pick people or companies.")
    targets: list[Target] = []
    who: dict = {}  # target key -> (kind, id shown)
    people = await stakeholders.get_many(workspace_id, list(dict.fromkeys(body.stakeholder_ids)))
    needs_record = [p for p in people if not p.lead_id and (p.linkedin_url or p.email)]
    if needs_record:
        await add_as_leads(AddAsLeadsIn(stakeholder_ids=[p.id for p in needs_record]), workspace_id, lead_intel, stakeholders, companies, session)
        people = await stakeholders.get_many(workspace_id, [p.id for p in people])
    lead_rows = {lead.id: lead for lead in await posts.leads(workspace_id, [p.lead_id for p in people if p.lead_id])}
    for p in people:
        lead = lead_rows.get(p.lead_id)
        company = (await companies.get(workspace_id, p.company_id)) if p.company_id else (await posts.company_for(workspace_id, lead) if lead else None)
        if lead is None and company is None:
            continue
        t = Target(lead, company)
        targets.append(t)
        who[t.key] = ("person", p.id)
    for c in await companies.get_many(workspace_id, list(dict.fromkeys(body.company_ids))):
        if c.id not in who:
            t = Target(None, c)
            targets.append(t)
            who[t.key] = ("company", c.id)

    async def stored(t: Target) -> list:
        return await posts.stored(workspace_id, t.lead.id if t.lead else None, t.company.id if t.company else None)

    async def record_apify(actor: str, run: RunResult | None, status: str) -> None:
        key = f"apify:run:{run.run_id}" if run and run.run_id else None
        await calls.record(workspace_id, "apify", actor, status, key=key, cost_usd=run.cost_usd if run else None, action="Read posts")

    async def record_claude(cost: float) -> None:
        await calls.record(workspace_id, "claude", "messages", "succeeded", model=JUDGE_MODEL, cost_usd=cost, action="Read posts")

    since = datetime.now(UTC) - timedelta(days=body.max_age_days)
    outcome = await gather(
        targets=targets, stages=list(body.sources), max_posts=body.max_posts, since=since, apify=apify, claude=claude, stored=stored,
        stored_count=lambda owner, network: posts.count_source(workspace_id, owner, network),
        save=lambda owner, found: posts.save_for(workspace_id, owner, found), record_apify=record_apify,
        record_claude=record_claude, progress=report,
    )  # fmt: skip
    results = []
    for o in outcome.leads:
        kind, shown = who[o.lead_id]
        results.append(GatheredFor(
            kind=kind, id=shown, company_linkedin=o.counts.get("company_linkedin"), linkedin=o.counts.get("person_linkedin"),
            instagram=o.counts.get("company_instagram"), new=o.new, enough=o.enough, stopped_at=o.stopped_at, reason=o.reason,
        ))  # fmt: skip
    for t in targets:
        if t.lead is not None and not outcome.error and t.lead.status in REFRESHABLE:
            await posts.set_status(t.lead, "enriched" if await stored(t) else "no_content")
    return PostGatherResult(results=results, cost_usd=outcome.apify_cost_usd, claude_cost_usd=outcome.claude_cost_usd, error=outcome.error)
