"""Smart lists: companies and people sorted by Claude into high-level groups, like GoHighLevel's smart lists.

Company lists are industries ("Retail", "Manufacturing"), people lists are roles ("Owners & Executives",
"Marketing"). Always the broad group, never a sub-category: a DTF transfer shop is "Retail" or "Printing",
not "Retail - DTF". Claude names the lists, but reuses an existing one whenever it's even remotely similar,
so the lists stay few and stable.

Duplicates aren't sent to Claude twice. Each company or person has a dedupe key (a company's website domain,
Google place, phone, then name and city; a person's email, LinkedIn profile, then name and company), and one
whose key is already sorted joins that list for free.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic
from pydantic import BaseModel

from services.claude import cost_usd, error_message
from services.prompts import render

BATCH = 25  # companies or people per Claude call
SITE_CHARS = 700  # of a company's website text, enough to tell what it does
SITE_READS = 20  # websites read at once while sorting
SITE_SECONDS = 15  # the most one homepage may take; a slow or dead site is sorted from what else we have
CLAUDE_AT_ONCE = 4  # sorting calls in flight after the first (which sets the starting lists alone)


def name_key(name: str) -> str:
    """A list name compared without case, punctuation or "and": "Food & Beverage" = "food and beverage"."""
    words = re.sub(r"[^a-z0-9]+", " ", name.lower().replace("&", " and ")).split()
    return " ".join(w for w in words if w != "and")


def _digits(phone: str | None) -> str:
    d = re.sub(r"\D", "", phone or "")
    return d[-10:] if len(d) >= 10 else ""


def _plain(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def company_key(c: Any) -> str:
    if getattr(c, "domain", None):
        return f"domain:{c.domain.lower()}"
    if getattr(c, "google_place_id", None):
        return f"place:{c.google_place_id}"
    if phone := _digits(getattr(c, "phone", None)):
        return f"phone:{phone}"
    return f"name:{_plain(c.name)}|{_plain(getattr(c, 'city', None))}"


def person_key(p: Any) -> str:
    if getattr(p, "email", None):
        return f"email:{p.email.strip().lower()}"
    if getattr(p, "linkedin_url", None):
        slug = p.linkedin_url.rstrip("/").rsplit("/", 1)[-1].lower()
        return f"linkedin:{slug}"
    return f"name:{_plain(p.first_name)}{_plain(p.last_name)}|{_plain(getattr(p, 'organization_name', None))}"


# ---------- Sorting ----------


class Pick(BaseModel):
    """A person's list."""

    id: str
    list: str
    reason: str


class Sorted(BaseModel):
    items: list[Pick]


class CompanyPick(BaseModel):
    """A company's list, and the ICP it is ("" when none fits)."""

    id: str
    list: str
    icp: str
    reason: str


class CompanySorted(BaseModel):
    items: list[CompanyPick]


@dataclass
class SortSpec:
    """The sorting prompt version in use: what Claude is told, and with which model."""

    system: str
    user: str
    model: str
    max_tokens: int
    version_id: str | None = None


@dataclass
class Choice:
    list: str
    icp: str  # an existing ICP's name, or "" for none
    reason: str


@dataclass
class SortResult:
    picks: dict[str, Choice] = field(default_factory=dict)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)


def icp_text(icp: dict[str, Any]) -> str:
    """One ICP as Claude sees it while sorting."""
    parts = [f"- {icp['name']}"]
    if icp.get("industry"):
        parts.append(f"industry: {icp['industry']}")
    if icp.get("description"):
        parts.append(f"who they are: {icp['description']}")
    words = list(dict.fromkeys([*(icp.get("search_terms") or []), *(icp.get("any_of") or [])]))
    if words:
        parts.append(f"identified by: {', '.join(words[:15])}")
    return "; ".join(parts)


def sort_variables(lists: list[str], icps: list[dict[str, Any]], entries: list[tuple[str, str]]) -> dict[str, str]:
    """The placeholders a sorting prompt can use."""
    return {
        "sort.lists": "\n".join(f"- {n}" for n in lists) or "(none yet)",
        "sort.icps": "\n".join(icp_text(i) for i in icps) or "(none yet)",
        "sort.items": "\n\n".join(f'<item id="{i}">\n{text}\n</item>' for i, text in entries),
    }


async def sort(
    client: anthropic.AsyncAnthropic, spec: SortSpec, kind: str, existing: list[str], icps: list[dict[str, Any]],
    entries: list[tuple[str, str]], on_batch: Callable[[int], Awaitable[None]] | None = None,
) -> SortResult:  # fmt: skip
    """Sort (id, description) entries in batches. Lists started in one batch are offered to the next, so a new
    list is reused, not started twice. An ICP name Claude returns that isn't one of ours counts as none."""
    out = SortResult()
    names = list(existing)
    icp_names = {i["name"].strip().lower(): i["name"] for i in icps}
    output = CompanySorted if kind == "company" else Sorted
    batches = [entries[start : start + BATCH] for start in range(0, len(entries), BATCH)]
    gate, finished = asyncio.Semaphore(CLAUDE_AT_ONCE), 0

    async def one(batch: list[tuple[str, str]]) -> None:
        nonlocal finished
        if out.errors:
            return  # a failed call stops the rest (a bad key or no credit fails them all)
        async with gate:
            user = render(spec.user, sort_variables(names, icps, batch))  # the lists as they are now
            try:
                response = await client.messages.parse(
                    model=spec.model, max_tokens=spec.max_tokens, system=spec.system, output_format=output,
                    messages=[{"role": "user", "content": user}],
                )  # fmt: skip
            except anthropic.APIError as exc:
                out.errors.append(error_message(exc))
                return
        take(batch, response)
        finished += len(batch)
        if on_batch:
            await on_batch(finished)

    def take(batch: list[tuple[str, str]], response: Any) -> None:
        out.calls += 1
        out.input_tokens += response.usage.input_tokens
        out.output_tokens += response.usage.output_tokens
        out.cost_usd += cost_usd(spec.model, response.usage.input_tokens, response.usage.output_tokens)
        wanted = {i for i, _ in batch}
        for pick in (response.parsed_output.items if response.parsed_output else []):
            name = " ".join(pick.list.split())[:60]
            if pick.id not in wanted or not name:
                continue
            # Claude's spelling of an existing list may differ slightly ("Food and Beverage"): use the list's own.
            same = next((n for n in names if name_key(n) == name_key(name)), None)
            if same is None:
                names.append(name)
            icp = icp_names.get((getattr(pick, "icp", "") or "").strip().lower(), "")
            out.picks[pick.id] = Choice(list=same or name, icp=icp, reason=pick.reason.strip()[:200])

    # The first batch alone sets the starting lists, so the ones running together reuse them, not invent their own.
    if batches:
        await one(batches[0])
        await asyncio.gather(*(one(b) for b in batches[1:]))
    out.cost_usd = round(out.cost_usd, 6)
    return out


def describe_company(c: Any) -> str:
    """What Claude sees of a company: its name, where it is, what Google or Apollo call it, and its own words."""
    research = getattr(c, "research", None) or {}
    raw = getattr(c, "raw", None) or {}
    site = ((research.get("website") or {}).get("text") or getattr(c, "_site_text", "") or "")
    lines = [f"Name: {c.name}"]
    if getattr(c, "domain", None):
        lines.append(f"Website: {c.domain}")
    if where := ", ".join(x for x in (getattr(c, "city", None), getattr(c, "state", None)) if x):
        lines.append(f"Location: {where}")
    if kind := raw.get("primary_type") or raw.get("industry"):
        lines.append(f"Listed as: {str(kind).replace('_', ' ')}")
    if category := (research.get("instagram") or {}).get("category"):
        lines.append(f"Instagram category: {category}")
    if keywords := raw.get("keywords"):
        lines.append(f"Keywords: {', '.join(keywords[:15]) if isinstance(keywords, list) else str(keywords)[:200]}")
    if site:
        lines.append(f"From their website: {' '.join(site.split())[:SITE_CHARS]}")
    return "\n".join(lines)


def describe_person(p: Any) -> str:
    extra = ((getattr(p, "raw", None) or {}).get("extra") or {})
    lines = [f"Title: {p.title or 'unknown'}"]
    if getattr(p, "seniority", None):
        lines.append(f"Seniority: {p.seniority}")
    for header in ("Departments", "Sub Departments"):
        if extra.get(header):
            lines.append(f"{header}: {extra[header]}")
    if getattr(p, "organization_name", None):
        lines.append(f"Company: {p.organization_name}")
    return "\n".join(lines)


# ---------- Base ICP drafts ----------


class IcpDraft(BaseModel):
    name: str
    industry: str
    description: str
    search_terms: list[str]
    any_of: list[str]
    titles: list[str]
    places: list[str]


DRAFT_SYSTEM = """You draft a base ideal customer profile (ICP) for a sales team, from one smart list of companies they already found.

Fill in:
- name: short, like the list ("Retail", "Printing shops"). At most 60 characters.
- industry: the one broad industry.
- description: one or two sentences: what these businesses are and do, from the evidence. No claims you can't see.
- search_terms: 3 to 6 phrases someone would type into Google Maps to find more businesses like these.
- any_of: 3 to 10 words or short phrases that identify such a business on its website or listing.
- titles: the job titles to reach at these companies, decision makers first (e.g. Owner, Founder, General Manager).
- places: the US states or cities most of them are in, only if the evidence shows a clear pattern; otherwise empty.
Leave a field empty rather than guess. Use only the evidence given."""


@dataclass
class DraftResult:
    draft: IcpDraft | None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


async def draft_icp(client: anthropic.AsyncAnthropic, model: str, list_name: str, companies: list[str], titles: list[str]) -> DraftResult:
    user = f"Smart list: {list_name}\n\nCompanies in it:\n\n" + "\n\n".join(companies)
    if titles:
        user += "\n\nTitles of people we found at them: " + ", ".join(titles)
    try:
        response = await client.messages.parse(
            model=model, max_tokens=2048, system=DRAFT_SYSTEM, output_format=IcpDraft, messages=[{"role": "user", "content": user}]
        )
    except anthropic.APIError as exc:
        return DraftResult(None, error=error_message(exc))
    usage = response.usage
    cost = round(cost_usd(model, usage.input_tokens, usage.output_tokens), 6)
    if response.parsed_output is None:
        return DraftResult(None, cost, usage.input_tokens, usage.output_tokens, "Claude didn't return a draft.")
    return DraftResult(response.parsed_output, cost, usage.input_tokens, usage.output_tokens)


# ---------- The whole sort: dedupe, websites, Claude, lists, ICPs, drafts ----------

Progress = Callable[[str, int | None, int | None], Awaitable[None]]
MAX_DRAFTS = 10  # base ICPs drafted in one sort, for new lists no ICP fits


@dataclass
class SortOutcome:
    sorted: int = 0
    by_claude: int = 0
    duplicates: int = 0
    new_lists: list[str] = field(default_factory=list)
    sites_read: int = 0
    matched_icp: int = 0  # companies that fit an existing ICP
    drafted: list[str] = field(default_factory=list)  # lists given a draft ICP
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)


async def _nothing(step: str, done: int | None, total: int | None) -> None:
    pass


async def run_sort(
    *, workspace_id: Any, kind: str, members: list[Any], repo: Any, claude: anthropic.AsyncAnthropic, spec: SortSpec,
    icps: list[dict[str, Any]], reader: Any = None, record: Callable[..., Awaitable[None]] | None = None,
    progress: Progress = _nothing,
) -> SortOutcome:  # fmt: skip
    """Sort these companies or people. A duplicate of one already sorted joins its list without asking Claude;
    a company with no research yet has its homepage read first (plain HTTP, Jina when that fails). A company that
    fits an existing ICP is put under it (with its people and leads, where they have none yet). A list that gets
    companies no ICP fits is given a draft base ICP, to create from the Smart lists page."""
    out = SortOutcome()
    known = await repo.sorted_keys(workspace_id, kind)
    key_of = company_key if kind == "company" else person_key
    rows: list[tuple[Any, Any, str, str]] = []
    ask: dict[str, list[Any]] = {}  # dedupe key -> the members sharing it; Claude sorts one of them
    for member in members:
        key = key_of(member)
        if key in known:
            rows.append((member.id, known[key], key, "Same as one already sorted"))
            out.duplicates += 1
        else:
            out.duplicates += key in ask
            ask.setdefault(key, []).append(member)

    if kind == "company" and reader is not None:
        unread = [g[0] for g in ask.values() if g[0].domain and not ((g[0].research or {}).get("website") or {}).get("text")]
        await progress("Reading websites", 0, len(unread))
        gate, done = asyncio.Semaphore(SITE_READS), 0

        async def read(company: Any) -> None:
            nonlocal done
            async with gate:
                try:
                    site = await asyncio.wait_for(reader.read(company.domain, max_pages=1), SITE_SECONDS)
                except TimeoutError:
                    site = None
            if site is not None and site.reached:
                company._site_text = site.text  # only for the description below; never saved
                out.sites_read += 1
            done += 1
            if done % 20 == 0 or done == len(unread):
                await progress("Reading websites", done, len(unread))

        # Plain HTTP requests only, no Jina: its rate limit (about 18 pages a minute without a key) made 1,607 sites
        # take over an hour on 2026-11-02. A site that blocks us or doesn't answer is skipped, and the company is
        # sorted from its name, listing and Apollo's industry and keywords.
        with reader.jina_off():
            await asyncio.gather(*(read(c) for c in unread))

    describe = describe_company if kind == "company" else describe_person
    entries = [(key, describe(group[0])) for key, group in ask.items()]
    existing = [lst.name for lst, _ in await repo.lists(workspace_id, kind)]
    await progress("Sorting with Claude", 0, len(entries))

    async def sorted_so_far(n: int) -> None:
        await progress("Sorting with Claude", n, len(entries))

    result = await sort(claude, spec, kind, existing, icps if kind == "company" else [], entries, sorted_so_far) if entries else SortResult()
    out.errors += result.errors
    out.cost_usd += result.cost_usd
    if result.calls and record:
        await record(spec.model, result.cost_usd, result.input_tokens, result.output_tokens, "Sort smart lists")

    existing_keys = {name_key(n) for n in existing}
    lists: dict[str, Any] = {}
    icp_ids = {i["name"]: i["id"] for i in icps}
    by_icp: dict[Any, list[Any]] = {}
    unmatched: dict[str, int] = {}  # list -> how many of its companies no ICP fits
    for key, choice in result.picks.items():
        nk = name_key(choice.list)
        if nk not in lists:
            lists[nk] = await repo.get_or_create(workspace_id, kind, choice.list, nk)
            if nk not in existing_keys:
                out.new_lists.append(lists[nk].name)
        reason = choice.reason + (f" · ICP: {choice.icp}" if choice.icp else "")
        for member in ask[key]:
            rows.append((member.id, lists[nk].id, key, reason))
        if kind == "company":
            if choice.icp:
                by_icp.setdefault(icp_ids[choice.icp], []).extend(m.id for m in ask[key])
                out.matched_icp += len(ask[key])
            else:
                unmatched[nk] = unmatched.get(nk, 0) + len(ask[key])
    out.by_claude = len(result.picks)
    await repo.add_members(workspace_id, kind, rows)
    out.sorted = len(rows)
    for icp_id, company_ids in by_icp.items():
        await repo.assign_icp(workspace_id, icp_id, company_ids)

    # New ICPs aren't created, only drafted: one per list that got companies no ICP fits.
    # Biggest first, so the lists that matter most get one within the cap; never "Uncategorized".
    ranked = sorted(unmatched, key=lambda nk: -unmatched[nk])
    to_draft = [lists[nk] for nk in ranked
                if nk != name_key("Uncategorized") and not lists[nk].lead_intel_icp_id and not lists[nk].icp_draft][:MAX_DRAFTS]  # fmt: skip
    for i, lst in enumerate(to_draft):
        await progress("Drafting ICPs", i, len(to_draft))
        drafted = await draft_for(workspace_id, lst, repo, claude, spec.model, record)
        if drafted.error:
            out.errors.append(f"{lst.name}: {drafted.error}")
        else:
            out.drafted.append(lst.name)
        out.cost_usd += drafted.cost_usd
    out.cost_usd = round(out.cost_usd, 6)
    return out


async def draft_for(workspace_id: Any, lst: Any, repo: Any, claude: anthropic.AsyncAnthropic, model: str,
                    record: Callable[..., Awaitable[None]] | None = None) -> "DraftResult":  # fmt: skip
    """Draft a base ICP from up to 30 of a list's companies and keep it on the list."""
    sample = [m for m, _ in await repo.members(workspace_id, lst)][:30]
    if not sample:
        return DraftResult(None, error="This list has no companies yet.")
    titles = sorted({p.title for p in await repo.people_at(workspace_id, [c.id for c in sample]) if p.title})[:40]
    result = await draft_icp(claude, model, lst.name, [describe_company(c) for c in sample], titles)
    if result.input_tokens and record:
        await record(model, result.cost_usd, result.input_tokens, result.output_tokens, "Draft ICP")
    if result.draft is not None:
        lst.icp_draft = result.draft.model_dump()
        await repo.save(lst)
    return result
