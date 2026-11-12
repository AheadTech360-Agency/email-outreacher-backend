import csv
import io
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from crud import ExclusionRepo, LeadFilters, LeadRepo, exclusion_note, person_keys
from dependencies import get_exclusion_repo, get_lead_intel_repo, get_lead_repo, require_manager, require_workspace_id
from lead_intel import LeadIntelRepo
from schemas import (
    ColumnMapping, ImportResult, LeadBriefIn, LeadBriefOut, LeadIdsIn, LeadIn, LeadMoveIn, LeadOut, LeadsChanged, LeadsPageOut,
    LeadSummaryOut, Page, RowOutcome,
)  # fmt: skip
from services.leads import (
    TEMPLATE_ROWS,
    CsvError,
    Existing,
    clean_lead,
    duplicate_reason,
    parse_csv,
    remember,
    rows_to_leads,
    validate,
)


router = APIRouter(prefix="/leads", tags=["leads"])

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
LEAD_STATUSES = ("imported", "enriching", "enriched", "no_content", "generated", "approved", "sent", "skipped", "failed")


async def _check_targets(workspace_id: UUID, icp_id: UUID | None, leads: LeadRepo, lead_intel: LeadIntelRepo) -> None:
    if not await leads.workspace_exists(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    if icp_id and not await lead_intel.get_icp(icp_id):
        raise HTTPException(status_code=404, detail="ICP not found")


@router.get("/template.csv", response_class=Response)
def download_template() -> Response:
    """The CSV layout the import reads best: one header row, one example lead."""
    out = io.StringIO()
    csv.writer(out).writerows(TEMPLATE_ROWS)
    return Response(
        content=out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="leads-template.csv"'},
    )


@router.get("", response_model=Page[LeadOut])
async def list_leads(
    workspace_id: UUID = Depends(require_workspace_id),
    icp_id: UUID | None = Query(default=None, alias="icpId"),
    status: str | None = Query(default=None, pattern=f"^({'|'.join(LEAD_STATUSES)})$"),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    leads: LeadRepo = Depends(get_lead_repo),
) -> Page[LeadOut]:
    rows, total = await leads.list(workspace_id, icp_id, status, q, limit, offset)
    posts = await leads.post_counts([r.id for r in rows])
    queued = await leads.queued_at([r.id for r in rows])
    items = [LeadOut.model_validate(r).model_copy(update={"post_count": posts.get(r.id, 0), "queued_at": queued.get(r.id)}) for r in rows]
    return Page[LeadOut](items=items, total=total, limit=limit, offset=offset)


PRESENCE = "^(all|yes|no)$"
MATCHING_CAP = 20000


def lead_filters(
    icp_id: UUID | None = Query(default=None, alias="icpId"),
    status: str | None = Query(default=None, pattern=f"^(all|{'|'.join(LEAD_STATUSES)})$"),
    source: str | None = Query(default=None, pattern="^(all|linkedin|instagram|facebook|website|none)$"),
    email: str | None = Query(default=None, pattern="^(all|has|verified|none)$"),
    linkedin: str | None = Query(default=None, pattern=PRESENCE),
    posts: str | None = Query(default=None, pattern=PRESENCE),
    score: str | None = Query(default=None, pattern="^(all|low|medium|high|unscored)$"),
    queued_within: str | None = Query(default=None, alias="queuedWithin", pattern="^(all|7|30|90)$"),
    q: str | None = Query(default=None, max_length=200),
) -> LeadFilters:
    """The Leads pages list only leads whose first email was queued (they count as leads from then on)."""
    return LeadFilters(True, icp_id, status, source, email, linkedin, posts, score, queued_within, q)


@router.get("/page", response_model=LeadsPageOut)
async def page_leads(
    f: LeadFilters = Depends(lead_filters),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    workspace_id: UUID = Depends(require_workspace_id),
    leads: LeadRepo = Depends(get_lead_repo),
) -> LeadsPageOut:
    """One page of the leads in sequence, filtered in the database (only the rows shown are read)."""
    rows, total = await leads.page(workspace_id, f, limit, offset, datetime.now(UTC))
    posts = await leads.post_counts([r.id for r in rows])
    queued = await leads.queued_at([r.id for r in rows])
    items = [LeadOut.model_validate(r).model_copy(update={"post_count": posts.get(r.id, 0), "queued_at": queued.get(r.id)}) for r in rows]
    queued_total, no_email, statuses = await leads.queued_counts_by(workspace_id, f.icp_id)
    return LeadsPageOut(items=items, total=total, limit=limit, offset=offset, queued_total=queued_total, no_email=no_email, statuses=statuses)


@router.get("/matching", response_model=list[LeadBriefOut])
async def matching_leads(
    f: LeadFilters = Depends(lead_filters),
    workspace_id: UUID = Depends(require_workspace_id),
    leads: LeadRepo = Depends(get_lead_repo),
) -> list[LeadBriefOut]:
    """Every lead in sequence matching these filters, as the few fields selecting needs (Select all)."""
    return [LeadBriefOut.model_validate(r) for r in await leads.matching(workspace_id, f, datetime.now(UTC), MATCHING_CAP)]


@router.post("/brief", response_model=list[LeadBriefOut])
async def brief_leads(
    body: LeadBriefIn, workspace_id: UUID = Depends(require_workspace_id), leads: LeadRepo = Depends(get_lead_repo)
) -> list[LeadBriefOut]:
    """These leads, as the few fields the Fetch posts and Write emails dialogs read."""
    return [LeadBriefOut.model_validate(r) for r in await leads.brief(workspace_id, body.ids)] if body.ids else []


@router.get("/summary", response_model=list[LeadSummaryOut])
async def leads_summary(
    workspace_id: UUID = Depends(require_workspace_id), leads: LeadRepo = Depends(get_lead_repo)
) -> list[LeadSummaryOut]:
    """Each ICP's leads per status (every lead), for the ICP pages' pipelines and counts."""
    by_icp: dict[UUID | None, dict[str, int]] = {}
    for icp_id, status, n in await leads.summary(workspace_id):
        by_icp.setdefault(icp_id, {})[status] = n
    return [LeadSummaryOut(icp_id=k, statuses=v) for k, v in by_icp.items()]


@router.get("/extra-keys", response_model=list[str])
async def lead_extra_keys(workspace_id: UUID = Depends(require_workspace_id), leads: LeadRepo = Depends(get_lead_repo)) -> list[str]:
    """Every column name in the leads' extra fields, for prompt variables."""
    return await leads.extra_keys(workspace_id)


@router.post("/delete", response_model=LeadsChanged, dependencies=[Depends(require_manager)])
async def delete_leads(
    body: LeadIdsIn,
    workspace_id: UUID = Depends(require_workspace_id),
    leads: LeadRepo = Depends(get_lead_repo),
) -> LeadsChanged:
    """Delete leads with their posts, hooks, drafts and ratings. The people they came from stay."""
    return LeadsChanged(changed=await leads.delete_many(workspace_id, list(dict.fromkeys(body.lead_ids))))


@router.post("/move", response_model=LeadsChanged, dependencies=[Depends(require_manager)])
async def move_leads(
    body: LeadMoveIn,
    workspace_id: UUID = Depends(require_workspace_id),
    leads: LeadRepo = Depends(get_lead_repo),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
) -> LeadsChanged:
    """Move leads to another ICP. New drafts use that ICP's prompts and messaging."""
    await _check_targets(workspace_id, body.icp_id, leads, lead_intel)
    return LeadsChanged(changed=await leads.move_many(workspace_id, list(dict.fromkeys(body.lead_ids)), body.icp_id))


@router.post("", response_model=LeadOut, status_code=201)
async def add_lead(
    body: LeadIn,
    workspace_id: UUID = Depends(require_workspace_id),
    leads: LeadRepo = Depends(get_lead_repo),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    exclusions: ExclusionRepo = Depends(get_exclusion_repo),
) -> LeadOut:
    """Add one lead by hand. 409 if the workspace already has them or they're on the do-not-contact list."""
    await _check_targets(workspace_id, body.icp_id, leads, lead_intel)
    lead = clean_lead(body.model_dump(), body.extra)
    if problem := validate(lead):
        raise HTTPException(status_code=422, detail=problem)
    if hit := await exclusions.match(person_keys(lead.email, lead.domain)):
        raise HTTPException(status_code=409, detail=exclusion_note(hit))
    if reason := duplicate_reason(lead, await leads.find_existing(workspace_id, [lead])):
        raise HTTPException(status_code=409, detail=f"Already a lead in this workspace ({reason})")
    [lead_id] = await leads.insert_many(workspace_id, body.icp_id, "manual", [lead])
    if lead_id is None:
        raise HTTPException(status_code=409, detail="Already a lead in this workspace")
    return LeadOut.model_validate(await leads.get(workspace_id, lead_id))


@router.post("/import", response_model=ImportResult)
async def import_csv(
    request: Request,
    workspace_id: UUID = Depends(require_workspace_id),
    icp_id: UUID | None = Query(default=None, alias="icpId"),
    dry_run: bool = Query(default=False, alias="dryRun"),
    leads: LeadRepo = Depends(get_lead_repo),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    exclusions: ExclusionRepo = Depends(get_exclusion_repo),
) -> ImportResult:
    """Import leads from a CSV sent as the request body (Content-Type: text/csv).

    Every row gets an outcome: imported, duplicate (in the file or already in the workspace) or
    invalid (including anyone on the do-not-contact list). With dryRun=true nothing is saved, so the result can be shown as a preview first.
    """
    await _check_targets(workspace_id, icp_id, leads, lead_intel)
    data = await request.body()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="The file is over 5 MB. Split it and import the parts.")
    try:
        headers, rows = parse_csv(data)
    except CsvError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    mapping, candidates = rows_to_leads(headers, rows)
    existing = await leads.find_existing(workspace_id, candidates)
    is_excluded = await exclusions.matcher()
    seen = Existing()
    outcomes: list[RowOutcome] = []
    to_insert = []
    counts = {"in_file": 0, "existing": 0, "invalid": 0}

    for index, lead in enumerate(candidates):
        row = index + 2
        if problem := validate(lead):
            counts["invalid"] += 1
            outcomes.append(RowOutcome(row=row, outcome="invalid", reason=problem))
        elif hit := is_excluded(person_keys(lead.email, lead.domain)):
            counts["invalid"] += 1
            outcomes.append(RowOutcome(row=row, outcome="invalid", reason=exclusion_note(hit)))
        elif reason := duplicate_reason(lead, existing):
            counts["existing"] += 1
            outcomes.append(RowOutcome(row=row, outcome="duplicate", reason=f"Already in this workspace ({reason})"))
        elif reason := duplicate_reason(lead, seen):
            counts["in_file"] += 1
            outcomes.append(RowOutcome(row=row, outcome="duplicate", reason=f"Repeated in this file ({reason})"))
        else:
            remember(lead, seen)
            outcomes.append(RowOutcome(row=row, outcome="imported"))
            to_insert.append((len(outcomes) - 1, lead))

    if not dry_run and to_insert:
        ids = await leads.insert_many(workspace_id, icp_id, "csv", [lead for _, lead in to_insert])
        for (position, _), lead_id in zip(to_insert, ids, strict=True):
            if lead_id is None:
                # Added by someone else since the check above; the database kept the first one.
                counts["existing"] += 1
                outcomes[position] = RowOutcome(
                    row=outcomes[position].row, outcome="duplicate", reason="Already in this workspace"
                )
            else:
                outcomes[position].lead_id = lead_id

    return ImportResult(
        dry_run=dry_run,
        total_rows=len(candidates),
        imported=sum(o.outcome == "imported" for o in outcomes),
        duplicates_in_file=counts["in_file"],
        duplicates_existing=counts["existing"],
        invalid=counts["invalid"],
        websites_from_email=sum(lead.website_derived for _, lead in to_insert),
        columns=[ColumnMapping(column=h, field=f) for h, f in zip(headers, mapping, strict=True)],
        rows=outcomes,
    )
