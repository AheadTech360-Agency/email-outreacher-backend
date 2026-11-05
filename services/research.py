"""Researching Apollo companies against an ICP: read, check, score, decide.

For each company:
  1. read its website (our polite crawl, Jina as the fallback) and the socials it links to;
  2. read its Instagram profile, if the website links one (one Apify run for the whole batch);
  3. check the ICP's rule (Lead Intel's matcher, services/icp_rules.py);
  4. if it passes, score it with Claude (services/scoring.py);
  5. with auto-approve on, approved (qualified) when it passes the rule and scores at least the threshold;
     otherwise it waits for a person to approve or reject it (review), with the reason. Only a person rejects.
     A company someone kept or dropped by hand keeps that decision.

Lead Intel's companies never come through here: Lead Intel already verified them.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from models import Company
from services.apify import ApifyClient, ApifyError, FetchedPost, InstagramProfile, instagram_handle
from services.contacts import extract_contacts
from services.icp_rules import Evidence, check
from services.scoring import CompanyScorer, MoreReading, PromptSpec
from services.website import AI_BUDGET_SECONDS, AI_MAX_PAGES, SiteRead, WebsiteReader, normalize_domain

READ_CONCURRENCY = 3  # sites read at once; each site is still read one page at a time


@dataclass
class ResearchSummary:
    qualified: int = 0
    not_fit: int = 0
    review: int = 0
    errors: int = 0
    apify_cost_usd: float = 0.0
    # Each Apify run as (run id, cost), logged one by one so its cost can be corrected once Apify settles it.
    apify_runs: list[tuple[str | None, float | None]] = field(default_factory=list)
    # What was read, for the caller to store: full page texts and the company's Instagram posts.
    pages: dict[UUID, tuple[str, list[dict]]] = field(default_factory=dict)  # company id -> (via, pages)
    posts: dict[UUID, list[FetchedPost]] = field(default_factory=dict)
    claude_calls: int = 0
    claude_model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    notes: list[str] = field(default_factory=list)


def _apollo_facts(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    keep = ("industry", "keywords", "short_description", "seo_description", "founded_year", "estimated_num_employees",
            "city", "state", "country")  # fmt: skip
    return {k: raw[k] for k in keep if raw.get(k) not in (None, "", [])}


def _evidence(company: Company, site: SiteRead | None, ig: InstagramProfile | None) -> Evidence:
    return Evidence(
        business_name=company.name,
        website_text=site.text if site and site.reached else None,
        website_domain=company.domain,
        instagram_bio=ig.bio if ig else None,
        instagram_category=ig.category if ig else None,
        instagram_handle=ig.handle if ig else None,
        instagram_followers=ig.followers if ig else None,
        has_website=bool(site and site.reached),
        city=company.city,
        state=company.state,
    )


def _score_evidence(company: Company, site: SiteRead | None, ig: InstagramProfile | None) -> dict[str, Any]:
    """What the Company Verification prompt's {{company.*}} placeholders are filled from."""
    return {
        "pages": [{"url": p["url"], "text": p.get("text") or ""} for p in site.pages] if site and site.reached else [],
        "name": company.name,
        "domain": company.domain,
        "location": ", ".join(p for p in (company.city, company.state) if p),
        "socials": {k: v for k, v in {
            "instagram": company.instagram_url, "facebook": company.facebook_url,
            "linkedin": company.linkedin_url, "x": company.twitter_url,
        }.items() if v},
        "website_text": "\n".join(x for x in (site.title, site.text) if x) if site and site.reached else None,
        "instagram": None if ig is None else {
            "handle": ig.handle, "name": ig.full_name, "bio": ig.bio, "category": ig.category,
            "followers": ig.followers, "private": ig.private,
        },
        # The company's own latest Instagram posts, for {{company.posts}}.
        "posts": [] if ig is None else [
            {"source": p.source, "date": p.posted_at.date().isoformat() if p.posted_at else "", "text": p.text}
            for p in ig.posts
        ],
        "apollo": _apollo_facts(company.raw),
    }  # fmt: skip


async def research_companies(
    companies: list[Company],
    icp: dict[str, Any],
    profile: dict[str, Any] | None,
    reader: WebsiteReader,
    apify: ApifyClient | None,
    scorer: CompanyScorer | None,
    threshold: int,
    prompt: PromptSpec | None = None,
    max_pages: int = AI_MAX_PAGES,
    max_seconds: float = AI_BUDGET_SECONDS,
    read_website: bool = True,
    unscored: str = "ANTHROPIC_API_KEY is not set",
    auto_approve: bool = True,
) -> ResearchSummary:
    """`read_website` False: the ICP has its website switched off as a verification source. `unscored`:
    why there's no Claude score when `scorer` is None."""
    summary = ResearchSummary()
    gate = asyncio.Semaphore(READ_CONCURRENCY)

    async def read(company: Company) -> SiteRead | None:
        target = company.domain or normalize_domain(company.website_url)
        if not target or not read_website:
            return None
        async with gate:
            # With Claude, only the homepage now: Claude chooses each further page (see decide).
            return await reader.read(target, max_pages=1) if scorer is not None else await reader.read(target)

    sites = await asyncio.gather(*(read(c) for c in companies))

    # Instagram: the account the company's own website links to (never a search).
    for company, site in zip(companies, sites, strict=True):
        if site and site.socials:
            company.instagram_url = company.instagram_url or site.socials.get("instagram")
            company.facebook_url = company.facebook_url or site.socials.get("facebook")
            company.linkedin_url = company.linkedin_url or site.socials.get("linkedin")
            company.twitter_url = company.twitter_url or site.socials.get("x")
    handles = {c.id: h for c in companies if (h := instagram_handle(c.instagram_url))}
    profiles: dict[str, InstagramProfile] = {}
    if handles and apify is not None:
        try:
            profiles, run = await apify.instagram_profiles(list(handles.values()))
            summary.apify_cost_usd += run.cost_usd or 0
            summary.apify_runs.append((run.run_id, run.cost_usd))
            # A stored handle Instagram doesn't know (renamed, or wrong where it came from): try the
            # account the company's own website links to, when that's a different one.
            retry = {}
            for company, site in zip(companies, sites, strict=True):
                linked = instagram_handle((site.socials if site else {}).get("instagram"))
                if company.id in handles and handles[company.id] not in profiles and linked and linked != handles[company.id]:
                    retry[company.id] = linked
            if retry:
                more, run = await apify.instagram_profiles(list(retry.values()))
                summary.apify_cost_usd += run.cost_usd or 0
                summary.apify_runs.append((run.run_id, run.cost_usd))
                profiles.update(more)
                for company in companies:
                    if company.id in retry and retry[company.id] in more:
                        handles[company.id] = retry[company.id]
                        company.instagram_url = f"https://www.instagram.com/{retry[company.id]}/"
            missing = [f"@{h}" for h in handles.values() if h not in profiles]
            if missing:
                summary.notes.append(f"Instagram couldn't find {', '.join(missing)} (renamed, private or removed).")
        except ApifyError as exc:
            summary.notes.append(f"Instagram profiles weren't read: {exc.message}")
    elif handles:
        summary.notes.append("Instagram profiles weren't read: APIFY_TOKEN is not set")

    places_by_search = bool([p for p in icp.get("places") or [] if p.strip()])

    async def read_with_claude(company: Company, site: SiteRead, ig: InstagramProfile | None):
        """Score from the homepage; while Claude asks for another page (and the limits allow), read it
        and score again. Returns the last result and the pages it chose, in order, with its reasons."""
        chosen: list[dict] = []
        while True:
            more = (
                MoreReading(site.candidates, len(site.pages), max_pages)
                if reader.can_read_more(site, max_pages, max_seconds)
                else None
            )
            result = await scorer.score(icp, profile, _score_evidence(company, site, ig), prompt, more)
            summary.claude_calls += 1
            summary.claude_model = result.model
            summary.input_tokens += result.input_tokens
            summary.output_tokens += result.output_tokens
            if result.error or more is None or not result.next_page:
                return result, chosen
            page = await reader.read_more(site, result.next_page, result.next_reason, max_pages, max_seconds)
            chosen.append({"url": result.next_page, "reason": result.next_reason, "read": page is not None})
            if page is None:
                # It couldn't be read: ask once more without it, then stop.
                if len([c for c in chosen if not c["read"]]) > 1:
                    return result, chosen

    async def decide(company: Company, site: SiteRead | None) -> None:
        ig = profiles.get(handles.get(company.id, ""))
        reading = None
        if scorer is not None and site is not None and site.reached:
            reading = await read_with_claude(company, site, ig)
        rule = check(icp, _evidence(company, site, ig), places_by_search=places_by_search)
        company.rule_fit = rule.fit
        company.rule_detail = {"reason": rule.reason, "hits": rule.hits, "skipped": rule.skipped}
        company.research = {
            "website": None if site is None else {
                "outcome": site.outcome, "via": site.via, "url": site.resolved_url, "title": site.title,
                "text": site.text, "detail": site.detail,
                # Full page texts go to eo_company_pages; only where they came from is kept here,
                # with why Claude chose each page after the homepage.
                "pages": [{k: v for k, v in page.items() if k != "text"} for page in site.pages],
            },
            "socials": {k: v for k, v in {
                "instagram": company.instagram_url, "facebook": company.facebook_url,
                "linkedin": company.linkedin_url, "x": company.twitter_url,
            }.items() if v},
            "instagram": None if ig is None else {
                "handle": ig.handle, "name": ig.full_name, "bio": ig.bio, "category": ig.category,
                "followers": ig.followers, "private": ig.private, "verified": ig.verified, "raw": ig.raw,
            },
        }  # fmt: skip
        # What the site publishes for reaching them, as Lead Intel took it (services/contacts.py).
        if site and site.reached:
            contacts = extract_contacts(site.pages, site.mailtos, site.tels, site.owner_names, company.domain)
            company.research = {**company.research, "contacts": None if contacts.empty else contacts.as_dict()}
            company.phone = company.phone or contacts.phone
        if site and site.reached and site.pages:
            summary.pages[company.id] = (site.via or "own_crawl", site.pages)
        if ig and ig.posts:
            summary.posts[company.id] = ig.posts
        company.researched_at = datetime.now(UTC)

        def keep_score(result, chosen: list[dict]) -> None:
            company.score = result.score
            company.score_detail = {
                "summary": result.summary, "reasons": result.reasons, "concerns": result.concerns,
                "model": result.model, "error": result.error,
                "prompt_version_id": prompt.version_id if prompt else None,
                "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                # The pages Claude asked to read after the homepage, in order, and why.
                "reading": chosen,
            }  # fmt: skip

        unread = "" if site and site.reached else (" (their website couldn't be read)" if read_website else " (website not read: off for this ICP)")
        if not rule.fit:
            status, reason = "review", f"Doesn't match the ICP's rule: {rule.reason}{unread}"
            # Claude read the site before the rule ran: keep what it said, the rule still decides.
            if reading is None:
                company.score, company.score_detail = None, None
            else:
                keep_score(*reading)
        elif scorer is None:
            status = "qualified" if auto_approve else "review"
            reason = f"Passes the ICP's rule: {rule.reason}. Not scored: {unscored}"
        else:
            if reading is None:
                # The site couldn't be read: Claude scores from Instagram and Apollo alone.
                result = await scorer.score(icp, profile, _score_evidence(company, site, ig), prompt)
                summary.claude_calls += 1
                summary.claude_model = result.model
                summary.input_tokens += result.input_tokens
                summary.output_tokens += result.output_tokens
                keep_score(result, [])
            else:
                result, chosen = reading
                keep_score(result, chosen)
            if result.error:
                status, reason = "error", result.error
            elif auto_approve and result.score is not None and result.score >= threshold:
                status, reason = "qualified", f"Scored {result.score}: {result.summary}"
            elif result.score is not None and result.score >= threshold:
                status, reason = "review", f"Scored {result.score}: {result.summary}"
            else:
                status, reason = "review", f"Scored {result.score}, under {threshold}: {result.summary}"

        if not company.overridden:
            company.status, company.status_reason = status, reason
        summary.qualified += status == "qualified"
        summary.not_fit += status == "not_fit"
        summary.review += status == "review"
        summary.errors += status == "error"

    await asyncio.gather(*(decide(c, s) for c, s in zip(companies, sites, strict=True)))
    return summary


@dataclass
class Reread:
    """What reading a company's website again found. No AI: a plain HTTP read (Jina when the site needs a browser)."""

    reached: bool
    via: str | None
    pages: list[dict]
    socials: dict[str, str]
    emails: list[str]
    phone: str | None
    detail: str | None


async def reread_site(company: Company, reader: WebsiteReader) -> Reread:
    """Read the company's website again (homepage, contact, about and social pages) and keep what it shows: the
    socials it links, the emails and phone it publishes, the page texts. Its score and status aren't touched, and a
    social or phone already known is replaced only when the site shows one."""
    target = company.domain or normalize_domain(company.website_url)
    if not target:
        return Reread(False, None, [], {}, [], None, "No website on file")
    return apply_site(company, await reader.read(target))


def apply_site(company: Company, site: Any) -> Reread:
    """Keep what a website read found on the company (see reread_site). No awaits: callers reading several sites at
    once apply each under one lock, so a save in progress never sees a company half changed."""
    if not site.reached:
        return Reread(False, site.via, [], {}, [], None, site.detail or site.outcome.replace("_", " "))
    found = site.socials or {}
    company.instagram_url = found.get("instagram") or company.instagram_url
    company.facebook_url = found.get("facebook") or company.facebook_url
    company.linkedin_url = found.get("linkedin") or company.linkedin_url
    company.twitter_url = found.get("x") or company.twitter_url
    contacts = extract_contacts(site.pages, site.mailtos, site.tels, site.owner_names, company.domain)
    company.phone = contacts.phone or company.phone
    research = dict(company.research or {})
    research["website"] = {
        **(research.get("website") or {}),
        "outcome": site.outcome, "via": site.via, "url": site.resolved_url, "title": site.title, "text": site.text,
        "detail": site.detail, "pages": [{k: v for k, v in page.items() if k != "text"} for page in site.pages],
    }  # fmt: skip
    research["socials"] = {k: v for k, v in {
        "instagram": company.instagram_url, "facebook": company.facebook_url, "linkedin": company.linkedin_url, "x": company.twitter_url,
        **{k: v for k, v in found.items() if k not in ("instagram", "facebook", "linkedin", "x")},
    }.items() if v}  # fmt: skip
    if not contacts.empty:
        research["contacts"] = contacts.as_dict()
    research["reread_at"] = datetime.now(UTC).isoformat()
    company.research = research
    return Reread(True, site.via or "own_crawl", site.pages, research["socials"], list(contacts.as_dict().get("all_emails") or []),
                  company.phone, None)  # fmt: skip
