"""Posts for leads, gathered only until there's enough to write a personal opening line.

Three sources, in order, each tried only for the leads still short of material after the one before:
  1. the company's LinkedIn page,
  2. the person's own LinkedIn,
  3. the company's Instagram (the account its website links, never a personal one).

Each stage is one Apify run for the whole batch (a company shared by several leads is scraped once), fetching a
few posts per profile. The scrapers can't start from the second post, so asking again for more would pay for the
first ones twice; a small batch per source is the cheapest way. Posts already stored are used and never bought
again: the company Instagram posts research read, or a company another lead at it already had scraped.

After each stage Claude reads each lead's posts so far and says whether there's enough; a lead with enough is
done and costs nothing more. Every post found is kept and used when the email is written.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import anthropic
from pydantic import BaseModel

from services.apify import INSTAGRAM_ACTOR, LINKEDIN_POSTS_ACTOR, ApifyError, FetchedPost, instagram_handle, linkedin_slug
from services.claude import cost_usd, error_message

JUDGE_MODEL = "claude-haiku-5-5"
POST_CHARS = 600  # of each post Claude reads while judging

# (stage, network, whose posts)
STAGES = [
    ("company_linkedin", "linkedin", "company"),
    ("person_linkedin", "linkedin", "person"),
    ("company_instagram", "instagram", "company"),
]
STAGE_LABEL = {"company_linkedin": "company LinkedIn", "person_linkedin": "their LinkedIn", "company_instagram": "company Instagram"}


class Enough(BaseModel):
    enough: bool
    reason: str


JUDGE_SYSTEM = """You help write cold emails. You get the recent social posts we found for one person and their company, and you decide one thing: is there enough here to open a cold email with something specific and recent about them or their business?

Enough means at least one post with a concrete detail worth mentioning: a launch, a new product or collection, an event, a milestone, a hire, a project, an opinion they shared, a problem they talked about. One good post is enough.

Not enough: only generic promotion ("Shop now", "Sale ends Sunday"), holiday greetings, reposts of other people with no comment of their own, posts with no text, or nothing at all.

reason: a few words, e.g. "post 2: launched a spring collection" or "only sale announcements"."""


@dataclass
class Target:
    """A lead, with the company it was found at (None when it has none)."""

    lead: Any
    company: Any | None


@dataclass
class LeadOutcome:
    lead_id: UUID
    counts: dict[str, int | None] = field(default_factory=dict)  # stage -> posts found; None: no URL for it
    new: int = 0
    enough: bool = False
    stopped_at: str | None = None  # the stage that gave enough
    reason: str = ""


@dataclass
class FetchOutcome:
    leads: list[LeadOutcome]
    apify_cost_usd: float = 0.0
    claude_cost_usd: float = 0.0
    error: str | None = None


def company_linkedin(company: Any | None) -> str | None:
    if company is None:
        return None
    url = company.linkedin_url or ((company.research or {}).get("socials") or {}).get("linkedin")
    return url if url and "/company/" in url.lower() else None


def company_instagram(company: Any | None) -> str | None:
    if company is None:
        return None
    url = company.instagram_url or ((company.research or {}).get("socials") or {}).get("instagram")
    return url if instagram_handle(url) else None


def _source(stage: str, target: Target) -> tuple[str, str] | None:
    """(owner key, URL) to scrape for this lead at this stage, or None when there's nothing to scrape."""
    if stage == "company_linkedin":
        url = company_linkedin(target.company)
        return (f"company:{target.company.id}", url) if url else None
    if stage == "person_linkedin":
        url = target.lead.linkedin_url
        return (f"lead:{target.lead.id}", url) if linkedin_slug(url) else None
    url = company_instagram(target.company)
    return (f"company:{target.company.id}", url) if url else None


def _match_key(network: str, url: str) -> str:
    return (linkedin_slug(url) if network == "linkedin" else instagram_handle(url)) or ""


def format_posts(posts: list[Any]) -> str:
    lines = []
    for i, p in enumerate(posts, 1):
        whose = "their company" if p.company_id else "them"
        when = p.posted_at.strftime("%b %d, %Y") if p.posted_at else "date unknown"
        repost = ", a repost" if p.is_repost else ""
        text = " ".join((p.text or "").split())[:POST_CHARS] or "(no text)"
        lines.append(f"{i}. {p.source.title()} post by {whose}, {when}{repost}:\n{text}")
    return "\n\n".join(lines)


async def judge(client: anthropic.AsyncAnthropic, lead: Any, company: Any | None, posts: list[Any]) -> tuple[Enough | None, float, str | None]:
    """(Claude's verdict, its cost, an error). Nothing to read is never enough, and costs nothing."""
    if not posts:
        return Enough(enough=False, reason="no posts"), 0.0, None
    who = " ".join(x for x in (lead.first_name, lead.last_name) if x) or "this person"
    at = f", {lead.title}" if getattr(lead, "title", "") else ""
    biz = f" at {company.name}" if company is not None else (f" at {lead.company}" if getattr(lead, "company", "") else "")
    user = f"Person: {who}{at}{biz}\n\nTheir posts so far, newest first:\n\n{format_posts(posts)}"
    try:
        response = await client.messages.parse(
            model=JUDGE_MODEL, max_tokens=512, system=JUDGE_SYSTEM, output_format=Enough, messages=[{"role": "user", "content": user}]
        )
    except anthropic.APIError as exc:
        return None, 0.0, error_message(exc)
    cost = cost_usd(JUDGE_MODEL, response.usage.input_tokens, response.usage.output_tokens)
    return response.parsed_output, cost, None


async def gather(
    *,
    targets: list[Target],
    stages: list[str],
    max_posts: int,
    since: datetime,
    apify: Any,
    claude: anthropic.AsyncAnthropic | None,
    stored: Callable[[Any], Awaitable[list[Any]]],
    stored_count: Callable[[str, str], Awaitable[int]],
    save: Callable[[str, list[FetchedPost]], Awaitable[int]],
    record_apify: Callable[[str, Any, str], Awaitable[None]],
    record_claude: Callable[[float], Awaitable[None]],
    progress: Callable[[str], Awaitable[None]] | None = None,
) -> FetchOutcome:
    """Run the stages for these leads. `stored(target)`: the lead's and its company's posts, newest first.
    `stored_count(owner, network)`: how many posts of that network are already stored for that owner. `save(owner, posts)`:
    store them, returns how many were new. Without Claude every stage runs (no judging)."""
    out = {t.lead.id: LeadOutcome(t.lead.id) for t in targets}
    result = FetchOutcome(list(out.values()))
    pending = list(targets)
    for stage, network, _whose in STAGES:
        if stage not in stages or not pending or result.error:
            continue
        if progress:
            await progress(f"Reading {STAGE_LABEL[stage]}")
        # Who has something to read at this stage, and which profiles still need scraping.
        to_scrape: dict[str, str] = {}  # owner -> URL
        have: dict[str, int] = {}  # owner -> posts already stored, used as they are
        readers: list[Target] = []
        for t in pending:
            src = _source(stage, t)
            if src is None:
                out[t.lead.id].counts[stage] = None
                continue
            owner, url = src
            readers.append(t)
            if owner not in to_scrape and owner not in have:
                if n := await stored_count(owner, network):
                    have[owner] = n
                else:
                    to_scrape[owner] = url
        if to_scrape:
            urls = list(dict.fromkeys(to_scrape.values()))
            actor = LINKEDIN_POSTS_ACTOR if network == "linkedin" else INSTAGRAM_ACTOR
            try:
                fetch = apify.linkedin_posts if network == "linkedin" else apify.instagram_posts
                fetched, run = await fetch(urls, max_posts, since)
            except ApifyError as exc:
                await record_apify(actor, None, "failed")
                result.error = exc.message
                break
            await record_apify(actor, run, "succeeded")
            result.apify_cost_usd += run.cost_usd or 0.0
            by_key = {_match_key(network, url): owner for owner, url in to_scrape.items()}
            grouped: dict[str, list[FetchedPost]] = {owner: [] for owner in to_scrape}
            for post in fetched:
                owner = by_key.get(post.owner_key or "") or (next(iter(to_scrape)) if len(to_scrape) == 1 else None)
                if owner:
                    grouped[owner].append(post)
            new_by_owner = {owner: await save(owner, posts) for owner, posts in grouped.items()}
            found_by_owner = {owner: len(posts) for owner, posts in grouped.items()}
        else:
            new_by_owner, found_by_owner = {}, {}
        reading = {t.lead.id for t in readers}
        still: list[Target] = [t for t in pending if t.lead.id not in reading]  # nothing new to judge at this stage
        for t in readers:
            owner, _ = _source(stage, t)
            out[t.lead.id].counts[stage] = found_by_owner.get(owner, have.get(owner, 0))
            out[t.lead.id].new += new_by_owner.get(owner, 0) if owner.startswith("lead:") else 0
        for t in readers:
            o = out[t.lead.id]
            if claude is None:
                still.append(t)
                continue
            verdict, cost, error = await judge(claude, t.lead, t.company, await stored(t))
            if cost:
                result.claude_cost_usd += cost
                await record_claude(cost)
            if error:
                result.error = error
                still.append(t)
                continue
            o.reason = verdict.reason
            if verdict.enough:
                o.enough, o.stopped_at = True, stage
            else:
                still.append(t)
        pending = still
        if result.error:
            break
    for o in out.values():
        if o.stopped_at is None and not o.reason:
            o.reason = "no posts found"
    result.apify_cost_usd = round(result.apify_cost_usd, 4)
    result.claude_cost_usd = round(result.claude_cost_usd, 6)
    return result
