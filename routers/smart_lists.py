"""Smart lists: companies and people sorted by Claude into high-level groups (services/smart_lists.py), each
company list with a draft base ICP to create in Lead Intel, and Apollo-format CSV downloads.

Sorting spends (Claude, and Jina for a site our own fetch can't read), so it and drafting are for Management
and admins; anyone in the workspace can see the lists and download them."""

from typing import Any, Literal
from uuid import UUID

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from config import get_settings
from background import report
from crud import IcpCampaignRepo, IcpProfileRepo, PromptRepo, ProviderCallRepo, SmartListRepo
from services.access import AccessRepo, Scope
from dependencies import (
    get_access_repo, get_scope, require_runner,
    get_icp_campaign_repo, get_icp_profile_repo, get_lead_intel_icp_writer, get_lead_intel_repo, get_optional_anthropic,
    get_optional_sequencer, get_prompt_repo, get_provider_call_repo, get_smart_list_repo, get_website_reader, require_manager, require_workspace_id,
)  # fmt: skip
from lead_intel import LeadIntelRepo
from lead_intel_writes import LeadIntelIcpWriter
from routers.icps import create_icp
from models import Company, SmartList, Stakeholder
from schemas import (
    CandidateIdOut, CandidateOut, CandidatesPageOut, IcpCreateIn, MoveMembersIn, MoveMembersResult, SmartListCompanyOut, SmartListFactsOut, SmartListDetailOut, SmartListIcpDraft, SmartListOut, SmartListPersonOut, SmartListsSummary,
    SmartListCreateIn, SmartListUpdateIn, SortListsIn, SortListsResult,
)  # fmt: skip
from services import smart_lists
from services.apify import search_name
from services.people_csv import export_csv
from services.prompts import DEFAULTS
from services.sending import SequencerClient
from services.website import WebsiteReader

router = APIRouter(prefix="/smart-lists", tags=["smart lists"])

NO_CLAUDE = "ANTHROPIC_API_KEY is not set"


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
    scope: Scope = Depends(get_scope),
) -> SmartListsSummary:
    """The lists of this kind. A Prospector gets theirs and those shared with them, each saying if they may edit it,
    and "not in a list" counts only the companies (or people) they may see."""
    only = await scope.ids("smart_list")
    lists = [_out(lst, n).model_copy(update=await scope.flags("smart_list", lst.id))
             for lst, n in await repo.lists(workspace_id, kind, only)]  # fmt: skip
    visible = scope.companies() if kind == "company" else scope.people()
    return SmartListsSummary(lists=lists, unsorted=await repo.count_unsorted(workspace_id, kind, visible))


async def sort_context(
    workspace_id: UUID, kind: str, prompts: PromptRepo, lead_intel: LeadIntelRepo, profiles: IcpProfileRepo
) -> tuple[smart_lists.SortSpec, list[dict[str, Any]]]:
    """The sorting prompt in use (the workspace's default of its kind, editable on Prompts) and, for companies,
    the ICPs a company can be matched to: the enabled ones, with their descriptions."""
    prompt_kind = "sort" if kind == "company" else "sort_people"
    version = await prompts.for_icp(workspace_id, prompt_kind, None)
    if version is None:
        d = DEFAULTS[prompt_kind]
        spec = smart_lists.SortSpec(d.system_prompt, d.user_prompt, d.model, d.max_tokens)
    else:
        spec = smart_lists.SortSpec(version.system_prompt, version.user_prompt, version.model, version.max_tokens, str(version.id))
    if kind != "company":
        return spec, []
    icps = [i for i in await lead_intel.list_icps() if i.get("enabled", True)]
    by_id = await profiles.get_many(workspace_id, [i["id"] for i in icps])
    return spec, [{**i, "description": getattr(by_id.get(i["id"]), "description", "") or ""} for i in icps]


def recorder(calls: ProviderCallRepo, workspace_id: UUID):
    """Log a Claude call's cost to Usage."""

    async def record(model: str, cost: float, input_tokens: int, output_tokens: int, action: str) -> None:
        await calls.record(
            workspace_id, "claude", "messages", "succeeded", model=model, cost_usd=cost, action=action,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )  # fmt: skip

    return record


def sort_out(outcome: smart_lists.SortOutcome, left: int) -> SortListsResult:
    return SortListsResult(
        sorted=outcome.sorted, by_claude=outcome.by_claude, duplicates=outcome.duplicates, new_lists=outcome.new_lists,
        sites_read=outcome.sites_read, matched_icp=outcome.matched_icp, drafted=outcome.drafted, left=left,
        cost_usd=outcome.cost_usd, errors=outcome.errors,
    )  # fmt: skip


@router.post("/sort", response_model=SortListsResult, dependencies=[Depends(require_manager)])
async def sort_lists(
    body: SortListsIn,
    workspace_id: UUID = Depends(require_workspace_id),
    repo: SmartListRepo = Depends(get_smart_list_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    prompts: PromptRepo = Depends(get_prompt_repo),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    profiles: IcpProfileRepo = Depends(get_icp_profile_repo),
    claude: anthropic.AsyncAnthropic | None = Depends(get_optional_anthropic),
    reader: WebsiteReader = Depends(get_website_reader),
) -> SortListsResult:
    """Sort companies or people not in a list yet (services.smart_lists.run_sort): lists, and for companies the
    ICP each one fits, or a draft ICP for a list none fits."""
    if claude is None:
        raise HTTPException(status_code=503, detail=NO_CLAUDE)
    spec, icps = await sort_context(workspace_id, body.kind, prompts, lead_intel, profiles)
    members = await repo.unsorted(workspace_id, body.kind, body.limit)
    outcome = await smart_lists.run_sort(
        workspace_id=workspace_id, kind=body.kind, members=members, repo=repo, claude=claude, spec=spec, icps=icps,
        reader=reader, record=recorder(calls, workspace_id), progress=report,
    )  # fmt: skip
    return sort_out(outcome, await repo.count_unsorted(workspace_id, body.kind))


@router.get("/memberships", response_model=dict[UUID, UUID])
async def memberships(
    kind: Literal["company", "person"] = Query(),
    workspace_id: UUID = Depends(require_workspace_id),
    repo: SmartListRepo = Depends(get_smart_list_repo),
) -> dict[UUID, UUID]:
    """Which list each company or person is in (member id to list id), for pickers that add them to a list."""
    return await repo.memberships(workspace_id, kind)


FACT_KEYS = ("processed", "contacted", "verified", "emails")


def _candidate_filters(
    kind: Literal["company", "person"] = Query(),
    list_id: UUID = Query(alias="listId"),
    icp: str | None = Query(default=None, pattern="^(all|none|[0-9a-fA-F-]{36})$"),
    in_list: str | None = Query(default=None, alias="from", pattern="^(all|none|[0-9a-fA-F-]{36})$"),
    q: str | None = Query(default=None, max_length=200),
    processed: str | None = Query(default=None, pattern="^(all|yes|no)$"),
    contacted: str | None = Query(default=None, pattern="^(all|yes|no)$"),
    verified: str | None = Query(default=None, pattern="^(all|yes|no)$"),
    emails: str | None = Query(default=None, pattern="^(all|yes|no)$"),
) -> dict[str, Any]:
    facets = {k: v for k, v in zip(FACT_KEYS, (processed, contacted, verified, emails), strict=True) if v}
    return {"kind": kind, "list_id": list_id, "icp": icp, "in_list": in_list, "facets": facets, "q": q}


async def _scoped(f: dict[str, Any], scope: Scope) -> dict[str, Any]:
    """The Add dialog's filters, for a Prospector only over what they may see and from lists they can see."""
    await scope.check("smart_list", f["list_id"])
    if f["in_list"] and f["in_list"] not in ("all", "none"):
        await scope.check("smart_list", UUID(f["in_list"]))
    return {**f, "scope": scope.companies() if f["kind"] == "company" else scope.people()}


@router.get("/candidates", response_model=CandidatesPageOut)
async def candidates(
    f: dict[str, Any] = Depends(_candidate_filters),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    workspace_id: UUID = Depends(require_workspace_id),
    repo: SmartListRepo = Depends(get_smart_list_repo),
    scope: Scope = Depends(get_scope),
) -> CandidatesPageOut:
    """One page of the companies or people that can be added to a list (not already in it), filtered in the database."""
    rows, total = await repo.candidates(workspace_id, limit=limit, offset=offset, **await _scoped(f, scope))
    return CandidatesPageOut(items=[CandidateOut(id=i, name=n, detail=d or "", icp_id=icp, list_id=cur) for i, n, d, icp, cur in rows], total=total)


@router.get("/candidates/ids", response_model=list[CandidateIdOut])
async def candidate_ids(
    f: dict[str, Any] = Depends(_candidate_filters),
    workspace_id: UUID = Depends(require_workspace_id),
    repo: SmartListRepo = Depends(get_smart_list_repo),
    scope: Scope = Depends(get_scope),
) -> list[CandidateIdOut]:
    """Every matching candidate's id and current list (Select all)."""
    return [CandidateIdOut(id=i, list_id=cur) for i, cur in await repo.candidate_ids(workspace_id, cap=20000, **await _scoped(f, scope))]


@router.get("/facts", response_model=SmartListFactsOut)
async def facts(
    kind: Literal["company", "person"] = Query(),
    workspace_id: UUID = Depends(require_workspace_id),
    repo: SmartListRepo = Depends(get_smart_list_repo),
) -> SmartListFactsOut:
    """Which companies (or people) were written to, sent or queued, have a verified email and have any email (for a
    company, anyone at it), for pickers that filter by them."""
    return SmartListFactsOut(**await repo.facts(workspace_id, kind))


@router.get("/people.csv", response_class=Response)
async def export_people(
    workspace_id: UUID = Depends(require_workspace_id), repo: SmartListRepo = Depends(get_smart_list_repo)
) -> Response:
    """Every person in the workspace, in Apollo's people export format."""
    people = await repo.all_people(workspace_id)
    return _csv(await _rows(repo, workspace_id, people), "people.csv")


@router.post("/move", response_model=MoveMembersResult, dependencies=[Depends(require_runner)])
async def move_members(
    body: MoveMembersIn, workspace_id: UUID = Depends(require_workspace_id), repo: SmartListRepo = Depends(get_smart_list_repo),
    scope: Scope = Depends(get_scope),
) -> MoveMembersResult:
    """Move companies or people to another list, an existing one or a new one named here. A company moved to a list
    whose ICP is created doesn't change ICP: that's set on the company. A Prospector moves only companies or people
    they may see, into a list they may edit, and out of no list but one they may edit (or none)."""
    if bool(body.to_list_id) == bool(body.new_list_name and body.new_list_name.strip()):
        raise HTTPException(status_code=422, detail="Pick a list to move them to, or name a new one.")
    if body.to_list_id:
        await scope.check("smart_list", body.to_list_id, edit=True)
        target = await _list(repo, workspace_id, body.to_list_id)
        if target.kind != body.kind:
            raise HTTPException(status_code=422, detail="That list is for the other kind (companies or people).")
    else:
        name = " ".join(body.new_list_name.split())
        key = smart_lists.name_key(name)
        if scope.limited and any(other.name_key == key for other, _ in await repo.lists(workspace_id, body.kind)):
            raise HTTPException(status_code=409, detail="A list with this name already exists. Pick another name.")
        target = await repo.get_or_create(workspace_id, body.kind, name, key)
        # The new list is committed with the move (or now, by scope.created, when a Prospector owns it).
        await scope.created("smart_list", target.id)
    ids = list(dict.fromkeys(body.member_ids))
    if scope.limited:
        ids = await scope.visible_members(repo.session, body.kind, ids)
        held = await repo.memberships(workspace_id, body.kind)
        ids = [i for i in ids if held.get(i) is None or await scope.can_edit("smart_list", held[i])]
    moved = await repo.move(workspace_id, body.kind, ids, target.id)
    return MoveMembersResult(moved=moved, to_list_id=target.id, to_list_name=target.name)


PAGE_SIZES = (25, 50, 100, 200)


@router.get("/{list_id}", response_model=SmartListDetailOut)
async def get_list(
    list_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, alias="pageSize"),
    q: str = Query(default="", max_length=200),
    processed: str = Query(default="", pattern="^(|yes|no)$"),
    emails: str = Query(default="", pattern="^(|yes|no)$"),
    contacted: str = Query(default="", pattern="^(|yes|no)$"),
    verified: str = Query(default="", pattern="^(|yes|no)$"),
    workspace_id: UUID = Depends(require_workspace_id),
    repo: SmartListRepo = Depends(get_smart_list_repo),
    scope: Scope = Depends(get_scope),
) -> SmartListDetailOut:
    """A list with one page of its members (50 by default), optionally searched by name and filtered by whether they
    were written to yet, sent or queued, and have a verified email or any email (for a company, anyone at it), together."""
    if page_size not in PAGE_SIZES:
        raise HTTPException(status_code=422, detail=f"pageSize is one of {', '.join(map(str, PAGE_SIZES))}")
    await scope.check("smart_list", list_id)
    lst = await _list(repo, workspace_id, list_id)
    members, matching = await repo.members_page(workspace_id, lst, (page - 1) * page_size, page_size, q,
                                               {"processed": processed, "emails": emails, "contacted": contacted, "verified": verified})
    written, with_email = await repo.member_facts(workspace_id, lst.kind, [m.id for m, _ in members])
    count = next((n for other, n in await repo.lists(workspace_id, lst.kind) if other.id == lst.id), 0)
    companies, people = [], []
    for m, member in members:
        if lst.kind == "company":
            companies.append(SmartListCompanyOut(id=m.id, name=m.name, domain=m.domain, city=m.city, state=m.state, source=m.source,
                                                 status=m.status, reason=member.reason, processed=m.id in written,
                                                 people_with_email=with_email.get(m.id, 0)))  # fmt: skip
        else:
            people.append(SmartListPersonOut(id=m.id, name=_name(m), title=m.title, company=m.organization_name, email=m.email,
                                             email_status=m.email_status, linkedin_url=m.linkedin_url, reason=member.reason,
                                             processed=m.id in written))  # fmt: skip
    return SmartListDetailOut(**_out(lst, count).model_dump(exclude={"can_edit", "is_owner"}), companies=companies, people=people, page=page,
                              page_size=page_size, matching=matching, **await scope.flags("smart_list", lst.id))  # fmt: skip


@router.post("", response_model=SmartListOut, status_code=201, dependencies=[Depends(require_runner)])
async def create_list(
    body: SmartListCreateIn, workspace_id: UUID = Depends(require_workspace_id), repo: SmartListRepo = Depends(get_smart_list_repo),
    scope: Scope = Depends(get_scope),
) -> SmartListOut:
    """An empty list to move companies or people into. Sorting also puts new ones in it when they fit."""
    name = " ".join(body.name.split())
    key = smart_lists.name_key(name)
    if any(other.name_key == key for other, _ in await repo.lists(workspace_id, body.kind)):
        raise HTTPException(status_code=409, detail="A list with this name already exists")
    lst = await repo.get_or_create(workspace_id, body.kind, name, key)
    await repo.save(lst)  # get_or_create leaves the commit to its caller
    await scope.created("smart_list", lst.id)
    return _out(lst, 0).model_copy(update={"can_edit": True})


@router.delete("/{list_id}", status_code=204, dependencies=[Depends(require_runner)])
async def delete_list(
    list_id: UUID, workspace_id: UUID = Depends(require_workspace_id), repo: SmartListRepo = Depends(get_smart_list_repo),
    scope: Scope = Depends(get_scope), access: AccessRepo = Depends(get_access_repo),
) -> None:
    """Delete a list. Its companies or people are kept, back in "not in a list"; an ICP created from it stays. A
    Prospector deletes only a list they created."""
    await scope.check("smart_list", list_id, owner=True)
    await repo.delete(await _list(repo, workspace_id, list_id))
    await access.forget("smart_list", list_id)


@router.patch("/{list_id}", response_model=SmartListOut, dependencies=[Depends(require_runner)])
async def update_list(
    list_id: UUID, body: SmartListUpdateIn, workspace_id: UUID = Depends(require_workspace_id),
    repo: SmartListRepo = Depends(get_smart_list_repo), scope: Scope = Depends(get_scope),
) -> SmartListOut:  # fmt: skip
    """Rename a list, or set its Lead Intel ICP (created from its draft, or an existing one). A Prospector changes
    only a list they may edit, and gives it only an ICP they can see."""
    await scope.check("smart_list", list_id, edit=True)
    if "lead_intel_icp_id" in body.model_fields_set and scope.limited:
        # Giving a list an ICP moves its companies under it (or out from under the one Management chose).
        raise HTTPException(status_code=403, detail="Only Management changes which ICP a list's companies are under.")
    lst = await _list(repo, workspace_id, list_id)
    if body.name is not None:
        key = smart_lists.name_key(body.name)
        if key != lst.name_key and any(other.name_key == key for other, _ in await repo.lists(workspace_id, lst.kind)):
            raise HTTPException(status_code=409, detail="Another list already has this name")
        lst.name, lst.name_key = " ".join(body.name.split()), key
    if "lead_intel_icp_id" in body.model_fields_set:
        lst.lead_intel_icp_id = body.lead_intel_icp_id
    await repo.save(lst)
    if body.lead_intel_icp_id and lst.kind == "company":
        # Giving a company list an ICP puts its companies (with their people) under it, where they have none yet.
        await repo.assign_icp(workspace_id, body.lead_intel_icp_id, [c.id for c, _ in await repo.members(workspace_id, lst)])
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
    result = await smart_lists.draft_for(workspace_id, lst, repo, claude, DEFAULTS["sort"].model, recorder(calls, workspace_id))
    if result.draft is None:
        raise HTTPException(status_code=422 if result.input_tokens == 0 else 502, detail=result.error or "Claude didn't return a draft.")
    count = next((n for other, n in await repo.lists(workspace_id, lst.kind) if other.id == lst.id), 0)
    return _out(lst, count)


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
    and titles on the ICP's profile. Empty fields stay empty. The list's companies without an ICP get this one."""
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
    # The list's companies (with their people and leads) go under it, where they have no ICP yet.
    await repo.assign_icp(workspace_id, icp.id, [c.id for c, _ in await repo.members(workspace_id, lst)])
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
