"""Posts for people and companies, read source by source, each only as far as it's needed.

Three sources, read in this order, each one ticked on its own:
  1. the company's LinkedIn page,
  2. the person's own LinkedIn,
  3. the company's Instagram (the account its website links, never a personal one).

Every ticked source is read, whatever the ones before it gave. Within a source: a first look of a couple of posts,
then Claude says whether they're enough to write with; only where they aren't is the source read up to the limit.
(The scrapers can't start from the third post, so that second read pays again for the first ones; most profiles
stop at the first look.) Each read is one Apify run for the whole batch, and a company shared by several people is
read and judged once. Posts already stored are used and never bought again. Every post found is used when the
email is written.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import anthropic
from pydantic import BaseModel

from services.apify import INSTAGRAM_ACTOR, LINKEDIN_POSTS_ACTOR, ApifyError, FetchedPost, instagram_handle, linkedin_slug
from services.claude import cost_usd, error_message

JUDGE_MODEL = "claude-haiku-5-5"
FIRST_LOOK = 2  # posts read from a source before Claude's check; more only if they weren't enough
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
    """A lead with the company it was found at (None when it has none), or a company on its own (lead None)."""

    lead: Any | None
    company: Any | None

    @property
    def key(self) -> Any:
        return self.lead.id if self.lead is not None else self.company.id


@dataclass
class LeadOutcome:
    lead_id: UUID
    counts: dict[str, int | None] = field(default_factory=dict)  # stage -> posts found; None: no URL for it
    new: int = 0
    enough: bool = False  # Claude found enough in at least one source
    stopped_at: str | None = None  # the first source that gave enough
    enough_in: list[str] = field(default_factory=list)  # every source that gave enough
    notes: list[str] = field(default_factory=list)  # Claude's word on each source
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
        if target.lead is None:
            return None
        url = target.lead.linkedin_url
        return (f"lead:{target.key}", url) if linkedin_slug(url) else None
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
    if lead is None:  # a company on its own
        user = f"Company: {company.name}\n\nIts posts so far, newest first:\n\n{format_posts(posts)}"
    else:
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
    instagram_since: datetime | None = None,
    apify: Any,
    claude: anthropic.AsyncAnthropic | None,
    owner_posts: Callable[[str, str], Awaitable[list[Any]]],
    save: Callable[[str, list[FetchedPost]], Awaitable[int]],
    record_apify: Callable[[str, Any, str], Awaitable[None]],
    record_claude: Callable[[float], Awaitable[None]],
    progress: Callable[[str], Awaitable[None]] | None = None,
) -> FetchOutcome:
    """Read each ticked source for these people or companies, each source on its own: a first look of a couple of
    posts, Claude's check on them, and up to `max_posts` only where that wasn't enough. Every ticked source is read,
    whatever the ones before it gave. `owner_posts(owner, network)`: the posts of that network stored for an owner
    ("lead:<id>" or "company:<id>"), newest first. `save(owner, posts)`: store them, returns how many were new.
    Without Claude every source is read up to the limit."""
    out = {t.key: LeadOutcome(t.key) for t in targets}
    result = FetchOutcome(list(out.values()))
    first = min(FIRST_LOOK, max_posts) if claude is not None else max_posts

    def recent(post: Any, network: str) -> bool:
        cutoff = instagram_since if network == "instagram" and instagram_since is not None else since
        date = post.posted_at
        if date is None:
            return False
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return date >= cutoff

    async def recent_posts(owner: str, network: str) -> list[Any]:
        return [post for post in await owner_posts(owner, network) if recent(post, network)]

    async def fetch(network: str, wanted: dict[str, str], limit: int) -> bool:
        """Scrape these owners' profiles (owner -> URL) for up to `limit` posts each, and store them."""
        actor = LINKEDIN_POSTS_ACTOR if network == "linkedin" else INSTAGRAM_ACTOR
        call = apify.linkedin_posts if network == "linkedin" else apify.instagram_posts
        try:
            fetched, run = await call(list(dict.fromkeys(wanted.values())), limit, instagram_since if network == "instagram" and instagram_since is not None else since)
        except ApifyError as exc:
            await record_apify(actor, None, "failed")
            result.error = exc.message
            return False
        await record_apify(actor, run, "succeeded")
        result.apify_cost_usd += run.cost_usd or 0.0
        by_key = {_match_key(network, url): owner for owner, url in wanted.items()}
        grouped: dict[str, list[FetchedPost]] = {owner: [] for owner in wanted}
        for post in fetched:
            if not recent(post, network):
                continue
            owner = by_key.get(post.owner_key or "") or (next(iter(wanted)) if len(wanted) == 1 else None)
            if owner:
                grouped[owner].append(post)
        for owner, posts in grouped.items():
            new_posts[owner] = new_posts.get(owner, 0) + await save(owner, posts)
        return True

    new_posts: dict[str, int] = {}
    for stage, network, _whose in STAGES:
        if stage not in stages or result.error:
            continue
        if progress:
            await progress(f"Reading {STAGE_LABEL[stage]}")
        owners: dict[str, str] = {}  # owner -> URL
        speaker: dict[str, Target] = {}  # owner -> a target it belongs to, for Claude's check
        for t in targets:
            src = _source(stage, t)
            if src is None:
                out[t.key].counts[stage] = None
                continue
            owner, url = src
            owners.setdefault(owner, url)
            speaker.setdefault(owner, t)
        if not owners:
            continue
        # 1. A first look, where fewer than that are stored.
        have = {owner: len(await recent_posts(owner, network)) for owner in owners}
        look = {o: u for o, u in owners.items() if have[o] < first}
        if look and not await fetch(network, look, first):
            break
        # 2. Claude's check on each profile's posts from this source; where it isn't enough, read up to the limit.
        verdicts: dict[str, Enough | None] = {}
        if claude is not None:
            for owner in owners:
                t = speaker[owner]
                posts = (await recent_posts(owner, network))[:max_posts]
                verdict, cost, error = await judge(claude, t.lead if owner.startswith("lead:") else None, t.company, posts)
                if cost:
                    result.claude_cost_usd += cost
                    await record_claude(cost)
                if error:
                    result.error = error
                    break
                verdicts[owner] = verdict
            if result.error:
                break
        more = {o: u for o, u in owners.items()
                if not (verdicts.get(o) and verdicts[o].enough) and max_posts > first and len(await recent_posts(o, network)) < max_posts}  # fmt: skip
        if more and not await fetch(network, more, max_posts):
            break
        for t in targets:
            src = _source(stage, t)
            if src is None:
                continue
            owner, _ = src
            o = out[t.key]
            o.counts[stage] = min(len(await recent_posts(owner, network)), max_posts)
            o.new += new_posts.get(owner, 0) if owner.startswith("lead:") else 0
            verdict = verdicts.get(owner)
            if verdict and verdict.enough:
                o.enough_in.append(stage)
                o.enough = True
                o.stopped_at = o.stopped_at or stage
            o.notes.append(f"{STAGE_LABEL[stage]}: {verdict.reason if verdict else 'read up to the limit'}")
    for o in out.values():
        o.reason = "; ".join(o.notes) or "no posts found"
    result.apify_cost_usd = round(result.apify_cost_usd, 4)
    result.claude_cost_usd = round(result.claude_cost_usd, 6)
    return result
