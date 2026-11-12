"""The company pipeline for an ICP: find companies, research and score them, find their stakeholders.

  find        Lead Intel companies matched to the ICP (already verified: qualified straight away),
              Apollo companies matching its search terms, places and size, and Google Maps
              businesses for its search terms around its places. Deduplicated.
  research    Apollo and Google Maps companies: website + Instagram read, ICP rule checked, Claude score.
  status      keep or drop a company by hand, whatever the research said.
  stakeholders  people at qualified companies, through Apollo or LinkedIn (Apify).

Lead Intel is only read here.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from config import get_settings
from background import report
from services import enrich, exports, record_data
from crud import PRODUCT_FIELDS, CompanyFilters, SmartListRepo, CompanyRepo, ExclusionRepo, IcpProfileRepo, PostRepo, ProductRepo, PromptRepo, ProviderCallRepo, SettingsRepo, StakeholderRepo
from dependencies import (
    get_smart_list_repo,
    get_company_repo,
    get_exclusion_repo,
    get_icp_profile_repo,
    get_lead_intel_repo,
    get_optional_apify,
    get_optional_apollo,
    get_optional_places,
    get_optional_brief_writer,
    get_optional_scorer,
    get_optional_brave,
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
    EnrichCompaniesIn,
    EnrichResult,
    PipelineCompanyDetailOut,
    RereadResult,
    FindCompaniesIn,
    FindCompaniesResult,
    PipelineCompanyListOut,
    PipelineCompanyOut,
    PipelineCompanyRowOut,
    CompaniesPageOut,
    CompanyBriefOut,
    PipelineStakeholdersIn,
    BriefsResult,
    CompanyIdsIn,
    LinkedInFindIn,
    LinkedInFindResult,
    PeopleWebIn,
    PeopleWebResult,
    WebSearchIn,
    WebSearchResult,
    ResearchIn,
    ResearchResult,
    SourceFindResult,
    StakeholderSearchResult,
)
from services.apify import (
    EMPLOYEES_ACTOR, INSTAGRAM_PROFILE_ACTOR, LINKEDIN_COMPANY_SEARCH_ACTOR, LINKEDIN_PROFILE_ACTOR, ApifyClient, ApifyError, company_keys,
    elsewhere, employee_from_profile,
)  # fmt: skip
from services.briefs import BriefWriter
from services.websearch import GOOGLE_SEARCH_ACTOR, BraveSearch, GoogleSearch, Searched, pick_facebook, pick_instagram, pick_profiles, pick_website, quoted
from services.apollo import SENIORITIES, ApolloClient, ApolloError
from services.claude import cost_usd
from services.google_places import CENTER_COST_USD, SEARCH_COST_USD, PlacesClient, PlacesError, shared_domain
from services.identity import identity_keys
from services.research import research_companies, reread_site
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


@router.get("/companies", response_model=list[PipelineCompanyListOut])
async def list_companies(
    icp_id: UUID | None = Query(default=None, alias="icpId"),
    status: str | None = Query(default=None, pattern="^(found|review|qualified|not_fit|error)$"),
    workspace_id: UUID = Depends(require_workspace_id),
    companies: CompanyRepo = Depends(get_company_repo),
) -> list[PipelineCompanyListOut]:
    return [PipelineCompanyListOut.model_validate(c) for c in await companies.list(workspace_id, icp_id, status, light=True)]


PRESENCE = "^(all|yes|no)$"
MATCHING_CAP = 20000


def company_filters(
    icp_id: UUID | None = Query(default=None, alias="icpId"),
    archive: str = Query(default="active", pattern="^(active|archived|all)$"),
    status: str = Query(default="all", pattern="^(all|found|review|qualified|not_fit|error)$"),
    q: str | None = Query(default=None, max_length=200),
    source: str | None = Query(default=None, max_length=40),
    location: str | None = Query(default=None, max_length=200),
    website: str | None = Query(default=None, pattern=PRESENCE),
    contacts: str | None = Query(default=None, pattern=PRESENCE),
    score_band: str | None = Query(default=None, alias="scoreBand", pattern="^(all|low|medium|high|unscored)$"),
    icp_state: str | None = Query(default=None, alias="icpState", pattern=PRESENCE),
) -> CompanyFilters:
    return CompanyFilters(icp_id, archive, status, q, source, location, website, contacts, score_band, icp_state)


@router.get("/companies/page", response_model=CompaniesPageOut)
async def page_companies(
    f: CompanyFilters = Depends(company_filters),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    workspace_id: UUID = Depends(require_workspace_id),
    companies: CompanyRepo = Depends(get_company_repo),
) -> CompaniesPageOut:
    """One page of the Companies list, filtered and counted in the database (only the rows shown are read)."""
    rows, total = await companies.page(workspace_id, f, limit, offset)
    people = await companies.people_counts([c.id for c in rows])
    counts = await companies.status_counts(workspace_id, f)
    return CompaniesPageOut(
        items=[PipelineCompanyRowOut.model_validate(c).model_copy(update={"people_count": people.get(c.id, 0)}) for c in rows],
        total=total, limit=limit, offset=offset, counts={"all": sum(counts.values()), **counts},
        sources=await companies.sources(workspace_id, f),
    )  # fmt: skip


@router.get("/companies/matching", response_model=list[CompanyBriefOut])
async def matching_companies(
    f: CompanyFilters = Depends(company_filters),
    workspace_id: UUID = Depends(require_workspace_id),
    companies: CompanyRepo = Depends(get_company_repo),
) -> list[CompanyBriefOut]:
    """Every company matching these filters, in list order, as the few fields selecting needs (Select all)."""
    return [CompanyBriefOut.model_validate(r) for r in await companies.matching(workspace_id, f, MATCHING_CAP)]


@router.get("/companies/lead-intel-ids", response_model=list[UUID])
async def lead_intel_ids(
    workspace_id: UUID = Depends(require_workspace_id), companies: CompanyRepo = Depends(get_company_repo)
) -> list[UUID]:
    """The Lead Intel businesses already in the pipeline."""
    return await companies.lead_intel_ids(workspace_id)


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
            "product": None if product is None else {k: getattr(product, k, [] if k in ("custom_fields", "notes") else "") for k in PRODUCT_FIELDS},
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


@router.get("/companies/{company_id}", response_model=PipelineCompanyDetailOut)
async def get_company(
    company_id: UUID,
    workspace_id: UUID = Depends(require_workspace_id),
    companies: CompanyRepo = Depends(get_company_repo),
    lists: SmartListRepo = Depends(get_smart_list_repo),
) -> PipelineCompanyDetailOut:
    """One company, for its own page, with everything stored about it (its record and its people's Apollo columns)."""
    company = await companies.get(workspace_id, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    people = await lists.people_at(workspace_id, [company.id])
    data = {**record_data.company_groups(company.raw, [p.raw for p in people]), **record_data.company_extras(company.research)}
    return PipelineCompanyDetailOut.model_validate(company).model_copy(update={"data": data})


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


@router.post("/companies/{company_id}/reread", response_model=RereadResult, dependencies=[Depends(require_manager)])
async def reread_company(
    company_id: UUID,
    workspace_id: UUID = Depends(require_workspace_id),
    companies: CompanyRepo = Depends(get_company_repo),
    reader: WebsiteReader = Depends(get_website_reader),
) -> RereadResult:
    """Read the company's website again for more of what it shows: socials, emails, phone, page texts. No AI and
    nothing paid: a plain HTTP read, Jina only when the site needs a browser. Its score and status stay as they are."""
    company = await companies.get(workspace_id, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    found = await reread_site(company, reader)
    if found.reached:
        await companies.save_pages(workspace_id, company.id, found.via or "own_crawl", found.pages)
        await companies.save(company)
    return RereadResult(
        reached=found.reached, via=found.via, pages=len(found.pages), socials=found.socials, emails=found.emails,
        phone=found.phone, detail=found.detail, company=PipelineCompanyOut.model_validate(company),
    )  # fmt: skip


def apify_recorder(calls: ProviderCallRepo, workspace_id: UUID, action: str):
    async def record(actor: str, run: Any, status: str) -> None:
        key = f"apify:run:{run.run_id}" if run is not None and run.run_id else None
        await calls.record(workspace_id, "apify", actor, status, key=key, cost_usd=run.cost_usd if run is not None else None, action=action)

    return record


async def enrich_companies_now(
    workspace_id: UUID, companies: list[Company], website: bool, instagram: bool, linkedin: bool, repo: CompanyRepo,
    posts: PostRepo, calls: ProviderCallRepo, reader: WebsiteReader, apify: ApifyClient | None,
) -> EnrichResult:  # fmt: skip
    """services.enrich.enrich_companies, with what it reads saved and what it costs on Usage."""
    if (instagram or linkedin) and apify is None:
        raise HTTPException(status_code=503, detail="APIFY_TOKEN is not set")

    async def save_pages(company: Company, via: str, pages: list[dict]) -> None:
        await repo.save_pages(workspace_id, company.id, via, pages)

    async def save_posts(company: Company, found: list) -> int:
        return await posts.save_company(workspace_id, company.id, found)

    out = await enrich.enrich_companies(
        companies, website=website, instagram=instagram, linkedin=linkedin, reader=reader, apify=apify, save_pages=save_pages,
        save_posts=save_posts, record=apify_recorder(calls, workspace_id, "Enrich companies"), progress=report,
    )  # fmt: skip
    await repo.save_all(companies)
    return EnrichResult(**vars(out))


@router.post("/enrich", response_model=EnrichResult, dependencies=[Depends(require_manager)])
async def enrich_companies(
    body: EnrichCompaniesIn,
    workspace_id: UUID = Depends(require_workspace_id),
    companies: CompanyRepo = Depends(get_company_repo),
    posts: PostRepo = Depends(get_post_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    reader: WebsiteReader = Depends(get_website_reader),
    apify: ApifyClient | None = Depends(get_optional_apify),
) -> EnrichResult:
    """Fill in companies without judging them (no Claude): their website (free), Instagram profile and LinkedIn page
    (paid, logged on Usage), as chosen. What's already filled in isn't bought again."""
    rows = await companies.get_many(workspace_id, list(dict.fromkeys(body.company_ids)))
    return await enrich_companies_now(workspace_id, rows, body.website, body.instagram, body.linkedin, companies, posts, calls, reader, apify)


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



# ---------- Quick Start's LinkedIn option: find on LinkedIn, web searches, people from the web, company briefs ----------


@router.post("/find-linkedin", response_model=LinkedInFindResult, dependencies=[Depends(require_manager)])
async def find_on_linkedin(
    body: LinkedInFindIn,
    workspace_id: UUID = Depends(require_workspace_id),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    companies: CompanyRepo = Depends(get_company_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    apify: ApifyClient | None = Depends(get_optional_apify),
) -> LinkedInFindResult:
    """Companies LinkedIn's company search finds for the ICP: each search term in turn, in its places, until the limit.
    Paid per company found (Apify). New ones are added to be checked; ones already here are left as they are."""
    if apify is None:
        raise HTTPException(status_code=503, detail="APIFY_TOKEN is not set")
    await _workspace(companies, workspace_id)
    await _icp(lead_intel, body.icp_id)
    terms = [t.strip() for t in body.search_terms if t.strip()]
    places = [p.strip() for p in body.places if p.strip()]
    record = apify_recorder(calls, workspace_id, "Find companies on LinkedIn")
    ids: list[UUID] = []
    dupes, cost, notes = 0, 0.0, []
    for i, term in enumerate(terms):
        left = body.max_companies - len(ids)
        if left <= 0:
            break
        await report("Searching LinkedIn", i, len(terms))
        try:
            found, run = await apify.search_linkedin_companies(term, places, min(left + 10, 1000))
        except ApifyError as exc:
            await record(LINKEDIN_COMPANY_SEARCH_ACTOR, None, "failed")
            notes.append(f"{term}: {exc.message}")
            if exc.status in STOP_STATUSES:
                break
            continue
        await record(LINKEDIN_COMPANY_SEARCH_ACTOR, run, "succeeded")
        cost += run.cost_usd or 0
        new, d = await companies.add_linkedin(workspace_id, body.icp_id, found[:left])
        ids += new
        dupes += d
    return LinkedInFindResult(added=len(ids), duplicates=dupes, company_ids=ids, cost_usd=round(cost, 4) or None, notes=notes)


def web_searcher(engine: str, brave: BraveSearch | None, apify: ApifyClient | None) -> tuple[Any, str | None]:
    """The engine a run chose, or why it can't be used (then its searches are skipped)."""
    if engine == "google":
        return (GoogleSearch(apify), None) if apify else (None, "Google search needs Apify (APIFY_TOKEN), so the web search was skipped.")
    return (brave, None) if brave else (None, "Web search isn't set up (BRAVE_API_KEY), so it was skipped.")


async def run_searches(searcher: Any, queries: list[str], limit: int, calls: ProviderCallRepo, workspace_id: UUID) -> Searched:
    """The searches. A Google (Apify) run is logged as spend; Brave searches are counted (free up to the month's
    allowance), and once the month's allowance is reached Brave isn't asked any more."""
    if isinstance(searcher, GoogleSearch):
        out = await searcher.search_many(queries, limit)
        await apify_recorder(calls, workspace_id, "Search the web")(GOOGLE_SEARCH_ACTOR, out.run, "succeeded" if out.run is not None else "failed")
        return out
    wanted = list(dict.fromkeys(queries))
    allowance = get_settings().brave_monthly_searches
    left = max(0, allowance - await calls.used_this_month("brave"))
    out = await searcher.search_many(wanted[:left], limit)
    if out.hits:
        await calls.record(workspace_id, "brave", "web/search", "succeeded", credits=len(out.hits), action="Search the web")
    if len(wanted) > left:
        out.notes.append(f"This month's {allowance:,} free Brave searches are used up. Choose Google (Apify) for web search on the next run.")
    out.remaining = max(0, left - len(out.hits))
    return out


def _web_queries(c: Company) -> dict[str, str]:
    place = " ".join(x for x in (c.city, c.state) if x)
    wanted = {"website": f"{quoted(c.name)} {place}".strip() if not c.domain else None,
              "instagram": f"site:instagram.com {quoted(c.name)}" if not c.instagram_url else None,
              "facebook": f"site:facebook.com {quoted(c.name)} {place}".strip() if not c.facebook_url else None}  # fmt: skip
    return {k: q for k, q in wanted.items() if q}


@router.post("/web-search", response_model=WebSearchResult, dependencies=[Depends(require_manager)])
async def web_search_companies(
    body: WebSearchIn,
    workspace_id: UUID = Depends(require_workspace_id),
    companies: CompanyRepo = Depends(get_company_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    brave: BraveSearch | None = Depends(get_optional_brave),
    apify: ApifyClient | None = Depends(get_optional_apify),
) -> WebSearchResult:
    """Fill in what's missing for these companies from web searches: their own website, their Instagram and their
    Facebook. Brave is free up to its monthly allowance; Google goes through Apify and is paid per search. Only an
    answer that names the company is used (a website only when its domain is the company's name); a website
    LinkedIn gave is never replaced, and nothing found is left empty."""
    searcher, why_not = web_searcher(body.engine, brave, apify)
    if searcher is None:
        return WebSearchResult(searched=0, websites=0, instagram=0, facebook=0, notes=[why_not or ""])
    rows = await companies.get_many(workspace_id, list(dict.fromkeys(body.company_ids)))
    wanted = {c.id: _web_queries(c) for c in rows}
    queries = [q for w in wanted.values() for q in w.values()]
    await report(f"Searching the web ({searcher.name})", 0, len(rows))
    out = await run_searches(searcher, queries, 10, calls, workspace_id)
    got = {"websites": 0, "instagram": 0, "facebook": 0}
    for c in rows:
        w = wanted[c.id]
        if (q := w.get("website")) in out.hits and (domain := pick_website(c.name, out.hits[q])):
            c.domain, c.website_url = domain, f"https://{domain}"
            got["websites"] += 1
        if (q := w.get("instagram")) in out.hits and (url := pick_instagram(c.name, out.hits[q])):
            c.instagram_url = url
            got["instagram"] += 1
        if (q := w.get("facebook")) in out.hits and (url := pick_facebook(c.name, out.hits[q])):
            c.facebook_url = url
            got["facebook"] += 1
    await companies.save_all(rows)
    # Searched: the companies every search of which was answered (a refusal partway leaves the rest unsearched).
    searched = sum(1 for w in wanted.values() if all(q in out.hits for q in w.values()))
    return WebSearchResult(searched=searched, searches=len(out.hits), cost_usd=round(out.cost_usd, 4) if out.cost_usd else None,
                           remaining=out.remaining, notes=out.notes, **got)  # fmt: skip


@router.post("/people-web", response_model=PeopleWebResult, dependencies=[Depends(require_manager)])
async def people_from_the_web(
    body: PeopleWebIn,
    workspace_id: UUID = Depends(require_workspace_id),
    companies: CompanyRepo = Depends(get_company_repo),
    stakeholders: StakeholderRepo = Depends(get_stakeholder_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    apify: ApifyClient | None = Depends(get_optional_apify),
    brave: BraveSearch | None = Depends(get_optional_brave),
) -> PeopleWebResult:
    """People at qualified companies found by web searches for LinkedIn profiles that name the company and a title
    (Brave, free; or Google through Apify, paid), then read from LinkedIn (Apify, paid per profile). Someone whose
    current job is elsewhere is left out."""
    searcher, why_not = web_searcher(body.engine, brave, apify)
    if searcher is None:
        return PeopleWebResult(companies=0, profiles=0, new=0, notes=[why_not or ""])
    if apify is None:
        raise HTTPException(status_code=503, detail="APIFY_TOKEN is not set")
    rows = [c for c in await companies.get_many(workspace_id, list(dict.fromkeys(body.company_ids))) if c.status == "qualified"]
    titles = [t.strip() for t in body.titles if t.strip()][:6] or ["Owner", "Founder", "CEO"]
    record = apify_recorder(calls, workspace_id, "Find stakeholders")
    role = " OR ".join(quoted(t) for t in titles)
    query = {c.id: f"site:linkedin.com/in {quoted(c.name)} ({role})" for c in rows}
    await report(f"Searching the web for people ({searcher.name})", 0, len(rows))
    searched = await run_searches(searcher, list(query.values()), 20, calls, workspace_id)
    profiles = new = 0
    cost, notes = searched.cost_usd or 0.0, list(searched.notes)
    for i, c in enumerate(rows):
        await report("Reading the people found", i, len(rows))
        urls = pick_profiles(c.name, searched.hits.get(query[c.id], []), body.max_per_company)
        if not urls:
            continue
        try:
            found, run = await apify.linkedin_profiles(urls)
        except ApifyError as exc:
            await record(LINKEDIN_PROFILE_ACTOR, None, "failed")
            notes.append(f"{c.name}: {exc.message}")
            if exc.status in STOP_STATUSES:
                break
            continue
        await record(LINKEDIN_PROFILE_ACTOR, run, "succeeded")
        cost += run.cost_usd or 0
        keys = company_keys(c.name, c.domain)
        people = [p for p in (employee_from_profile(item, keys, c.linkedin_url) for item in found.values()) if p]
        for p in people:
            p.company_name = p.company_name or c.name
        profiles += len(people)
        new += await stakeholders.save_employees(workspace_id, c.lead_intel_business_id, people, pipeline_company_id=c.id, icp_id=c.icp_id)
    return PeopleWebResult(companies=len(rows), profiles=profiles, new=new, cost_usd=round(cost, 4) or None, notes=notes)


@router.post("/briefs", response_model=BriefsResult, dependencies=[Depends(require_manager)])
async def write_briefs(
    body: CompanyIdsIn,
    workspace_id: UUID = Depends(require_workspace_id),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    companies: CompanyRepo = Depends(get_company_repo),
    profiles: IcpProfileRepo = Depends(get_icp_profile_repo),
    products: ProductRepo = Depends(get_product_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    writer: BriefWriter | None = Depends(get_optional_brief_writer),
) -> BriefsResult:
    """An outreach brief for each of these companies, by Claude, from everything gathered about it: who they are, why
    they fit, their signals and online presence, what they post, the hooks most likely to work, who to email first.
    Kept with the company (its research, under "brief")."""
    if writer is None:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not set")
    rows = await companies.get_many(workspace_id, list(dict.fromkeys(body.company_ids)))
    icps: dict[UUID, dict[str, Any]] = {}
    written = failed = 0
    cost, notes = 0.0, []
    for i, c in enumerate(rows):
        await report("Writing company briefs", i, len(rows))
        context: dict[str, Any] = {}
        if c.icp_id:
            if c.icp_id not in icps:
                icp = await lead_intel.get_icp(c.icp_id) or {}
                profile = (await profiles.get_many(workspace_id, [c.icp_id])).get(c.icp_id)
                product = await products.get(workspace_id, profile.product_id) if profile and profile.product_id else None
                icps[c.icp_id] = {
                    "name": icp.get("name"), "industry": icp.get("industry"), "places": icp.get("places"),
                    "description": getattr(profile, "description", None), "pains": getattr(profile, "pains", None),
                    "offer": getattr(profile, "offer", None), "proof": getattr(profile, "proof", None),
                    "product": None if product is None else {k: getattr(product, k, None) for k in PRODUCT_FIELDS},
                }  # fmt: skip
            context = icps[c.icp_id]
        material = await companies.brief_material(workspace_id, c)
        research = c.research or {}
        evidence = {
            "icp_and_product": context,
            "company": {"name": c.name, "website": c.website_url or c.domain, "city": c.city, "state": c.state, "phone": c.phone,
                        "linkedin": c.linkedin_url, "instagram": c.instagram_url, "facebook": c.facebook_url, "twitter": c.twitter_url,
                        "source": c.source, "status": c.status},  # fmt: skip
            "linkedin_listing": c.raw if c.source == "linkedin" else None,
            "verdict": {"score": c.score, "detail": c.score_detail, "reason": c.status_reason},
            "website": research.get("website"),
            "instagram": research.get("instagram"),
            "contacts": research.get("contacts"),
            **material,
        }
        result = await writer.write(evidence)
        call_cost = cost_usd(result.model, result.input_tokens, result.output_tokens) if result.input_tokens else 0
        cost += call_cost
        await calls.record(workspace_id, "claude", "messages", "failed" if result.error else "succeeded", cost_usd=round(call_cost, 4),
                           action="Write company briefs", model=result.model, input_tokens=result.input_tokens, output_tokens=result.output_tokens)  # fmt: skip
        if result.brief is None:
            failed += 1
            notes.append(f"{c.name}: {result.error}")
            continue
        c.research = {**research, "brief": result.brief.model_dump(), "brief_at": datetime.now(UTC).isoformat()}
        written += 1
    await companies.save_all(rows)
    return BriefsResult(written=written, failed=failed, cost_usd=round(cost, 4) or None, notes=notes)


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
