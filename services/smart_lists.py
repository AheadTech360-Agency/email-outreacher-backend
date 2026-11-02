"""Smart lists: companies and people sorted by Claude into high-level groups, like GoHighLevel's smart lists.

Company lists are industries ("Retail", "Manufacturing"), people lists are roles ("Owners & Executives",
"Marketing"). Always the broad group, never a sub-category: a DTF transfer shop is "Retail" or "Printing",
not "Retail - DTF". Claude names the lists, but reuses an existing one whenever it's even remotely similar,
so the lists stay few and stable.

Duplicates aren't sent to Claude twice. Each company or person has a dedupe key (a company's website domain,
Google place, phone, then name and city; a person's email, LinkedIn profile, then name and company), and one
whose key is already sorted joins that list for free.
"""

import re
from dataclasses import dataclass, field
from typing import Any

import anthropic
from pydantic import BaseModel

from services.claude import cost_usd, error_message

BATCH = 25  # companies or people per Claude call
SITE_CHARS = 700  # of a company's website text, enough to tell what it does


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
    id: str
    list: str
    reason: str


class Sorted(BaseModel):
    items: list[Pick]


SYSTEM = {
    "company": """You sort businesses into smart lists for a sales team. Each list is one high-level industry, named in one to three plain words, like "Retail", "Manufacturing", "Healthcare", "Construction", "Food & Beverage", "Printing", "Professional Services", "Real Estate", "Automotive", "Education", "Nonprofit".

Rules:
- Always the broad industry, never a sub-category or a combination. "Retail", never "Retail - DTF", "Apparel Retail" or "Online T-shirt Store".
- If an existing list is even remotely similar, use it, spelled exactly as given. Only start a new list when nothing existing fits at all.
- Judge from what the business does and sells, from its own words first. When there's too little to tell, use "Uncategorized".
- reason: a few words naming the evidence, e.g. "sells custom DTF transfers online".
Return one item for every id you're given, with the same id.""",
    "person": """You sort people into smart lists for a sales team. Each list is one high-level role, named in one to three plain words, like "Owners & Executives", "Sales", "Marketing", "Operations", "Finance", "Technology", "Human Resources", "Customer Service", "Production".

Rules:
- Always the broad role, never a specific title or seniority split. "Marketing", never "Senior Marketing Manager" or "Marketing - Social".
- Owners, founders, presidents, CEOs and other top executives are "Owners & Executives".
- If an existing list is even remotely similar, use it, spelled exactly as given. Only start a new list when nothing existing fits at all.
- When there's too little to tell, use "Uncategorized".
- reason: a few words naming the evidence, e.g. "title: Head of Growth".
Return one item for every id you're given, with the same id.""",
}


@dataclass
class SortResult:
    picks: dict[str, Pick] = field(default_factory=dict)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)


def _user(kind: str, existing: list[str], entries: list[tuple[str, str]]) -> str:
    lists = "\n".join(f"- {n}" for n in existing) or "(none yet)"
    who = "businesses" if kind == "company" else "people"
    body = "\n\n".join(f'<item id="{i}">\n{text}\n</item>' for i, text in entries)
    return f"Existing lists:\n{lists}\n\nSort these {who}:\n\n{body}"


async def sort(
    client: anthropic.AsyncAnthropic, model: str, kind: str, existing: list[str], entries: list[tuple[str, str]]
) -> SortResult:
    """Sort (id, description) entries in batches. Lists started in one batch are offered to the next, so a
    new list is reused, not started twice."""
    out = SortResult()
    names = list(existing)
    for start in range(0, len(entries), BATCH):
        batch = entries[start : start + BATCH]
        try:
            response = await client.messages.parse(
                model=model, max_tokens=4096, system=SYSTEM[kind], output_format=Sorted,
                messages=[{"role": "user", "content": _user(kind, names, batch)}],
            )  # fmt: skip
        except anthropic.APIError as exc:
            out.errors.append(error_message(exc))
            break
        out.calls += 1
        out.input_tokens += response.usage.input_tokens
        out.output_tokens += response.usage.output_tokens
        out.cost_usd += cost_usd(model, response.usage.input_tokens, response.usage.output_tokens)
        wanted = {i for i, _ in batch}
        for pick in (response.parsed_output.items if response.parsed_output else []):
            name = " ".join(pick.list.split())[:60]
            if pick.id not in wanted or not name:
                continue
            # Claude's spelling of an existing list may differ slightly ("Food and Beverage"): use the list's own.
            same = next((n for n in names if name_key(n) == name_key(name)), None)
            if same is None:
                names.append(name)
            out.picks[pick.id] = Pick(id=pick.id, list=same or name, reason=pick.reason.strip()[:200])
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
