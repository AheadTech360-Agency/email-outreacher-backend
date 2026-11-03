"""Leads' recent posts, fetched through Apify only until there's enough to write with (services/smart_posts.py):
the company's LinkedIn page, the person's LinkedIn, the company's Instagram. One Apify run per source per batch.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import anthropic
from fastapi import APIRouter, Depends, Query

from background import report
from crud import PostRepo, ProviderCallRepo
from dependencies import get_apify, get_optional_anthropic, get_post_repo, get_provider_call_repo, require_manager, require_workspace_id
from schemas import LeadPostsResult, PostFetchIn, PostFetchResult, PostOut
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
