"""The company pipeline for an ICP: find companies, research and score them, find their stakeholders.

  find        Lead Intel companies matched to the ICP (already verified: qualified straight away),
              Apollo companies matching its search terms, places and size, and Google Maps
              businesses for its search terms around its places. Deduplicated.
  research    Apollo and Google Maps companies: website + Instagram read, ICP rule checked, Claude score.
  status      keep or drop a company by hand, whatever the research said.
  stakeholders  people at qualified companies, through Apollo or LinkedIn (Apify).

Lead Intel is only read here.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from config import get_settings
from services import exports
from crud import PRODUCT_FIELDS, SmartListRepo, CompanyRepo, ExclusionRepo, IcpProfileRepo, PostRepo, ProductRepo, PromptRepo, ProviderCallRepo, SettingsRepo, StakeholderRepo
from dependencies import (
    get_smart_list_repo,
    get_company_repo,
    get_exclusion_repo,
    get_icp_profile_repo,
    get_lead_intel_repo,
    get_optional_apify,
    get_optional_apollo,
    get_optional_places,
    get_optional_scorer,
    get_post_repo,
    get_product_repo,
    get_prompt_repo,
    get_provider_call_repo,
    get_settings_repo,
    get_stakeholder_repo,
    get_website_reader,
    require_manager,
    require_workspace_id,
)
from lead_intel import LeadIntelRepo
from schemas import (
    CompanyContentOut,
    CompanyPageOut,
    CompanyPostOut,
    CompanySearchResult,
    CompanyDecisionIn,
    CompanyDecisionResult,
    CompanyStatusIn,
    FindCompaniesIn,
    FindCompaniesResult,
    PipelineCompanyOut,
    PipelineStakeholdersIn,
    ResearchIn,
    ResearchResult,
    SourceFindResult,
    StakeholderSearchResult,
)
from services.apify import EMPLOYEES_ACTOR, INSTAGRAM_PROFILE_ACTOR, ApifyClient, ApifyError, elsewhere
from services.apollo import SENIORITIES, ApolloClient, ApolloError
from services.claude import cost_usd
from services.google_places import CENTER_COST_USD, SEARCH_COST_USD, PlacesClient, PlacesError, shared_domain
from services.identity import identity_keys
from services.research import research_companies
from services.scoring import CompanyScorer, PromptSpec
from models import VERIFICATION_SOURCES, Company
from services.website import WebsiteReader, normalize_domain

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

LEAD_INTEL_MAX = 2000
STOP_STATUSES = {401, 402, 403, 429}


async def _icp(lead_intel: LeadIntelRepo, icp_id: UUID) -> dict[str, Any]:
    icp = await lead_intel.get_icp(icp_id)
    if not icp:
        raise HTTPException(status_code=404, detail="ICP not found")
    return icp


async def _workspace(companies: CompanyRepo, workspace_id: UUID) -> None:
    if not await companies.workspace_exists(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")


@router.get("/companies", response_model=list[PipelineCompanyOut])
async def list_companies(
    icp_id: UUID | None = Query(default=None, alias="icpId"),
    status: str | None = Query(default=None, pattern="^(found|review|qualified|not_fit|error)$"),
    workspace_id: UUID = Depends(require_workspace_id),
    companies: CompanyRepo = Depends(get_company_repo),
) -> list[PipelineCompanyOut]:
    return [PipelineCompanyOut.model_validate(c) for c in await companies.list(workspace_id, icp_id, status)]


@router.get("/companies.csv", response_class=Response)
async def export_companies(
    icp_id: UUID | None = Query(default=None, alias="icpId"),
    workspace_id: UUID = Depends(require_workspace_id),
    companies: CompanyRepo = Depends(get_company_repo),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    lists: SmartListRepo = Depends(get_smart_list_repo),
) -> Response:
    """The Companies page as a CSV (all of them, or one ICP's): research, score, ICP, smart list and people found."""
    rows = await companies.list(workspace_id, icp_id, None)
    icps = {i["id"]: i["name"] for i in await lead_intel.list_icps()}
    smart = await lists.list_names(workspace_id, "company", [c.id for c in rows])
    people: dict[UUID, int] = {}
    for p in await lists.people_at(workspace_id, [c.id for c in rows]):
        people[p.company_id] = people.get(p.company_id, 0) + 1
    body = exports.write(exports.COMPANY_COLUMNS, [
        exports.company_row(c, icps.get(c.icp_id, ""), smart.get(c.id, ""), people.get(c.id, 0)) for c in rows
    ])  # fmt: skip
    return Response(content=body, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="companies.csv"'})


@router.post("/find", response_model=FindCompaniesResult, dependencies=[Depends(require_manager)])
async def find_companies(
    body: FindCompaniesIn,
    workspace_id: UUID = Depends(require_workspace_id),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    companies: CompanyRepo = Depends(get_company_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    apollo: ApolloClient | None = Depends(get_optional_apollo),
    places: PlacesClient | None = Depends(get_optional_places),
    exclusions: ExclusionRepo = Depends(get_exclusion_repo),
) -> FindCompaniesResult:
    """Find companies for an ICP in Lead Intel, Apollo and/or Google Maps, and add the new ones to the
    pipeline. Anything on the do-not-contact list is left out."""
    await _workspace(companies, workspace_id)
    icp = await _icp(lead_intel, body.icp_id)
    li_result = apollo_result = maps_result = None
    is_excluded = await exclusions.matcher()

    if "lead_intel" in body.sources:
        rows: list[dict[str, Any]] = []
        picked = list(dict.fromkeys(body.lead_intel_ids)) if body.lead_intel_ids else None
        while len(rows) < LEAD_INTEL_MAX:
            page, total = await lead_intel.list_companies(body.icp_id, None, 200, len(rows), only_ids=picked)
            rows += page
            if not page or len(rows) >= total:
                break
        found = [
            {
                "id": r["id"], "name": r["display_name"], "domain": normalize_domain(r.get("domain")),
                "website_url": r["fields"].get("website_url"), "instagram_url": r["fields"].get("instagram_url"),
                "city": r.get("city"), "state": r.get("state"),
            }
            for r in rows
        ]  # fmt: skip
        allowed = [c for c in found if not is_excluded(identity_keys(domain=c["domain"], name=c["name"], city=c["city"], state=c["state"]))]
        added, upgraded, had = await companies.add_lead_intel(workspace_id, body.icp_id, allowed)
        li_result = SourceFindResult(
            found=len(found), added=added, upgraded=upgraded, duplicates=had, total_available=len(rows), excluded=len(found) - len(allowed)
        )

    if "apollo" in body.sources:
        if apollo is None:
            apollo_result = SourceFindResult(error="APOLLO_API_KEY is not set")
        else:
            keywords = [k.strip() for k in body.apollo.keywords if k.strip()] or (icp.get("search_terms") or icp.get("any_of") or [])
            locations = [p.strip() for p in body.apollo.locations if p.strip()] or (icp.get("places") or [])
            try:
                orgs, total = await apollo.search_organizations(
                    keywords=keywords,
                    locations=locations,
                    employee_ranges=body.apollo.employee_ranges,
                    page=body.apollo.page,
                    per_page=body.apollo.per_page,
                )
            except ApolloError as exc:
                await calls.record(workspace_id, "apollo", "mixed_companies/search", "failed", action="Find companies")
                apollo_result = SourceFindResult(error=exc.message)
            else:
                await calls.record(workspace_id, "apollo", "mixed_companies/search", "succeeded", credits=1, action="Find companies")
                for org in orgs:
                    org.domain = normalize_domain(org.domain or org.website_url)
                allowed = [o for o in orgs if not is_excluded(identity_keys(domain=o.domain))]
                added, dupes = await companies.add_apollo(workspace_id, body.icp_id, allowed)
                apollo_result = SourceFindResult(
                    found=len(orgs), added=added, duplicates=dupes, total_available=total, excluded=len(orgs) - len(allowed)
                )

    if "google_maps" in body.sources:
        maps_result = await _find_on_google_maps(workspace_id, body, icp, places, companies, calls, is_excluded)

    return FindCompaniesResult(lead_intel=li_result, apollo=apollo_result, google_maps=maps_result)


async def _find_on_google_maps(
    workspace_id: UUID, body: FindCompaniesIn, icp: dict[str, Any], places: PlacesClient | None,
    companies: CompanyRepo, calls: ProviderCallRepo, is_excluded=lambda keys: None,
) -> SourceFindResult:  # fmt: skip
    """Each search term around each place, sharing one request budget. Chains are skipped; a listing
    whose website is a platform profile (Booksy, Facebook...) is kept without a domain."""
    if places is None:
        return SourceFindResult(error="GOOGLE_PLACES_API_KEY is not set")
    gm = body.google_maps
    terms = [t.strip() for t in gm.search_terms if t.strip()] or (icp.get("search_terms") or icp.get("any_of") or [])
    locations = [p.strip() for p in gm.locations if p.strip()] or (icp.get("places") or [])
    if not terms:
        return SourceFindResult(error="Add a search term, e.g. \"screen printing\". This ICP has none.")
    if not locations:
        return SourceFindResult(error="Add a city, state or postcode. Google Maps searches around a place.")

    found: dict[str, Any] = {}
    searches = lookups = capped = 0
    notes: list[str] = []
    fatal: str | None = None
    pairs = [(term, location) for term in terms for location in locations]
    for i, (term, location) in enumerate(pairs):
        left = gm.max_searches - searches - lookups
        if left < 2:  # a lookup and at least one search
            skipped = len(pairs) - i
            notes.append(f"Stopped at the limit of {gm.max_searches} Google requests; {skipped} search{'es' if skipped != 1 else ''} not run.")
            break
        # An even share of what's left, so the first term can't use it all; a share it doesn't need passes on.
        share = max(2, left // (len(pairs) - i))
        try:
            area = await places.search_area(term, location, gm.radius_miles, share)
        except PlacesError as exc:
            lookups += 1  # the centre lookup that failed is still billed
            if exc.status in (401, 403, 429):
                fatal = exc.message
                break
            notes.append(f"{term} around {location}: {exc.message}")
            continue
        searches += area.searches
        lookups += area.center_lookups
        capped += area.capped_cells
        for pid, place in area.places.items():
            found.setdefault(pid, place)
        notes.extend(n for n in area.notes if n not in notes)
        if area.stopped_at_limit:
            notes.append(f"{term} in {location}: found {len(area.places)}, then used its share of {share} requests. Google has more; raise the request limit to see them.")
    if capped:
        notes.append(f"{capped} area{'s were' if capped != 1 else ' was'} still full after the finest split, so Google may have more there.")

    cost = round(searches * SEARCH_COST_USD + lookups * CENTER_COST_USD, 4)
    if searches + lookups:
        await calls.record(
            workspace_id, "google_places", "places:searchText", "failed" if fatal and not found else "succeeded",
            credits=searches + lookups, cost_usd=cost, action="Find companies",
        )  # fmt: skip

    chains = excluded = 0
    to_add = []
    for place in found.values():
        domain = normalize_domain(place.website_url)
        kind = shared_domain(domain)
        if kind == "chain":
            chains += 1
            continue
        keys = identity_keys(place_id=place.place_id, domain=domain, phone=place.phone, name=place.name, city=place.city, state=place.state)
        if is_excluded(keys):
            excluded += 1
            continue
        to_add.append((place, None if kind == "platform" else domain))
    if chains:
        notes.append(f"{chains} national chain location{'s' if chains != 1 else ''} skipped.")
    added, dupes = await companies.add_google_maps(workspace_id, body.icp_id, to_add)
    return SourceFindResult(
        found=len(found), added=added, duplicates=dupes, error=fatal, searches=searches + lookups, cost_usd=cost, notes=notes,
        excluded=excluded,
    )


@router.post("/research", response_model=ResearchResult, dependencies=[Depends(require_manager)])
async def research(
    body: ResearchIn,
    workspace_id: UUID = Depends(require_workspace_id),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    companies: CompanyRepo = Depends(get_company_repo),
    profiles: IcpProfileRepo = Depends(get_icp_profile_repo),
    prompts: PromptRepo = Depends(get_prompt_repo),
    posts: PostRepo = Depends(get_post_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    workspace_settings: SettingsRepo = Depends(get_settings_repo),
    products: ProductRepo = Depends(get_product_repo),
    reader: WebsiteReader = Depends(get_website_reader),
    apify: ApifyClient | None = Depends(get_optional_apify),
    scorer: CompanyScorer | None = Depends(get_optional_scorer),
) -> ResearchResult:
    """Research Apollo and Google Maps companies against their ICP and decide: qualified or not a fit.

    Lead Intel companies are skipped: Lead Intel already verified them. Up to 10 at a time; a
    website read takes up to 30 seconds (they're read three at a time).
    """
    rows = await companies.get_many(workspace_id, list(dict.fromkeys(body.company_ids)))
    todo = [c for c in rows if c.source != "lead_intel" and (body.force or c.researched_at is None)]
    skipped = len(body.company_ids) - len(todo)
    if body.force:
        # Verifying again, asked for by a person: the new verdict replaces an approval or rejection made by hand.
        for c in todo:
            c.overridden = False
    if not todo:
        return ResearchResult(researched=0, skipped=skipped, qualified=0, not_fit=0, errors=0, cost_usd=0, notes=[])

    settings = get_settings()
    limits = await workspace_settings.research(workspace_id)
    approval = await workspace_settings.general(workspace_id)
    summary_total = ResearchResult(researched=0, skipped=skipped, qualified=0, not_fit=0, errors=0, cost_usd=0, notes=[])
    by_icp: dict[UUID | None, list] = {}
    for c in todo:
        by_icp.setdefault(c.icp_id, []).append(c)
    for icp_id, group in by_icp.items():
        if icp_id is None:
            continue
        icp = await _icp(lead_intel, icp_id)
        profile_rows = await profiles.get_many(workspace_id, [icp_id])
        profile = profile_rows.get(icp_id)
        product = await products.get(workspace_id, profile.product_id) if profile and profile.product_id else None
        profile_dict = None if profile is None else {
            "description": profile.description, "pains": profile.pains, "offer": profile.offer,
            "product": None if product is None else {k: getattr(product, k) for k in PRODUCT_FIELDS},
        }  # fmt: skip
        version = await prompts.for_icp(workspace_id, "verify", profile.verify_prompt_id if profile else None)
        spec = None if version is None else PromptSpec(
            version.system_prompt, version.user_prompt, version.model, version.max_tokens, str(version.id)
        )
        # The ICP's verification sources: each one switched off is skipped for its companies.
        sources = set(profile.verification_sources if profile else VERIFICATION_SOURCES)
        with reader.jina_off("jina" not in sources):
            summary = await research_companies(
                group, icp, profile_dict, reader, apify if "instagram" in sources else None,
                scorer if "score" in sources else None, approval["company_threshold"], spec,
                max_pages=limits["research_max_pages"], max_seconds=limits["research_max_seconds"],
                read_website="website" in sources, auto_approve=approval["company_auto_approve"],
                unscored="Claude's fit score is off for this ICP" if scorer and "score" not in sources else "ANTHROPIC_API_KEY is not set",
            )  # fmt: skip
        for company in group:
            await companies.save(company)
        # Everything read is kept: each page's full text, and the company's own Instagram posts.
        for company_id, (via, pages) in summary.pages.items():
            await companies.save_pages(workspace_id, company_id, via, pages)
        for company_id, found in summary.posts.items():
            await posts.save_company(workspace_id, company_id, found)
        for run_id, run_cost in summary.apify_runs:
            await calls.record(
                workspace_id, "apify", INSTAGRAM_PROFILE_ACTOR, "succeeded",
                key=f"apify:run:{run_id}" if run_id else None, cost_usd=run_cost, action="Research companies",
            )  # fmt: skip
        if summary.claude_calls:
            cost = cost_usd(summary.claude_model or settings.claude_scoring_model, summary.input_tokens, summary.output_tokens)
            await calls.record(
                workspace_id, "claude", "messages", "succeeded", cost_usd=round(cost, 4), action="Research companies",
                model=summary.claude_model, input_tokens=summary.input_tokens, output_tokens=summary.output_tokens,
            )  # fmt: skip
        summary_total.researched += len(group)
        summary_total.qualified += summary.qualified
        summary_total.not_fit += summary.not_fit
        summary_total.to_approve += summary.review
        summary_total.errors += summary.errors
        summary_total.cost_usd = round(summary_total.cost_usd + summary.apify_cost_usd, 4)
        summary_total.notes += summary.notes
    return summary_total


@router.get("/companies/{company_id}", response_model=PipelineCompanyOut)
async def get_company(
    company_id: UUID, workspace_id: UUID = Depends(require_workspace_id), companies: CompanyRepo = Depends(get_company_repo)
) -> PipelineCompanyOut:
    """One company, for its own page."""
    company = await companies.get(workspace_id, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return PipelineCompanyOut.model_validate(company)


@router.get("/companies/{company_id}/content", response_model=CompanyContentOut)
async def company_content(
    company_id: UUID,
    workspace_id: UUID = Depends(require_workspace_id),
    companies: CompanyRepo = Depends(get_company_repo),
    posts: PostRepo = Depends(get_post_repo),
) -> CompanyContentOut:
    """What research read for a company: each website page's full text, and its own Instagram posts."""
    if not await companies.get(workspace_id, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    return CompanyContentOut(
        pages=[CompanyPageOut.model_validate(p) for p in await companies.pages(workspace_id, company_id)],
        posts=[CompanyPostOut.model_validate(p) for p in await posts.for_company(workspace_id, company_id)],
    )


@router.patch("/companies/{company_id}", response_model=PipelineCompanyOut, dependencies=[Depends(require_manager)])
async def set_status(
    company_id: UUID,
    body: CompanyStatusIn,
    workspace_id: UUID = Depends(require_workspace_id),
    companies: CompanyRepo = Depends(get_company_repo),
) -> PipelineCompanyOut:
    """Keep or drop a company by hand. Research won't change it back."""
    company = await companies.get(workspace_id, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    _decide(company, body.status)
    await companies.save(company)
    return PipelineCompanyOut.model_validate(company)


def _decide(company: Company, status: str) -> None:
    """A person's decision, with what Claude said kept after it."""
    said = (company.status_reason or "").split(" · ", 1)[-1] if company.status_reason else ""
    said = "" if said in ("Kept by hand", "Dropped by hand") or said.startswith(("Approved", "Rejected")) else said
    company.status = status
    company.status_reason = ("Approved" if status == "qualified" else "Rejected") + (f" · {said}" if said else "")
    company.overridden = True


@router.post("/companies/decide", response_model=CompanyDecisionResult, dependencies=[Depends(require_manager)])
async def decide_companies(
    body: CompanyDecisionIn,
    workspace_id: UUID = Depends(require_workspace_id),
    companies: CompanyRepo = Depends(get_company_repo),
) -> CompanyDecisionResult:
    """Approve or reject companies in bulk (the To approve queue). Research won't change them back."""
    rows = await companies.get_many(workspace_id, list(dict.fromkeys(body.company_ids)))
    for company in rows:
        _decide(company, body.status)
    await companies.save_all(rows)
    return CompanyDecisionResult(changed=len(rows))


@router.post("/stakeholders", response_model=StakeholderSearchResult, dependencies=[Depends(require_manager)])
async def find_stakeholders(
    body: PipelineStakeholdersIn,
    workspace_id: UUID = Depends(require_workspace_id),
    companies: CompanyRepo = Depends(get_company_repo),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    apollo: ApolloClient | None = Depends(get_optional_apollo),
    apify: ApifyClient | None = Depends(get_optional_apify),
) -> StakeholderSearchResult:
    """Find people at qualified companies: Apollo by website domain (paid Apollo plans only; no credits), or LinkedIn
    through Apify by company name (paid per person). Only qualified companies are searched."""
    if bad := [s for s in body.seniorities if s not in SENIORITIES]:
        raise HTTPException(status_code=422, detail=f"Unknown seniorities: {', '.join(bad)}")
    client = apollo if body.source == "apollo" else apify
    if client is None:
        missing = "APOLLO_API_KEY" if body.source == "apollo" else "APIFY_TOKEN"
        raise HTTPException(status_code=503, detail=f"{missing} is not set")
    if body.source == "linkedin" and len(body.company_ids) > 10:
        raise HTTPException(status_code=422, detail="Search LinkedIn for 10 companies or fewer at a time")

    rows = {c.id: c for c in await companies.get_many(workspace_id, list(dict.fromkeys(body.company_ids)))}
    titles = [t.strip() for t in body.titles if t.strip()]
    results: list[CompanySearchResult] = []
    error: str | None = None
    cost = 0.0
    for cid in dict.fromkeys(body.company_ids):
        c = rows.get(cid)
        name = c.name if c else None
        if c is None:
            results.append(CompanySearchResult(company_id=cid, name=None, status="skipped", reason="Not in this workspace"))
            continue
        if error:
            results.append(CompanySearchResult(company_id=cid, name=name, status="failed", reason="Not searched"))
            continue
        if c.status != "qualified":
            results.append(CompanySearchResult(company_id=cid, name=name, status="skipped", reason="Not qualified yet"))
            continue
        try:
            if body.source == "apollo":
                if not c.domain:
                    results.append(CompanySearchResult(company_id=cid, name=name, status="skipped", reason="No website domain to search by"))
                    continue
                people, total = await apollo.search_people(c.domain, titles, body.seniorities)  # type: ignore[union-attr]
                await calls.record(workspace_id, "apollo", "mixed_people/api_search", "succeeded", credits=0, action="Find stakeholders")
                for p in people:
                    p.organization_name = p.organization_name or c.name
                new = await stakeholders.save_found(
                    workspace_id, c.lead_intel_business_id, people, icp_id=c.icp_id, pipeline_company_id=c.id
                )
                results.append(CompanySearchResult(company_id=cid, name=name, status="searched", found=len(people), new=new, total_at_apollo=total))
            else:
                people, run = await apify.company_employees(  # type: ignore[union-attr]
                    c.name, titles, body.max_per_company, body.find_emails, linkedin_url=c.linkedin_url, domain=c.domain
                )
                cost += run.cost_usd or 0
                await calls.record(
                    workspace_id, "apify", EMPLOYEES_ACTOR, "succeeded",
                    key=f"apify:run:{run.run_id}" if run.run_id else None, cost_usd=run.cost_usd, action="Find stakeholders",
                )  # fmt: skip
                for p in people:
                    p.company_name = p.company_name or c.name
                new = await stakeholders.save_employees(
                    workspace_id, c.lead_intel_business_id, people, pipeline_company_id=c.id, icp_id=c.icp_id
                )
                results.append(CompanySearchResult(company_id=cid, name=name, status="searched", found=len(people), new=new,
                                                   reason=elsewhere(run.skipped_elsewhere)))  # fmt: skip
        except (ApolloError, ApifyError) as exc:
            provider = "apollo" if body.source == "apollo" else "apify"
            await calls.record(workspace_id, provider, "people search", "failed", action="Find stakeholders")
            results.append(CompanySearchResult(company_id=cid, name=name, status="failed", reason=exc.message))
            if exc.status in STOP_STATUSES:
                error = exc.message
    return StakeholderSearchResult(results=results, error=error, cost_usd=round(cost, 4) or None)
