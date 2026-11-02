"""Smart lists: companies and people sorted by Claude into high-level groups (services/smart_lists.py), each
company list with a draft base ICP to create in Lead Intel, and Apollo-format CSV downloads.

Sorting spends (Claude, and Jina for a site our own fetch can't read), so it and drafting are for Management
and admins; anyone in the workspace can see the lists and download them."""

import asyncio
from typing import Any
from uuid import UUID

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from config import get_settings
from crud import IcpCampaignRepo, IcpProfileRepo, ProviderCallRepo, SmartListRepo
from dependencies import (
    get_icp_campaign_repo, get_icp_profile_repo, get_lead_intel_icp_writer, get_optional_anthropic, get_optional_sequencer,
    get_provider_call_repo, get_smart_list_repo, get_website_reader, require_manager, require_workspace_id,
)  # fmt: skip
from lead_intel_writes import LeadIntelIcpWriter
from routers.icps import create_icp
from models import Company, SmartList, Stakeholder
from schemas import (
    IcpCreateIn, SmartListCompanyOut, SmartListDetailOut, SmartListIcpDraft, SmartListOut, SmartListPersonOut, SmartListsSummary,
    SmartListUpdateIn, SortListsIn, SortListsResult,
)  # fmt: skip
from services import smart_lists
from services.apify import search_name
from services.people_csv import export_csv
from services.sending import SequencerClient
from services.website import WebsiteReader

router = APIRouter(prefix="/smart-lists", tags=["smart lists"])

NO_CLAUDE = "ANTHROPIC_API_KEY is not set"
SITE_READS = 5  # websites read at once while sorting


def _out(lst: SmartList, count: int) -> SmartListOut:
    return SmartListOut(
        id=lst.id, kind=lst.kind, name=lst.name, count=count, lead_intel_icp_id=lst.lead_intel_icp_id, created_at=lst.created_at,
        icp_draft=SmartListIcpDraft.model_validate(lst.icp_draft) if lst.icp_draft else None,
    )  # fmt: skip


async def _list(repo: SmartListRepo, workspace_id: UUID, list_id: UUID) -> SmartList:
    lst = await repo.get(workspace_id, list_id)
    if lst is None:
        raise HTTPException(status_code=404, detail="Smart list not found")
    return lst


def _name(p: Stakeholder) -> str:
    return f"{p.first_name} {p.last_name}".strip() or p.email or "Unnamed"


@router.get("", response_model=SmartListsSummary)
async def get_lists(
    kind: str = Query(default="company", pattern="^(company|person)$"),
    workspace_id: UUID = Depends(require_workspace_id),
    repo: SmartListRepo = Depends(get_smart_list_repo),
) -> SmartListsSummary:
    lists = [_out(lst, n) for lst, n in await repo.lists(workspace_id, kind)]
    return SmartListsSummary(lists=lists, unsorted=await repo.count_unsorted(workspace_id, kind))


@router.post("/sort", response_model=SortListsResult, dependencies=[Depends(require_manager)])
async def sort_lists(
    body: SortListsIn,
    workspace_id: UUID = Depends(require_workspace_id),
    repo: SmartListRepo = Depends(get_smart_list_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    claude: anthropic.AsyncAnthropic | None = Depends(get_optional_anthropic),
    reader: WebsiteReader = Depends(get_website_reader),
) -> SortListsResult:
    """Sort companies or people not in a list yet. A duplicate of one already sorted joins its list without
    asking Claude; a company with no research yet has its homepage read first (plain HTTP, Jina when that fails)."""
    if claude is None:
        raise HTTPException(status_code=503, detail=NO_CLAUDE)
    kind = body.kind
    todo = await repo.unsorted(workspace_id, kind, body.limit)
    known = await repo.sorted_keys(workspace_id, kind)
    key_of = smart_lists.company_key if kind == "company" else smart_lists.person_key

    rows: list[tuple[UUID, UUID, str, str]] = []
    ask: dict[str, list[Any]] = {}  # dedupe key -> the members sharing it; Claude sorts one of them
    duplicates = 0
    for member in todo:
        key = key_of(member)
        if key in known:
            rows.append((member.id, known[key], key, "Same as one already sorted"))
            duplicates += 1
        else:
            duplicates += key in ask
            ask.setdefault(key, []).append(member)

    sites_read = 0
    if kind == "company":
        unread = [group[0] for group in ask.values() if group[0].domain and not ((group[0].research or {}).get("website") or {}).get("text")]
        gate = asyncio.Semaphore(SITE_READS)

        async def read(company: Company) -> None:
            nonlocal sites_read
            async with gate:
                site = await reader.read(company.domain, max_pages=1)
            if site.reached:
                company._site_text = site.text  # only for the description below; never saved
                sites_read += 1

        await asyncio.gather(*(read(c) for c in unread))

    describe = smart_lists.describe_company if kind == "company" else smart_lists.describe_person
    entries = [(key, describe(group[0])) for key, group in ask.items()]
    existing = [lst.name for lst, _ in await repo.lists(workspace_id, kind)]
    model = get_settings().claude_scoring_model
    result = await smart_lists.sort(claude, model, kind, existing, entries) if entries else smart_lists.SortResult()
    if result.calls:
        await calls.record(
            workspace_id, "claude", "messages", "succeeded", model=model, cost_usd=result.cost_usd, action="Sort smart lists",
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        )  # fmt: skip

    existing_keys = {smart_lists.name_key(n) for n in existing}
    new_lists: list[str] = []
    by_name: dict[str, SmartList] = {}
    for key, pick in result.picks.items():
        name_key = smart_lists.name_key(pick.list)
        if name_key not in by_name:
            by_name[name_key] = await repo.get_or_create(workspace_id, kind, pick.list, name_key)
            if name_key not in existing_keys:
                new_lists.append(by_name[name_key].name)
        for member in ask[key]:
            rows.append((member.id, by_name[name_key].id, key, pick.reason))
    await repo.add_members(workspace_id, kind, rows)
    return SortListsResult(
        sorted=len(rows), by_claude=len(result.picks), duplicates=duplicates, new_lists=new_lists, sites_read=sites_read,
        left=await repo.count_unsorted(workspace_id, kind), cost_usd=result.cost_usd, errors=result.errors,
    )  # fmt: skip


@router.get("/people.csv", response_class=Response)
async def export_people(
    workspace_id: UUID = Depends(require_workspace_id), repo: SmartListRepo = Depends(get_smart_list_repo)
) -> Response:
    """Every person in the workspace, in Apollo's people export format."""
    people = await repo.all_people(workspace_id)
    return _csv(await _rows(repo, workspace_id, people), "people.csv")


@router.get("/{list_id}", response_model=SmartListDetailOut)
async def get_list(
    list_id: UUID, workspace_id: UUID = Depends(require_workspace_id), repo: SmartListRepo = Depends(get_smart_list_repo)
) -> SmartListDetailOut:
    lst = await _list(repo, workspace_id, list_id)
    members = await repo.members(workspace_id, lst)
    companies, people = [], []
    for m, member in members:
        if lst.kind == "company":
            companies.append(SmartListCompanyOut(id=m.id, name=m.name, domain=m.domain, city=m.city, state=m.state, source=m.source,
                                                 status=m.status, reason=member.reason))  # fmt: skip
        else:
            people.append(SmartListPersonOut(id=m.id, name=_name(m), title=m.title, company=m.organization_name, email=m.email,
                                             email_status=m.email_status, linkedin_url=m.linkedin_url, reason=member.reason))  # fmt: skip
    return SmartListDetailOut(**_out(lst, len(members)).model_dump(), companies=companies, people=people)


@router.patch("/{list_id}", response_model=SmartListOut, dependencies=[Depends(require_manager)])
async def update_list(
    list_id: UUID, body: SmartListUpdateIn, workspace_id: UUID = Depends(require_workspace_id),
    repo: SmartListRepo = Depends(get_smart_list_repo),
) -> SmartListOut:  # fmt: skip
    """Rename a list, or record the Lead Intel ICP created from its draft."""
    lst = await _list(repo, workspace_id, list_id)
    if body.name is not None:
        key = smart_lists.name_key(body.name)
        if key != lst.name_key and any(other.name_key == key for other, _ in await repo.lists(workspace_id, lst.kind)):
            raise HTTPException(status_code=409, detail="Another list already has this name")
        lst.name, lst.name_key = " ".join(body.name.split()), key
    if "lead_intel_icp_id" in body.model_fields_set:
        lst.lead_intel_icp_id = body.lead_intel_icp_id
    await repo.save(lst)
    count = next((n for other, n in await repo.lists(workspace_id, lst.kind) if other.id == lst.id), 0)
    return _out(lst, count)


@router.post("/{list_id}/draft-icp", response_model=SmartListOut, dependencies=[Depends(require_manager)])
async def draft_icp(
    list_id: UUID,
    workspace_id: UUID = Depends(require_workspace_id),
    repo: SmartListRepo = Depends(get_smart_list_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    claude: anthropic.AsyncAnthropic | None = Depends(get_optional_anthropic),
) -> SmartListOut:
    """Claude drafts a base ICP from up to 30 of the list's companies. Nothing is created until you create it."""
    if claude is None:
        raise HTTPException(status_code=503, detail=NO_CLAUDE)
    lst = await _list(repo, workspace_id, list_id)
    if lst.kind != "company":
        raise HTTPException(status_code=422, detail="Base ICPs are drafted from company lists.")
    members = [m for m, _ in await repo.members(workspace_id, lst)]
    if not members:
        raise HTTPException(status_code=422, detail="This list has no companies yet.")
    sample = members[:30]
    titles = sorted({p.title for p in await repo.people_at(workspace_id, [c.id for c in sample]) if p.title})[:40]
    model = get_settings().claude_scoring_model
    result = await smart_lists.draft_icp(claude, model, lst.name, [smart_lists.describe_company(c) for c in sample], titles)
    if result.input_tokens:
        await calls.record(
            workspace_id, "claude", "messages", "succeeded" if result.draft else "failed", model=model, cost_usd=result.cost_usd,
            action="Draft ICP", input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        )  # fmt: skip
    if result.draft is None:
        raise HTTPException(status_code=502, detail=result.error or "Claude didn't return a draft.")
    lst.icp_draft = SmartListIcpDraft.model_validate(result.draft.model_dump()).model_dump()
    await repo.save(lst)
    return _out(lst, len(members))


@router.post("/{list_id}/create-icp", response_model=SmartListOut, status_code=201)
async def create_icp_from_list(
    list_id: UUID,
    body: SmartListIcpDraft,
    workspace_id: UUID = Depends(require_workspace_id),
    repo: SmartListRepo = Depends(get_smart_list_repo),
    writer: LeadIntelIcpWriter = Depends(get_lead_intel_icp_writer),
    profiles: IcpProfileRepo = Depends(get_icp_profile_repo),
    sequencer: SequencerClient | None = Depends(get_optional_sequencer),
    links: IcpCampaignRepo = Depends(get_icp_campaign_repo),
    manager: object = Depends(require_manager),
) -> SmartListOut:
    """Create the (edited) draft as a Lead Intel ICP, the same way the ICPs page does, with its description
    and titles on the ICP's profile. Empty fields stay empty."""
    lst = await _list(repo, workspace_id, list_id)
    if lst.lead_intel_icp_id:
        raise HTTPException(status_code=409, detail="This list's ICP was already created.")
    icp_in = IcpCreateIn(
        name=body.name.strip() or lst.name, industry=body.industry.strip() or lst.name, any_of=body.any_of,
        search_terms=body.search_terms, places=body.places,
    )  # fmt: skip
    icp = await create_icp(icp_in, writer, sequencer, links, manager)
    await profiles.upsert(workspace_id, icp.id, {"description": body.description.strip(), "titles": body.titles})
    lst.icp_draft, lst.lead_intel_icp_id = body.model_dump(), icp.id
    await repo.save(lst)
    count = next((n for other, n in await repo.lists(workspace_id, lst.kind) if other.id == lst.id), 0)
    return _out(lst, count)


@router.get("/{list_id}/export.csv", response_class=Response)
async def export_list(
    list_id: UUID, workspace_id: UUID = Depends(require_workspace_id), repo: SmartListRepo = Depends(get_smart_list_repo)
) -> Response:
    """The list in Apollo's people export format. A company list gives the people at its companies, and a row
    with only the company's details for a company with no one found yet."""
    lst = await _list(repo, workspace_id, list_id)
    members = [m for m, _ in await repo.members(workspace_id, lst)]
    if lst.kind == "person":
        rows = await _rows(repo, workspace_id, members)
    else:
        people = await repo.people_at(workspace_id, [c.id for c in members])
        rows = await _rows(repo, workspace_id, people)
        with_people = {p.company_id for p in people}
        rows += [_company_row(c, lst.name) for c in members if c.id not in with_people]
    slug = "".join(ch if ch.isalnum() else "-" for ch in lst.name.lower()).strip("-") or "list"
    return _csv(rows, f"{slug}.csv")


# ---------- Apollo-format rows ----------


def _csv(rows: list[dict[str, str]], filename: str) -> Response:
    return Response(
        content=export_csv(rows), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )  # fmt: skip


def _company_row(c: Company, industry: str | None = None) -> dict[str, str]:
    raw = c.raw or {}
    return {
        "Company Name": c.name, "Company Name for Emails": search_name(c.name), "Website": c.website_url or (f"http://{c.domain}" if c.domain else ""),
        "Company Linkedin Url": c.linkedin_url or "", "Facebook Url": c.facebook_url or "", "Twitter Url": c.twitter_url or "",
        "Company Address": raw.get("address") or "", "Company City": c.city or "", "Company State": c.state or "",
        "Company Country": raw.get("country") or "", "Company Phone": c.phone or "", "Industry": industry or "",
        "Apollo Account Id": c.apollo_org_id or "",
    }  # fmt: skip


async def _rows(repo: SmartListRepo, workspace_id: UUID, people: list[Stakeholder]) -> list[dict[str, str]]:
    """Each person as Apollo's columns: what we know, over the columns their own CSV had (kept as `extra`)."""
    companies = await repo.companies(workspace_id, list({p.company_id for p in people if p.company_id}))
    person_lists = await repo.list_names(workspace_id, "person", [p.id for p in people])
    company_lists = await repo.list_names(workspace_id, "company", list(companies))
    rows = []
    for p in people:
        raw = p.raw or {}
        company = companies.get(p.company_id) if p.company_id else None
        row = dict(raw.get("extra") or {})
        if company:
            row |= {k: v for k, v in _company_row(company, company_lists.get(company.id)).items() if v}
        lists = [x for x in (row.get("Lists"), person_lists.get(p.id), company_lists.get(p.company_id)) if x]
        row |= {k: v for k, v in {
            "First Name": p.first_name, "Last Name": p.last_name, "Title": p.title,
            "Company Name": p.organization_name or raw.get("company"), "Email": p.email, "Email Status": p.email_status,
            "Seniority": p.seniority, "Person Linkedin Url": p.linkedin_url, "City": raw.get("city"), "State": raw.get("state"),
            "Country": raw.get("country"), "Work Direct Phone": raw.get("phone"),
            "Apollo Contact Id": p.external_id if p.source == "apollo" else None,
            "Apollo Account Id": raw.get("apollo_account_id"), "Lists": ", ".join(dict.fromkeys(lists)),
        }.items() if v}  # fmt: skip
        row.setdefault("Company Name for Emails", search_name(row.get("Company Name") or ""))
        if not row.get("Website") and raw.get("website"):
            row["Website"] = raw["website"]
        rows.append(row)
    return rows
