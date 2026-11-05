"""Filling in companies and people that are already verified (imported from Apollo, say), without judging them again:
no Claude, only reading what's there. Each part is chosen by the person running it.

Companies:
  website    a plain read of the site (Jina when it needs a browser): socials, emails, phone, page texts. Free.
  instagram  the profile of the account the site links: bio, category, followers, and its latest posts. $0.0023 each.
  linkedin   the LinkedIn company page: description, size, industry, address, specialties. $0.004 each.
People:
  linkedin   their full LinkedIn profile: about, experience, education, skills. $0.004 each. Written into the person's
             record, where email writing reads it.

Anything already filled in isn't bought again. One Apify run per kind per batch.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from services.apify import (
    INSTAGRAM_PROFILE_ACTOR, LINKEDIN_COMPANY_ACTOR, LINKEDIN_PROFILE_ACTOR, ApifyError, FetchedPost, instagram_handle, linkedin_slug,
)  # fmt: skip
from services.research import apply_site
from services.smart_posts import company_linkedin
from services.website import normalize_domain

log = logging.getLogger(__name__)

SITE_READS = 10  # websites read at once
SITE_SECONDS = 45  # the most one website may take
BATCH = 100  # profiles per Apify run
# What a LinkedIn profile adds to a person's record (what email writing and their page read).
PROFILE_KEYS = ("headline", "about", "experience", "education", "skills", "location", "certifications", "languages", "volunteering")

Progress = Callable[[str, int | None, int | None], Awaitable[None]]
Record = Callable[[str, Any, str], Awaitable[None]]  # (actor, run or None, succeeded | failed)


async def _quiet(step: str, done: int | None, total: int | None) -> None:
    pass


@dataclass
class EnrichOutcome:
    sites_read: int = 0
    sites_failed: int = 0
    instagram: int = 0
    linkedin_companies: int = 0
    linkedin_people: int = 0
    posts_saved: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)


def _chunks(items: list[Any], size: int = BATCH) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value.get("linkedinText") or "")
    return str(value or "")


def linkedin_company_summary(item: dict[str, Any]) -> dict[str, Any]:
    """The parts of a LinkedIn company page worth keeping, under plain names."""
    hq = item.get("headquarter") or item.get("headquarters") or {}
    if isinstance(hq, dict):
        hq = ", ".join(x for x in (hq.get("city"), hq.get("geographicArea"), hq.get("country")) if x)
    industries = item.get("industries") or item.get("industry") or []
    if isinstance(industries, list):
        industries = ", ".join(_text(i) for i in industries if _text(i))
    specialties = item.get("specialities") or item.get("specialties") or []
    founded = item.get("foundedOn") or item.get("founded")
    if isinstance(founded, dict):
        founded = founded.get("year")
    summary = {
        "name": item.get("name"), "url": item.get("linkedinUrl"), "tagline": item.get("tagline"),
        "description": item.get("description"), "website": item.get("website"), "industry": industries,
        "employees": item.get("employeeCount") or item.get("staffCount"),
        "size": _text(item.get("employeeCountRange")) or item.get("companySize"),
        "followers": item.get("followerCount"), "founded": founded, "headquarters": hq,
        "specialties": ", ".join(specialties) if isinstance(specialties, list) else specialties,
        "phone": _text(item.get("phone")), "type": _text(item.get("companyType")),
    }  # fmt: skip
    return {k: v for k, v in summary.items() if v not in (None, "", [], {})}


async def enrich_companies(
    companies: list[Any], *, website: bool, instagram: bool, linkedin: bool, reader: Any, apify: Any,
    save_pages: Callable[[Any, str, list[dict]], Awaitable[None]], save_posts: Callable[[Any, list[FetchedPost]], Awaitable[int]],
    record: Record, progress: Progress = _quiet,
) -> EnrichOutcome:  # fmt: skip
    """Fill these companies in. They're changed in place; the caller saves them."""
    out = EnrichOutcome()
    if website:
        targets = [c for c in companies if c.domain or c.website_url]
        gate, done = asyncio.Semaphore(SITE_READS), 0
        # Sites are read side by side, but saved one at a time: they share one database session, which refuses
        # two statements at once (a Quick Start run crashed on it, 2026-11-04).
        writing = asyncio.Lock()
        await progress("Reading websites", 0, len(targets))

        async def read(company: Any) -> None:
            nonlocal done
            site = None
            async with gate:
                try:
                    site = await asyncio.wait_for(reader.read(company.domain or normalize_domain(company.website_url)), SITE_SECONDS)
                except TimeoutError:
                    pass
                except Exception:  # noqa: BLE001 - one site's failure is that site's, never the whole run's
                    log.exception("reading %s failed", company.domain or company.website_url)
            # Applied and saved one at a time: the companies share one database session, which refuses two statements
            # at once, and a company changed while another's save is flushing loses the change.
            async with writing:
                found = apply_site(company, site) if site is not None else None
                if found is not None and found.reached:
                    out.sites_read += 1
                    await save_pages(company, found.via or "own_crawl", found.pages)
                else:
                    out.sites_failed += 1
                done += 1
                if done % 10 == 0 or done == len(targets):
                    await progress("Reading websites", done, len(targets))

        await asyncio.gather(*(read(c) for c in targets))

    if instagram and apify is not None:
        by_handle = {}
        for c in companies:
            handle = instagram_handle(c.instagram_url or ((c.research or {}).get("socials") or {}).get("instagram"))
            if handle and not (c.research or {}).get("instagram"):
                by_handle.setdefault(handle, []).append(c)
        handles = list(by_handle)
        await progress("Reading Instagram profiles", 0, len(handles))
        for i, chunk in enumerate(_chunks(handles)):
            try:
                profiles, run = await apify.instagram_profiles(chunk)
            except ApifyError as exc:
                await record(INSTAGRAM_PROFILE_ACTOR, None, "failed")
                out.errors.append(f"Instagram: {exc.message}")
                break
            await record(INSTAGRAM_PROFILE_ACTOR, run, "succeeded")
            out.cost_usd += run.cost_usd or 0.0
            for handle, ig in profiles.items():
                for c in by_handle.get(handle, []):
                    c.instagram_url = c.instagram_url or f"https://www.instagram.com/{handle}/"
                    c.research = {**(c.research or {}), "instagram": {
                        "handle": ig.handle, "name": ig.full_name, "bio": ig.bio, "category": ig.category,
                        "followers": ig.followers, "private": ig.private, "verified": ig.verified, "raw": ig.raw,
                    }}  # fmt: skip
                    out.instagram += 1
                    if ig.posts:
                        out.posts_saved += await save_posts(c, ig.posts)
            await progress("Reading Instagram profiles", min((i + 1) * BATCH, len(handles)), len(handles))

    if linkedin and apify is not None:
        by_slug: dict[str, list[Any]] = {}
        for c in companies:
            url = company_linkedin(c)
            if url and not (c.research or {}).get("linkedin"):
                by_slug.setdefault(linkedin_slug(url), []).append(c)
        urls = {slug: company_linkedin(cs[0]) for slug, cs in by_slug.items()}
        await progress("Reading LinkedIn company pages", 0, len(urls))
        for i, chunk in enumerate(_chunks(list(urls))):
            try:
                pages, run = await apify.linkedin_companies([urls[s] for s in chunk])
            except ApifyError as exc:
                await record(LINKEDIN_COMPANY_ACTOR, None, "failed")
                out.errors.append(f"LinkedIn: {exc.message}")
                break
            await record(LINKEDIN_COMPANY_ACTOR, run, "succeeded")
            out.cost_usd += run.cost_usd or 0.0
            for slug, item in pages.items():
                summary = linkedin_company_summary(item)
                for c in by_slug.get(slug, []):
                    c.research = {**(c.research or {}), "linkedin": summary}
                    raw = dict(c.raw or {})
                    # Where email writing and verification read a company's facts (services.research._apollo_facts).
                    raw.setdefault("short_description", summary.get("description"))
                    raw.setdefault("founded_year", summary.get("founded"))
                    raw.setdefault("estimated_num_employees", summary.get("employees"))
                    raw.setdefault("industry", summary.get("industry"))
                    c.raw = {k: v for k, v in raw.items() if v is not None}
                    c.phone = c.phone or summary.get("phone") or None
                    out.linkedin_companies += 1
            await progress("Reading LinkedIn company pages", min((i + 1) * BATCH, len(urls)), len(urls))
    out.cost_usd = round(out.cost_usd, 4)
    return out


async def enrich_people(people: list[Any], *, apify: Any, record: Record, progress: Progress = _quiet) -> EnrichOutcome:
    """Their full LinkedIn profiles, written into their records. Changed in place; the caller saves them."""
    out = EnrichOutcome()
    by_slug: dict[str, list[Any]] = {}
    for p in people:
        slug = linkedin_slug(p.linkedin_url)
        if slug and not (p.raw or {}).get("linkedin_profile_at"):
            by_slug.setdefault(slug, []).append(p)
    slugs = list(by_slug)
    await progress("Reading LinkedIn profiles", 0, len(slugs))
    for i, chunk in enumerate(_chunks(slugs)):
        try:
            profiles, run = await apify.linkedin_profiles([by_slug[s][0].linkedin_url for s in chunk])
        except ApifyError as exc:
            await record(LINKEDIN_PROFILE_ACTOR, None, "failed")
            out.errors.append(f"LinkedIn: {exc.message}")
            break
        await record(LINKEDIN_PROFILE_ACTOR, run, "succeeded")
        out.cost_usd += run.cost_usd or 0.0
        for slug, item in profiles.items():
            for p in by_slug.get(slug, []):
                raw = dict(p.raw or {})
                for key in PROFILE_KEYS:
                    if item.get(key) not in (None, "", [], {}):
                        raw[key] = item[key]
                raw["linkedin_profile_at"] = datetime.now(UTC).isoformat()
                p.raw = raw
                out.linkedin_people += 1
        await progress("Reading LinkedIn profiles", min((i + 1) * BATCH, len(slugs)), len(slugs))
    out.cost_usd = round(out.cost_usd, 4)
    return out
