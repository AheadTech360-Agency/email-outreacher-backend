"""Quick Start: one ICP from a Google Maps search to drafts waiting for review, run in the background
(services/quickstart.py). Starting, stopping and resuming spend budget, so they're for Management and admins;
anyone in the workspace can watch a run and approve its drafts (on Review's endpoints)."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from crud import IcpProfileRepo, ProductRepo, QuickstartRepo
from dependencies import (
    get_icp_profile_repo, get_lead_intel_repo, get_product_repo, get_quickstart_queue, get_quickstart_repo, require_manager,
    require_workspace_id,
)  # fmt: skip
from lead_intel import LeadIntelRepo
from models import QuickstartRun
from routers.emails import NO_OFFER, has_offer
from schemas import (
    QuickstartCompanyOut, QuickstartDetailOut, QuickstartDraftOut, QuickstartIn, QuickstartLeadOut, QuickstartRunOut, QuickstartStep,
)  # fmt: skip
from services.auth import CurrentUser
from services.quickstart import STEPS

router = APIRouter(prefix="/quickstart", tags=["quickstart"])

NO_QUEUE = "The background worker's queue (Redis) can't be reached, so the run didn't start."


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(w.title() for w in rest)


def _out(run: QuickstartRun, icp_name: str) -> dict:
    # Settings are stored as the engine reads them (search_terms); the page reads them as the API speaks (searchTerms).
    settings = {_camel(k): v for k, v in run.settings.items()}
    return {
        "id": run.id, "icp_id": run.icp_id, "icp_name": icp_name, "status": run.status, "step": run.step, "settings": settings,
        "steps": {s: QuickstartStep(**(run.progress.get(s) or {})) for s in STEPS},
        "spent_usd": float(run.spent_usd), "spend_limit_usd": float(run.spend_limit_usd), "detail": run.detail,
        "created_at": run.created_at, "finished_at": run.finished_at,
    }  # fmt: skip


async def _name(lead_intel: LeadIntelRepo, icp_id: UUID) -> str:
    icp = await lead_intel.get_icp(icp_id)
    return icp["name"] if icp else "Deleted ICP"


async def _run(runs: QuickstartRepo, workspace_id: UUID, run_id: UUID) -> QuickstartRun:
    run = await runs.get(workspace_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Quick Start run not found")
    return run


@router.post("/runs", response_model=QuickstartRunOut, status_code=201)
async def start_run(
    body: QuickstartIn,
    workspace_id: UUID = Depends(require_workspace_id),
    user: CurrentUser = Depends(require_manager),
    runs: QuickstartRepo = Depends(get_quickstart_repo),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    profiles: IcpProfileRepo = Depends(get_icp_profile_repo),
    products: ProductRepo = Depends(get_product_repo),
    queue: Callable[[UUID], None] = Depends(get_quickstart_queue),
) -> QuickstartRunOut:
    """Start a run. The ICP must say what we sell, or no email could be written at the end."""
    icp = await lead_intel.get_icp(body.icp_id)
    if not icp:
        raise HTTPException(status_code=404, detail="ICP not found")
    profile = (await profiles.get_many(workspace_id, [body.icp_id])).get(body.icp_id)
    product = await products.get(workspace_id, profile.product_id) if profile and profile.product_id else None
    if not has_offer(profile, product):
        raise HTTPException(status_code=422, detail=NO_OFFER)
    settings = body.model_dump(mode="json", exclude={"icp_id", "spend_limit_usd"})
    settings["search_terms"] = [t.strip() for t in body.search_terms if t.strip()]
    settings["places"] = [p.strip() for p in body.places if p.strip()]
    if body.source == "google_maps":
        if not settings["search_terms"] or not settings["places"]:
            raise HTTPException(status_code=422, detail="Add at least one search term and one place.")
        if body.companies > 100:
            raise HTTPException(status_code=422, detail="From Google Maps, verify up to 100 companies a run.")
    elif not await runs.existing_companies(workspace_id, body.icp_id, body.smart_list_id, 1):
        raise HTTPException(status_code=422, detail="No companies for this ICP are waiting: each one already has someone written to.")
    run = await runs.create(workspace_id, user.id, body.icp_id, settings, body.spend_limit_usd)
    try:
        queue(run.id)
    except Exception as exc:  # noqa: BLE001 - Redis down: say so instead of a run that never moves
        run.status, run.detail, run.finished_at = "failed", NO_QUEUE, datetime.now(UTC)
        await runs.save(run)
        raise HTTPException(status_code=503, detail=NO_QUEUE) from exc
    return QuickstartRunOut(**_out(run, icp["name"]))


@router.get("/runs", response_model=list[QuickstartRunOut])
async def list_runs(
    workspace_id: UUID = Depends(require_workspace_id),
    runs: QuickstartRepo = Depends(get_quickstart_repo),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
) -> list[QuickstartRunOut]:
    names: dict[UUID, str] = {}
    out = []
    for run in await runs.recent(workspace_id):
        if run.icp_id not in names:
            names[run.icp_id] = await _name(lead_intel, run.icp_id)
        out.append(QuickstartRunOut(**_out(run, names[run.icp_id])))
    return out


@router.get("/runs/{run_id}", response_model=QuickstartDetailOut)
async def get_run(
    run_id: UUID,
    workspace_id: UUID = Depends(require_workspace_id),
    runs: QuickstartRepo = Depends(get_quickstart_repo),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
) -> QuickstartDetailOut:
    """A run with what it made so far: companies and their verdicts, leads and their emails, and the drafts."""
    run = await _run(runs, workspace_id, run_id)
    companies = await runs.companies(list(run.company_ids))
    leads = await runs.leads(list(run.lead_ids))
    by_lead = {lead.id: lead for lead in leads}
    posts = await runs.post_counts(list(run.lead_ids))

    def name(lead) -> str:
        return f"{lead.first_name} {lead.last_name}".strip() or lead.email or "Unnamed"

    drafts = [e for e in await runs.first_emails(list(run.lead_ids)) if e.lead_id in by_lead]
    return QuickstartDetailOut(
        **_out(run, await _name(lead_intel, run.icp_id)),
        companies=[
            QuickstartCompanyOut(id=c.id, name=c.name, domain=c.domain, status=c.status, reason=c.status_reason, score=c.score)
            for c in companies
        ],
        leads=[
            QuickstartLeadOut(
                id=lead.id, name=name(lead), title=lead.title, company=lead.company, email=lead.email,
                email_status=lead.email_status, posts=posts.get(lead.id, 0), status=lead.status,
            )
            for lead in leads
        ],
        drafts=[
            QuickstartDraftOut(
                id=e.id, lead_id=e.lead_id, lead_name=name(by_lead[e.lead_id]), company=by_lead[e.lead_id].company,
                email=by_lead[e.lead_id].email, email_status=by_lead[e.lead_id].email_status, subject=e.subject,
                body=e.edited_body or e.body, status=e.status, score=by_lead[e.lead_id].score, flags=list(e.flags),
            )
            for e in drafts
        ],
    )  # fmt: skip


@router.post("/runs/{run_id}/stop", response_model=QuickstartRunOut, dependencies=[Depends(require_manager)])
async def stop_run(
    run_id: UUID,
    workspace_id: UUID = Depends(require_workspace_id),
    runs: QuickstartRepo = Depends(get_quickstart_repo),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
) -> QuickstartRunOut:
    """Stop after the batch it's on. What it made so far stays."""
    run = await _run(runs, workspace_id, run_id)
    if run.status in ("queued", "running", "waiting"):
        run.status, run.detail, run.finished_at = "cancelled", "Stopped. What it made so far is below.", datetime.now(UTC)
        await runs.save(run)
    return QuickstartRunOut(**_out(run, await _name(lead_intel, run.icp_id)))


@router.post("/runs/{run_id}/resume", response_model=QuickstartRunOut, dependencies=[Depends(require_manager)])
async def resume_run(
    run_id: UUID,
    workspace_id: UUID = Depends(require_workspace_id),
    runs: QuickstartRepo = Depends(get_quickstart_repo),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    queue: Callable[[UUID], None] = Depends(get_quickstart_queue),
) -> QuickstartRunOut:
    """Pick a run up again: after the worker restarted mid-step, or after a step failed (it's tried again, and
    nothing before it is redone or paid for twice)."""
    run = await _run(runs, workspace_id, run_id)
    if run.status not in ("queued", "running", "failed", "waiting"):
        raise HTTPException(status_code=409, detail="This run has finished.")
    if run.busy_until and run.busy_until > datetime.now(UTC):
        raise HTTPException(status_code=409, detail="It's still working on a step.")
    if run.status in ("failed", "waiting"):
        progress = dict(run.progress)
        note = "Trying again" if run.status == "failed" else "Continuing"
        progress[run.step] = {**progress.get(run.step, {}), "state": "waiting", "note": note}
        run.progress, run.status, run.detail, run.finished_at = progress, "running", None, None
        await runs.save(run)
    try:
        queue(run.id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=NO_QUEUE) from exc
    return QuickstartRunOut(**_out(run, await _name(lead_intel, run.icp_id)))
