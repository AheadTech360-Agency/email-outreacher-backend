"""Find the people at a workspace's companies (Apollo search, free) and reveal the ones worth emailing
(Apollo enrichment, paid). Revealing a person turns them into a lead.

People can also be added by hand or from a CSV in Apollo's export format (template.csv). Anyone with
an email or a LinkedIn URL becomes a lead straight away; each is linked to the pipeline company with
the same domain (or the same name).

Lead Intel is only read here: for each company's name, domain and ICP.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

import anthropic

from background import report
from crud import (
    CompanyRepo, ExclusionRepo, IcpProfileRepo, PeopleFilters, PostRepo, PromptRepo, ProviderCallRepo, SmartListRepo, StakeholderRepo,
    WorkspaceCompanyRepo,
    lead_from_stakeholder,
)  # fmt: skip
from database import get_session
from dependencies import (
    MANAGERS_ONLY,
    get_apify,
    get_apollo,
    get_company_repo,
    get_current_user,
    get_exclusion_repo,
    get_icp_profile_repo,
    get_lead_intel_repo,
    get_optional_anthropic,
    get_optional_apify,
    get_post_repo,
    get_prompt_repo,
    get_smart_list_repo,
    get_website_reader,
    get_provider_call_repo,
    get_stakeholder_repo,
    get_workspace_company_repo,
    require_manager,
    require_workspace_id,
)
from lead_intel import LeadIntelRepo
from routers.pipeline import apify_recorder, enrich_companies_now
from routers.smart_lists import recorder, sort_context, sort_out
from services import enrich, exports, record_data
from services.auth import CurrentUser
from schemas import (
    EnrichPeopleIn,
    EnrichResult,
    ImportRunIn,
    ImportRunResult,
    AddAsLeadsIn,
    AddAsLeadsResult,
    CompanySearchResult,
    LinkedInSearchIn,
    PeopleImportResult,
    PeopleRowError,
    PersonAdded,
    PersonIn,
    IcpSearchIn,
    IcpSearchResult,
    RevealOutcome,
    StakeholderDetailOut,
    StakeholderOut,
    PeoplePageOut,
    PersonBriefOut,
    PersonIdsIn,
    PersonRowOut,
    StakeholderRevealIn,
    StakeholderRevealResult,
    StakeholderSearchIn,
    StakeholderSearchResult,
)
from services.apify import EMPLOYEES_ACTOR, ApifyClient, ApifyError, elsewhere
from services.apollo import SENIORITIES, ApolloClient, ApolloError
from services import smart_lists
from services.leads import CsvError, clean_lead
from services.website import WebsiteReader
from services.people_csv import (
    PersonRow,
    clean_person,
    company_domain,
    parse_people_csv,
    template_csv,
)

router = APIRouter(prefix="/stakeholders", tags=["stakeholders"])

# Refusals that will repeat for every other company too, so there's no point carrying on.
STOP_STATUSES = {401, 403, 429}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
# What a lead made from a person records as its origin.
LEAD_ORIGIN = {"apollo": "apollo", "apify_linkedin": "apify", "manual": "manual"}


@router.get("/template.csv", response_class=Response)
def people_template() -> Response:
    """The people CSV template: the columns of Apollo's people export that we use, with one example."""
    return Response(
        content=template_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="people-template.csv"'},
    )


async def _companies_of(lead_intel: LeadIntelRepo, companies: CompanyRepo, workspace_id: UUID, people) -> dict[UUID, dict]:
    """Each person's company as {display_name, domain, icp_id}: from Lead Intel, or else from the pipeline
    company they were found at (Google Maps and Apollo companies aren't in Lead Intel)."""
    li_ids = list({s.lead_intel_business_id for s in people if s.lead_intel_business_id})
    rows, _ = await lead_intel.list_companies(None, None, max(len(li_ids), 1), 0, only_ids=li_ids)
    by_li = {c["id"]: c for c in rows}
    pipeline_ids = list({s.company_id for s in people if s.company_id and s.lead_intel_business_id not in by_li})
    by_pipeline = {c.id: c for c in await companies.get_many(workspace_id, pipeline_ids)} if pipeline_ids else {}
    found: dict[UUID, dict] = {}
    for s in people:
        if c := by_li.get(s.lead_intel_business_id):
            found[s.id] = c
        elif c := by_pipeline.get(s.company_id):
            found[s.id] = {"display_name": c.name, "domain": c.domain or c.website_url, "icp_id": c.icp_id}
    return found


async def _check_icp(lead_intel: LeadIntelRepo, icp_id: UUID | None) -> None:
    if icp_id and not await lead_intel.get_icp(icp_id):
        raise HTTPException(status_code=404, detail="ICP not found")


async def _add(
    session: AsyncSession, stakeholders: StakeholderRepo, workspace_id: UUID, icp_id: UUID | None, person: PersonRow,
    company, origin: str,
):  # fmt: skip
    """Save one person; with an email or LinkedIn URL, make them a lead too. Returns (row, new, lead created)."""
    row, new = await stakeholders.save_person(workspace_id, icp_id, person, company, origin)
    await stakeholders.commit()
    lead_created = False
    if not row.lead_id and (person.email or person.linkedin_url):
        lead = clean_lead({
            "first_name": person.first_name, "last_name": person.last_name, "email": person.email,
            "title": person.title, "company": row.organization_name or person.company,
            "website": person.website or (company.website_url if company else "") or "", "linkedin_url": person.linkedin_url,
        })  # fmt: skip
        lead_id, lead_created = await lead_from_stakeholder(
            session, workspace_id, row.lead_intel_icp_id, row.lead_intel_business_id, lead, row.external_id,
            row.email_status, origin=origin,
        )  # fmt: skip
        if lead_id:
            await stakeholders.link_lead(row, lead_id)
    return row, new, lead_created


@router.post("", response_model=PersonAdded, status_code=201)
async def add_person(
    body: PersonIn,
    workspace_id: UUID = Depends(require_workspace_id),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
    companies: CompanyRepo = Depends(get_company_repo),
    session: AsyncSession = Depends(get_session),
) -> PersonAdded:
    """Add one person by hand. Adding someone already here updates them instead."""
    if not await companies.workspace_exists(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    await _check_icp(lead_intel, body.icp_id)
    person, problem = clean_person(body.model_dump(exclude={"icp_id"}))
    if person is None:
        raise HTTPException(status_code=422, detail=problem)
    find = await companies.matcher(workspace_id)
    company = find(company_domain(person), person.company)
    row, new, lead_created = await _add(session, stakeholders, workspace_id, body.icp_id, person, company, "manual")
    return PersonAdded(
        person=StakeholderOut.model_validate(row), new=new, lead_created=lead_created, company_name=company.name if company else None
    )


@router.post("/import", response_model=PeopleImportResult)
async def import_people(
    request: Request,
    workspace_id: UUID = Depends(require_workspace_id),
    icp_id: UUID | None = Query(default=None, alias="icpId"),
    dry_run: bool = Query(default=False, alias="dryRun"),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
    companies: CompanyRepo = Depends(get_company_repo),
    session: AsyncSession = Depends(get_session),
) -> PeopleImportResult:
    """Import people from a CSV sent as the body (Content-Type: text/csv), in Apollo's export format.

    With dryRun=true nothing is saved: the result previews what would happen. People already here
    (same Apollo id, LinkedIn URL or email) are updated, not added twice.
    """
    if not await companies.workspace_exists(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    await _check_icp(lead_intel, icp_id)
    data = await request.body()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="The file is over 5 MB. Split it and import the parts.")
    try:
        parsed = parse_people_csv(data.decode("utf-8-sig", errors="replace"))
    except CsvError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    find = await companies.matcher(workspace_id)
    add_company = await companies.csv_company_adder(workspace_id, icp_id)
    added = updated = leads_created = linked = 0
    for _, person in parsed.people:
        company = find(company_domain(person), person.company)
        linked += company is not None
        if dry_run:
            continue
        if company is None:
            # From an Apollo export: the company comes too (Companies page, smart lists), saved with the person.
            company = add_company(person, company_domain(person))
            if company is not None:
                await session.flush()
        _, new, lead_created = await _add(session, stakeholders, workspace_id, icp_id, person, company, "csv")
        added += new
        updated += not new
        leads_created += lead_created
    return PeopleImportResult(
        dry_run=dry_run,
        total_rows=len(parsed.people) + len(parsed.errors) + parsed.duplicates,
        valid=len(parsed.people),
        added=added,
        updated=updated,
        leads_created=leads_created,
        linked_to_companies=linked,
        duplicates_in_file=parsed.duplicates,
        errors=[PeopleRowError(row=r, reason=reason) for r, reason in parsed.errors[:200]],
        columns=parsed.columns,
    )


@router.post("/enrich", response_model=EnrichResult, dependencies=[Depends(require_manager)])
async def enrich_people(
    body: EnrichPeopleIn,
    workspace_id: UUID = Depends(require_workspace_id),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    apify: ApifyClient = Depends(get_apify),
) -> EnrichResult:
    """Their full LinkedIn profiles (about, experience, education, skills), $0.004 each, logged on Usage. Written into
    their records, where email writing reads them. A profile already read isn't bought again."""
    people = await stakeholders.get_many(workspace_id, list(dict.fromkeys(body.stakeholder_ids)))
    out = await enrich.enrich_people(people, apify=apify, record=apify_recorder(calls, workspace_id, "Enrich people"), progress=report)
    await stakeholders.commit()
    return EnrichResult(**vars(out))


IMPORT_BATCH = 100  # people saved per round of database writes


@router.post("/import-run", response_model=ImportRunResult)
async def import_run(
    body: ImportRunIn,
    workspace_id: UUID = Depends(require_workspace_id),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
    companies: CompanyRepo = Depends(get_company_repo),
    exclusions: ExclusionRepo = Depends(get_exclusion_repo),
    lists: SmartListRepo = Depends(get_smart_list_repo),
    prompts: PromptRepo = Depends(get_prompt_repo),
    profiles: IcpProfileRepo = Depends(get_icp_profile_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    claude: anthropic.AsyncAnthropic | None = Depends(get_optional_anthropic),
    reader: WebsiteReader = Depends(get_website_reader),
    posts: PostRepo = Depends(get_post_repo),
    apify: ApifyClient | None = Depends(get_optional_apify),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> ImportRunResult:
    """Import a people CSV (Apollo's export format) as a background job, showing its progress: people saved in
    batches (with their leads, and their companies from an Apollo export), then, with `sort`, what it brought in
    sorted into smart lists and ICPs by Claude. Importing is free; sorting spends a little, logged on Usage."""
    if not await companies.workspace_exists(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    await _check_icp(lead_intel, body.icp_id)
    if body.sort and user.role not in ("admin", "management"):
        raise HTTPException(status_code=403, detail=MANAGERS_ONLY)  # adding people is Sales' too; spending isn't
    if body.sort and claude is None:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not set, so it can't sort. Untick sorting to import only.")
    await report("Reading the file")
    try:
        parsed = parse_people_csv(body.csv)
    except CsvError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    blocked = {(e.key_type, e.key_value) for e in await exclusions.list()}
    find = await companies.matcher(workspace_id)
    add_company = await companies.csv_company_adder(workspace_id, body.icp_id)
    people = [person for _, person in parsed.people]
    added = updated = leads_created = 0
    new_companies: set[int] = set()  # by object: a company added earlier in the same batch has no id yet
    person_ids: list[UUID] = []
    company_ids: set[UUID] = set()
    enriched: list[EnrichResult] = []
    await report("Saving people", 0, len(people))
    for start in range(0, len(people), IMPORT_BATCH):
        items = []
        for person in people[start : start + IMPORT_BATCH]:
            company = find(company_domain(person), person.company)
            if company is None and (company := add_company(person, company_domain(person))) is not None and company.id is None:
                new_companies.add(id(company))
            items.append((person, company))
        await session.flush()  # new companies get their ids
        company_ids |= {c.id for _, c in items if c is not None}
        a, u, made, ids = await stakeholders.import_batch(workspace_id, body.icp_id, items, "csv", blocked)
        added, updated, leads_created = added + a, updated + u, leads_created + made
        person_ids += ids
        await report("Saving people", min(start + IMPORT_BATCH, len(people)), len(people))

    # Filling in, as opted into, before sorting: sorting then reads the websites too.
    opts = body.enrich
    if opts and (opts.website or opts.instagram or opts.linkedin or opts.people_linkedin):
        if user.role not in ("admin", "management"):
            raise HTTPException(status_code=403, detail=MANAGERS_ONLY)
        if opts.website or opts.instagram or opts.linkedin:
            rows = await companies.get_many(workspace_id, list(company_ids))
            done = await enrich_companies_now(
                workspace_id, rows, opts.website, opts.instagram, opts.linkedin, companies, posts, calls, reader, apify
            )  # fmt: skip
            enriched.append(done)
        if opts.people_linkedin:
            if apify is None:
                raise HTTPException(status_code=503, detail="APIFY_TOKEN is not set")
            found = await stakeholders.get_many(workspace_id, person_ids)
            done_people = await enrich.enrich_people(
                found, apify=apify, record=apify_recorder(calls, workspace_id, "Enrich people"), progress=report
            )  # fmt: skip
            await stakeholders.commit()
            enriched.append(EnrichResult(**vars(done_people)))

    result = ImportRunResult(
        total_rows=len(parsed.people) + len(parsed.errors) + parsed.duplicates, valid=len(parsed.people), added=added,
        updated=updated, leads_created=leads_created, companies_added=len(new_companies), duplicates_in_file=parsed.duplicates,
        errors=[PeopleRowError(row=r, reason=reason) for r, reason in parsed.errors[:200]],
    )  # fmt: skip
    if enriched:
        result.enriched = EnrichResult(**{
            k: (round(sum(getattr(e, k) for e in enriched), 4) if k == "cost_usd" else sum(getattr(e, k) for e in enriched))
            for k in ("sites_read", "sites_failed", "instagram", "linkedin_companies", "linkedin_people", "posts_saved", "cost_usd")
        }, errors=[x for e in enriched for x in e.errors])  # fmt: skip
        result.cost_usd = round(result.cost_usd + result.enriched.cost_usd, 6)
    if not body.sort:
        return result
    record = recorder(calls, workspace_id)
    for kind, ids in (("company", list(company_ids)), ("person", person_ids)):
        members = await lists.unsorted_among(workspace_id, kind, ids)
        if not members:
            continue
        spec, icps = await sort_context(workspace_id, kind, prompts, lead_intel, profiles)
        what = "companies" if kind == "company" else "people"

        async def progress(step: str, done: int | None, total: int | None, what: str = what) -> None:
            await report(f"{step} ({what})", done, total)

        outcome = await smart_lists.run_sort(
            workspace_id=workspace_id, kind=kind, members=members, repo=lists, claude=claude, spec=spec, icps=icps,
            reader=reader if kind == "company" else None, record=record, progress=progress,
        )  # fmt: skip
        out = sort_out(outcome, await lists.count_unsorted(workspace_id, kind))
        if kind == "company":
            result.companies_sorted = out
        else:
            result.people_sorted = out
        result.cost_usd = round(result.cost_usd + outcome.cost_usd, 6)
    return result


@router.get("", response_model=list[StakeholderOut])
async def list_stakeholders(
    company_id: UUID | None = Query(default=None, alias="companyId"),
    icp_id: UUID | None = Query(default=None, alias="icpId"),
    workspace_id: UUID = Depends(require_workspace_id),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
) -> list[StakeholderOut]:
    people = await stakeholders.list(workspace_id, company_id, icp_id, light=True)
    queued = await stakeholders.queued_at([s.lead_id for s in people if s.lead_id])
    return [StakeholderOut.model_validate(s).model_copy(update={"queued_at": queued.get(s.lead_id)}) for s in people]


PRESENCE = "^(all|yes|no)$"
MATCHING_CAP = 20000


def people_filters(
    archive: str = Query(default="active", pattern="^(active|archived|all)$"),
    q: str | None = Query(default=None, max_length=200),
    found_by: str | None = Query(default=None, alias="foundBy", pattern="^(all|company|linkedin|manual|[0-9a-fA-F-]{36})$"),
    status: str | None = Query(default=None, pattern="^(all|hidden|revealed|lead|notlead)$"),
    email: str | None = Query(default=None, pattern="^(all|has|verified|none)$"),
    linkedin: str | None = Query(default=None, pattern=PRESENCE),
    post_state: str | None = Query(default=None, alias="postState", pattern=PRESENCE),
    email_state: str | None = Query(default=None, alias="emailState", max_length=60),
    company_text: str | None = Query(default=None, alias="companyText", max_length=200),
    pipeline_company_id: UUID | None = Query(default=None, alias="pipelineCompanyId"),
) -> PeopleFilters:
    return PeopleFilters(archive, q, found_by, status, email, linkedin, post_state, email_state, company_text, pipeline_company_id)


@router.get("/page", response_model=PeoplePageOut)
async def page_people(
    f: PeopleFilters = Depends(people_filters),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    workspace_id: UUID = Depends(require_workspace_id),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
) -> PeoplePageOut:
    """One page of the People list, filtered in the database (only the rows shown are read)."""
    rows, total = await stakeholders.page(workspace_id, f, limit, offset)
    leads = [s.lead_id for s, _ in rows if s.lead_id]
    queued = await stakeholders.queued_at(leads)
    posts = await stakeholders.post_counts(leads)
    items = [
        PersonRowOut.model_validate(s).model_copy(update={
            "queued_at": queued.get(s.lead_id), "company_name": name, "post_count": posts.get(s.lead_id, 0) if s.lead_id else 0,
        })
        for s, name in rows
    ]  # fmt: skip
    return PeoplePageOut(items=items, total=total, limit=limit, offset=offset, email_statuses=await stakeholders.email_statuses(workspace_id, f))


@router.get("/matching", response_model=list[PersonBriefOut])
async def matching_people(
    f: PeopleFilters = Depends(people_filters),
    workspace_id: UUID = Depends(require_workspace_id),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
) -> list[PersonBriefOut]:
    """Every person matching these filters, in list order, as the few fields selecting needs (Select all)."""
    return [PersonBriefOut.model_validate(r) for r in await stakeholders.matching(workspace_id, f, MATCHING_CAP)]


@router.post("/lead-ids", response_model=dict[str, UUID | None])
async def people_lead_ids(
    body: PersonIdsIn,
    workspace_id: UUID = Depends(require_workspace_id),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
) -> dict[str, UUID | None]:
    """Each of these people's lead (after Add as leads), without reading the whole People list."""
    return {str(k): v for k, v in (await stakeholders.lead_ids(workspace_id, body.ids)).items()}


@router.get("/export.csv", response_class=Response)
async def export_people(
    workspace_id: UUID = Depends(require_workspace_id),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    lists: SmartListRepo = Depends(get_smart_list_repo),
) -> Response:
    """The People page as a CSV: who they are, their email, ICP, smart list, and whether they're a lead (queued)."""
    people = await stakeholders.list(workspace_id, None, None)
    queued = await stakeholders.queued_at([p.lead_id for p in people if p.lead_id])
    icps = {i["id"]: i["name"] for i in await lead_intel.list_icps()}
    smart = await lists.list_names(workspace_id, "person", [p.id for p in people])
    companies = await lists.companies(workspace_id, list({p.company_id for p in people if p.company_id}))
    body = exports.write(exports.PEOPLE_COLUMNS, [
        exports.person_row(
            p, (companies[p.company_id].name if p.company_id in companies else p.organization_name) or "",
            icps.get(p.lead_intel_icp_id, ""), smart.get(p.id, ""), queued.get(p.lead_id),
        )
        for p in people
    ])  # fmt: skip
    return Response(content=body, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="people.csv"'})


@router.get("/{stakeholder_id}", response_model=StakeholderDetailOut)
async def get_stakeholder(
    stakeholder_id: UUID,
    workspace_id: UUID = Depends(require_workspace_id),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
) -> StakeholderOut:
    """One person, for their own page."""
    [person] = await stakeholders.get_many(workspace_id, [stakeholder_id]) or [None]
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    queued = await stakeholders.queued_at([person.lead_id] if person.lead_id else [])
    return StakeholderDetailOut.model_validate(person).model_copy(
        update={"queued_at": queued.get(person.lead_id), "data": record_data.person_groups(person.raw)}
    )


@router.post("/search-linkedin", response_model=StakeholderSearchResult, dependencies=[Depends(require_manager)])
async def search_linkedin(
    body: LinkedInSearchIn,
    workspace_id: UUID = Depends(require_workspace_id),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    workspace_companies: WorkspaceCompanyRepo = Depends(get_workspace_company_repo),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    apify: ApifyClient = Depends(get_apify),
) -> StakeholderSearchResult:
    """Find people at each company on LinkedIn, through Apify. Full names and LinkedIn URLs, no emails.

    Paid per profile found (about $0.004 each). Companies are looked up on LinkedIn by name, since
    Lead Intel has no LinkedIn company URL; check the results look like the right company.
    """
    if not await workspace_companies.workspace_exists(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    in_workspace = await workspace_companies.imported(workspace_id)
    ids = list(dict.fromkeys(body.company_ids))
    companies, _ = await lead_intel.list_companies(None, None, len(ids), 0, only_ids=ids)
    by_id = {c["id"]: c for c in companies}
    titles = [t.strip() for t in body.titles if t.strip()]

    results: list[CompanySearchResult] = []
    error: str | None = None
    total_cost = 0.0
    for company_id in ids:
        company = by_id.get(company_id)
        name = company["display_name"] if company else None
        if error:
            results.append(CompanySearchResult(company_id=company_id, name=name, status="failed", reason="Not searched"))
            continue
        if company_id not in in_workspace or not company:
            reason = "Not in this workspace" if company else "Not found in Lead Intel, or no longer contactable"
            results.append(CompanySearchResult(company_id=company_id, name=name, status="skipped", reason=reason))
            continue
        try:
            people, run = await apify.company_employees(
                company["display_name"], titles, body.max_per_company, body.find_emails, domain=company.get("domain")
            )
        except ApifyError as exc:
            await calls.record(workspace_id, "apify", EMPLOYEES_ACTOR, "failed", action="Find stakeholders")
            results.append(CompanySearchResult(company_id=company_id, name=name, status="failed", reason=exc.message))
            if exc.status in STOP_STATUSES | {402}:
                error = exc.message
            continue
        total_cost += run.cost_usd or 0
        await calls.record(
            workspace_id, "apify", EMPLOYEES_ACTOR, "succeeded",
            key=f"apify:run:{run.run_id}" if run.run_id else None, cost_usd=run.cost_usd, action="Find stakeholders",
        )  # fmt: skip
        new = await stakeholders.save_employees(workspace_id, company_id, people)
        results.append(CompanySearchResult(company_id=company_id, name=name, status="searched", found=len(people), new=new,
                                           reason=elsewhere(run.skipped_elsewhere)))  # fmt: skip
    return StakeholderSearchResult(results=results, error=error, cost_usd=round(total_cost, 4))


@router.post("/add-as-leads", response_model=AddAsLeadsResult)
async def add_as_leads(
    body: AddAsLeadsIn,
    workspace_id: UUID = Depends(require_workspace_id),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
    companies: CompanyRepo = Depends(get_company_repo),
    session: AsyncSession = Depends(get_session),
) -> AddAsLeadsResult:
    """Make leads from people you already have a LinkedIn URL or an email for. Free: no provider calls.

    A lead with only a LinkedIn URL can have its posts fetched now and its email revealed later.
    """
    ids = list(dict.fromkeys(body.stakeholder_ids))
    found = await stakeholders.get_many(workspace_id, ids)
    by_person = await _companies_of(lead_intel, companies, workspace_id, found)
    added = linked = skipped = 0
    for s in found:
        # Unrevealed Apollo people only have a masked name and no contact; reveal them instead.
        if s.lead_id or not (s.linkedin_url or s.email) or (s.source == "apollo" and not s.revealed_at):
            skipped += 1
            continue
        company = by_person.get(s.id) or {}
        lead = clean_lead(
            {
                "first_name": s.first_name,
                "last_name": s.last_name,
                "email": s.email or "",
                "title": s.title,
                "company": company.get("display_name") or s.organization_name or "",
                "website": company.get("domain") or "",
                "linkedin_url": s.linkedin_url or "",
            }
        )
        lead_id, created = await lead_from_stakeholder(
            session, workspace_id, company.get("icp_id") or s.lead_intel_icp_id, s.lead_intel_business_id, lead,
            s.external_id, s.email_status, origin=LEAD_ORIGIN.get(s.source, "apollo"),
        )  # fmt: skip
        if lead_id is None:
            skipped += 1
            continue
        await stakeholders.link_lead(s, lead_id)
        added += created
        linked += not created
    skipped += len(ids) - len(found)
    return AddAsLeadsResult(added=added, linked_existing=linked, skipped=skipped)


@router.post("/search-icp", response_model=IcpSearchResult, dependencies=[Depends(require_manager)])
async def search_by_icp(
    body: IcpSearchIn,
    workspace_id: UUID = Depends(require_workspace_id),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    workspace_companies: WorkspaceCompanyRepo = Depends(get_workspace_company_repo),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    apollo: ApolloClient = Depends(get_apollo),
) -> IcpSearchResult:
    """Search Apollo for people who fit an ICP: its titles, places, size and keywords. Uses no credits.

    One page of results per call; ask for the next page to see more. Lead Intel is only read, to
    check the ICP exists.
    """
    if bad := [s for s in body.seniorities if s not in SENIORITIES]:
        raise HTTPException(status_code=422, detail=f"Unknown seniorities: {', '.join(bad)}")
    if bad := [r for r in body.employee_ranges if not _is_range(r)]:
        raise HTTPException(status_code=422, detail=f"Employee ranges must look like 1,10: {', '.join(bad)}")
    if not await workspace_companies.workspace_exists(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    if not await lead_intel.get_icp(body.icp_id):
        raise HTTPException(status_code=404, detail="ICP not found")
    titles = [t.strip() for t in body.titles if t.strip()]
    locations = [loc.strip() for loc in body.locations if loc.strip()]
    if not (titles or body.seniorities or locations or body.keywords.strip() or body.employee_ranges):
        raise HTTPException(status_code=422, detail="Add at least one filter, or the search would match everyone")

    try:
        people, total = await apollo.search_people(
            None,
            titles,
            body.seniorities,
            body.per_page,
            page=body.page,
            organization_locations=locations,
            keywords=body.keywords.strip() or None,
            employee_ranges=body.employee_ranges,
            email_statuses=["verified"] if body.verified_email_only else None,
        )
    except ApolloError as exc:
        await calls.record(workspace_id, "apollo", "mixed_people/api_search", "failed", action="Search people by ICP")
        return IcpSearchResult(found=0, new=0, total_at_apollo=None, page=body.page, error=exc.message)
    await calls.record(workspace_id, "apollo", "mixed_people/api_search", "succeeded", credits=0, action="Search people by ICP")
    new = await stakeholders.save_found(workspace_id, None, people, icp_id=body.icp_id)
    return IcpSearchResult(found=len(people), new=new, total_at_apollo=total, page=body.page)


def _is_range(value: str) -> bool:
    parts = value.split(",")
    return len(parts) == 2 and all(p.strip().isdigit() for p in parts) and int(parts[0]) <= int(parts[1])


@router.post("/search", response_model=StakeholderSearchResult, dependencies=[Depends(require_manager)])
async def search(
    body: StakeholderSearchIn,
    workspace_id: UUID = Depends(require_workspace_id),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    workspace_companies: WorkspaceCompanyRepo = Depends(get_workspace_company_repo),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    apollo: ApolloClient = Depends(get_apollo),
) -> StakeholderSearchResult:
    """Search Apollo for people at each company, by title and seniority. Uses no Apollo credits."""
    if bad := [s for s in body.seniorities if s not in SENIORITIES]:
        raise HTTPException(status_code=422, detail=f"Unknown seniorities: {', '.join(bad)}")
    if not await workspace_companies.workspace_exists(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    in_workspace = await workspace_companies.imported(workspace_id)
    ids = list(dict.fromkeys(body.company_ids))
    companies, _ = await lead_intel.list_companies(None, None, len(ids), 0, only_ids=ids)
    by_id = {c["id"]: c for c in companies}
    titles = [t.strip() for t in body.titles if t.strip()]

    results: list[CompanySearchResult] = []
    error: str | None = None
    for company_id in ids:
        company = by_id.get(company_id)
        name = company["display_name"] if company else None
        if error:
            results.append(CompanySearchResult(company_id=company_id, name=name, status="failed", reason="Not searched"))
            continue
        if company_id not in in_workspace or not company:
            reason = "Not in this workspace" if company else "Not found in Lead Intel, or no longer contactable"
            results.append(CompanySearchResult(company_id=company_id, name=name, status="skipped", reason=reason))
            continue
        if not company["domain"]:
            results.append(
                CompanySearchResult(
                    company_id=company_id, name=name, status="skipped", reason="No website domain in Lead Intel to search by"
                )
            )
            continue
        try:
            people, total = await apollo.search_people(company["domain"], titles, body.seniorities)
        except ApolloError as exc:
            await calls.record(workspace_id, "apollo", "mixed_people/api_search", "failed", action="Find stakeholders")
            results.append(CompanySearchResult(company_id=company_id, name=name, status="failed", reason=exc.message))
            if exc.status in STOP_STATUSES:
                error = exc.message
            continue
        await calls.record(workspace_id, "apollo", "mixed_people/api_search", "succeeded", credits=0, action="Find stakeholders")
        new = await stakeholders.save_found(workspace_id, company_id, people)
        results.append(
            CompanySearchResult(
                company_id=company_id, name=name, status="searched", found=len(people), new=new, total_at_apollo=total
            )
        )
    return StakeholderSearchResult(results=results, error=error)


@router.post("/reveal", response_model=StakeholderRevealResult, dependencies=[Depends(require_manager)])
async def reveal(
    body: StakeholderRevealIn,
    workspace_id: UUID = Depends(require_workspace_id),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    companies: CompanyRepo = Depends(get_company_repo),
    session: AsyncSession = Depends(get_session),
    apollo: ApolloClient = Depends(get_apollo),
) -> StakeholderRevealResult:
    """Reveal each person's full name, work email and LinkedIn, and add them as a lead.

    Costs Apollo credits (about 1 per person with data; nothing when Apollo finds no match).
    People already revealed are skipped, so they're never paid for twice.
    """
    ids = list(dict.fromkeys(body.stakeholder_ids))
    found = {s.id: s for s in await stakeholders.get_many(workspace_id, ids)}
    by_person = await _companies_of(lead_intel, companies, workspace_id, list(found.values()))

    outcomes: list[RevealOutcome] = []
    error: str | None = None
    for sid in ids:
        s = found.get(sid)
        if s is None:
            outcomes.append(RevealOutcome(stakeholder_id=sid, status="not_found"))
            continue
        if s.revealed_at is not None:
            outcomes.append(
                RevealOutcome(stakeholder_id=sid, status="already_revealed", email=s.email, lead_id=s.lead_id)
            )
            continue
        if error:
            outcomes.append(RevealOutcome(stakeholder_id=sid, status="failed", reason="Not revealed"))
            continue
        try:
            # People found on LinkedIn have no Apollo id; Apollo matches them by LinkedIn URL.
            # Only Apollo people have an Apollo id; the rest are looked up by their LinkedIn URL.
            if s.source != "apollo":
                person = await apollo.reveal(linkedin_url=s.linkedin_url) if s.linkedin_url else None
            else:
                person = await apollo.reveal(s.external_id)
        except ApolloError as exc:
            await calls.record(workspace_id, "apollo", "people/match", "failed", action="Reveal email")
            outcomes.append(RevealOutcome(stakeholder_id=sid, status="failed", reason=exc.message))
            if exc.status in STOP_STATUSES:
                error = exc.message
            continue

        if person is None:
            await calls.record(workspace_id, "apollo", "people/match", "succeeded", credits=0, action="Reveal email")
            await stakeholders.save_revealed(s, None, None)
            outcomes.append(RevealOutcome(stakeholder_id=sid, status="no_match", reason="Apollo had no match"))
            continue

        # People found by ICP have no Lead Intel company; Apollo's own company data stands in.
        company = by_person.get(s.id) or {}
        lead = clean_lead(
            {
                "first_name": person.first_name,
                "last_name": person.last_name,
                "email": person.email or "",
                "title": person.title,
                "company": company.get("display_name") or person.organization_name or "",
                "website": company.get("domain") or person.organization_domain or "",
                "linkedin_url": person.linkedin_url or "",
            }
        )
        lead_id, created = (None, False)
        if lead.email or lead.linkedin_url:
            lead_id, created = await lead_from_stakeholder(
                session, workspace_id, company.get("icp_id") or s.lead_intel_icp_id, s.lead_intel_business_id, lead,
                s.external_id, person.email_status, origin=LEAD_ORIGIN.get(s.source, "apollo"),
            )  # fmt: skip
        await calls.record(
            workspace_id,
            "apollo",
            "people/match",
            "succeeded",
            action="Reveal email",
            credits=1,
            key=f"apollo:people/match:{workspace_id}:{s.external_id}",
            lead_id=lead_id,
        )
        await stakeholders.save_revealed(s, person, lead_id)
        outcomes.append(
            RevealOutcome(
                stakeholder_id=sid, status="revealed", email=person.email, lead_id=lead_id, lead_created=created
            )
        )

    return StakeholderRevealResult(
        outcomes=outcomes, revealed=sum(o.status == "revealed" for o in outcomes), error=error
    )
