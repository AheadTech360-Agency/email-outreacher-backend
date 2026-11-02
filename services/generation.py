"""Writing an email for a lead with Claude, in up to three steps, each on a chosen prompt.

  1. Hook selection (the ICP's Hook Selection prompt): the person's own posts, their company's posts,
     website and profile go in; a ranked list of hooks comes out, best first. A hook can come from a
     post or from the website or profile. Skipped when there's nothing to read at all.
  2. Email generation (the ICP's Email Generation prompt): the person, the ranked hooks, the post
     behind the top one, the company, the product, the example emails and the rated examples go in; the
     subject and body come out.
  3. Email scoring (the workspace's default Email Scoring prompt): the draft goes in next to the good
     and bad examples and the example emails; a score from 0 to 100 and reasons come out. A failed score
     doesn't lose the draft.

Answers come back in a fixed JSON shape (structured outputs), whatever the prompt asks for. Nothing
here sends an email: the result is a draft for a person to review.

Temperature is never sent (Claude Haiku 5.5 rejects it). Thinking is on by default and counts toward
max tokens, so a prompt's max tokens is raised to at least MIN_MAX_TOKENS.
"""

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import anthropic
from pydantic import BaseModel, Field

from services.apify import search_name
from services.claude import error_message
from services.prompts import product_variables, render

MIN_MAX_TOKENS = 4096
DEFAULT_POSTS = 15  # when a prompt version doesn't set max_posts
ITEM_CHARS = 700
EXAMPLE_CHARS = 1500
MAX_HOOKS = 5
# Material the hook step can work from besides posts. "(couldn't be read)" means there's no website text.
NO_WEBSITE = "(couldn't be read)"


class HookIdea(BaseModel):
    """One hook: a specific detail about the person or their company the email could open with."""

    item: int | None = Field(
        description="The number of the post it comes from, as numbered in the lists given, or null when it comes from the website or their profile."
    )
    source: str = Field(
        description='Where it comes from, e.g. "their LinkedIn post", "their Instagram post", "company Instagram post", "company website", "their profile".'
    )
    point: str = Field(description="What they said or did, in one plain line.")
    hook: str = Field(description="The opening sentence, written to the person, at most 25 words.")
    why: str = Field(description="One line on how it connects to their pains or to what we sell.")
    relevance: int = Field(
        description="0 to 5: 5 is a recent, personal detail that leads straight to our product, 3 a solid company detail, 1 weak, 0 unusable."
    )


class HookChoice(BaseModel):
    """Hook Selection's answer: the hooks it found, ranked."""

    hooks: list[HookIdea] = Field(description="Up to 5 hooks, ranked best first. Empty when nothing is usable.")
    flags: list[str] = Field(
        description='Any that apply: "sensitive_topic", "repost", "old_content", "thin_material".'
    )


class EmailDraft(BaseModel):
    """Email Generation's answer: the email to send the person."""

    subject: str = Field(description="2 to 6 words, lowercase, specific to the person.")
    body: str = Field(description="The email body, from the greeting to the closing question, with no signature.")
    confidence: int = Field(description="0 to 100: how strong and well grounded the email is.")


class FollowUpDraft(BaseModel):
    """A follow-up's answer: the body of email 2 or 3. It replies in email 1's thread, so no subject."""

    body: str = Field(description="The email body, from the greeting to the closing question, with no signature.")
    confidence: int = Field(description="0 to 100: how natural and well grounded the follow-up is.")


class EmailScore(BaseModel):
    """Email Scoring's answer on one draft."""

    score: int = Field(
        description="0 to 100: how close the draft is to the emails rated good and the example emails, and how far from the ones rated bad."
    )
    reasons: list[str] = Field(description="2 to 5 short reasons, most important first.")


@dataclass(frozen=True)
class PromptVersionSpec:
    version_id: UUID | None
    system: str
    user: str
    model: str
    max_tokens: int
    max_posts: int | None = None

    @property
    def posts(self) -> int:
        return self.max_posts or DEFAULT_POSTS


@dataclass
class Item:
    """One numbered post Claude may build the email around."""

    post_id: UUID
    whose: str  # person | company
    source: str  # linkedin | instagram
    posted_at: datetime | None
    text: str
    url: str
    # Someone else's post they reposted: who wrote it ("Martin Gould, Personal Injury Attorney"). Empty for their own.
    reposted_from: str = ""


@dataclass
class Example:
    """An earlier email for the same ICP that a person rated, shown to Claude in the email and score steps."""

    rating: str  # good | bad
    subject: str
    body: str
    hook: str
    tags: list[str]
    note: str


@dataclass
class Sample:
    """An email from the example library (the Example emails page), in the voice we want."""

    subject: str
    body: str
    note: str


@dataclass
class LeadContext:
    icp: dict[str, Any]
    profile: dict[str, Any] | None
    # first_name, last_name, title, seniority, headline, location, company, website, linkedin_url,
    # instagram_url, experience, extra
    lead: dict[str, Any]
    items: list[Item]
    instruction: str = ""
    examples: list[Example] = field(default_factory=list)
    # The {{company.*}} placeholders for the lead's company (services.prompts.company_variables).
    company: dict[str, str] = field(default_factory=dict)
    # Example emails for this ICP from the library, newest first.
    samples: list[Sample] = field(default_factory=list)


@dataclass
class Call:
    step: str  # hook | email | score
    model: str
    input_tokens: int
    output_tokens: int


@dataclass
class Draft:
    hooks: list[HookIdea] = field(default_factory=list)  # the usable hooks, best first
    flags: list[str] = field(default_factory=list)
    top: HookIdea | None = None  # the hook the email opens with
    chosen: Item | None = None  # the post behind it, when it came from a post
    email: EmailDraft | None = None
    score: EmailScore | None = None
    score_error: str | None = None
    error: str | None = None
    calls: list[Call] = field(default_factory=list)


def format_items(items: list[Item], only: Callable[[Item], bool] | None = None) -> str:
    """Numbered posts. A post keeps its number in `items` when `only` picks a subset of them."""
    rows = [(i, it) for i, it in enumerate(items, 1) if only is None or only(it)]
    if not rows:
        return "(no posts found)"
    lines = []
    for i, it in rows:
        date = it.posted_at.date().isoformat() if it.posted_at else "undated"
        if it.reposted_from:
            who = f"they reposted this post by {it.reposted_from}; not their words"
        else:
            who = "their own post" if it.whose == "person" else "their company's post"
        text = " ".join(it.text.split())[:ITEM_CHARS] or "(no text)"
        lines.append(f"{i}. [{it.source}, {date}, {who}] {text}")
    return "\n".join(lines)


def format_hooks(hooks: list[HookIdea]) -> str:
    if not hooks:
        return "(no hooks found)"
    lines = []
    for rank, h in enumerate(hooks, 1):
        lines.append(
            f"{rank}. {h.hook}\n   From: {h.source}. About: {h.point}\n   Why it fits: {h.why} (relevance {h.relevance}/5)"
        )
    return "\n".join(lines)


def _one_example(e: Example) -> str:
    why = ", ".join(t.replace("_", " ") for t in e.tags)
    lines = [f"Subject: {e.subject}", e.body.strip()[:EXAMPLE_CHARS]]
    if why or e.note.strip():
        lines.append(f"Reviewer said: {'; '.join(x for x in (why, e.note.strip()) if x)}")
    return "\n".join(lines)


def format_rated(examples: list[Example], rating: str) -> str:
    """The {{examples.good}} or {{examples.bad}} block."""
    chosen = [e for e in examples if e.rating == rating]
    if not chosen:
        return "(none rated yet)"
    tag = f"{rating}_example"
    return "\n\n".join(f"<{tag}>\n{_one_example(e)}\n</{tag}>" for e in chosen)


def format_samples(samples: list[Sample]) -> str:
    """The {{examples.samples}} block: our team's example emails (templates), each with its note."""
    if not samples:
        return "(no example emails yet)"
    blocks = []
    for s in samples:
        lines = [f"Subject: {s.subject}"] if s.subject.strip() else []
        lines.append(s.body.strip()[:EXAMPLE_CHARS])
        if s.note.strip():
            lines.append(f"Note from our team (when to use it, how to fill it): {s.note.strip()}")
        blocks.append("<example_email>\n" + "\n".join(lines) + "\n</example_email>")
    return "\n\n".join(blocks)


def format_examples(examples: list[Example]) -> str:
    """Rated emails as one block, added after an email prompt that doesn't place {{examples.*}} itself."""
    if not examples:
        return ""
    good = [e for e in examples if e.rating == "good"]
    bad = [e for e in examples if e.rating == "bad"]
    parts = ["Feedback from the person reviewing these emails, on earlier emails for this same ICP."]
    if good:
        parts.append("Emails they rated GOOD. Match their style, length and approach (not their facts):")
        parts += [f"<good_example>\n{_one_example(e)}\n</good_example>" for e in good]
    if bad:
        parts.append("Emails they rated BAD. Avoid what they were criticised for:")
        parts += [f"<bad_example>\n{_one_example(e)}\n</bad_example>" for e in bad]
    parts.append("These are about other people and companies. Never reuse their details in this email.")
    return "\n\n".join(parts)


def uses_examples(template: str) -> bool:
    return re.search(r"\{\{\s*examples\.(good|bad)\s*\}\}", template) is not None


def variables(
    ctx: LeadContext,
    items: list[Item],
    top: HookIdea | None = None,
    chosen: Item | None = None,
    hooks: list[HookIdea] | None = None,
    email: EmailDraft | None = None,
) -> dict[str, str]:
    """The same placeholder names the prompt editor offers (lib/templating.ts). `items` are the newest
    posts, as many as the prompt being filled allows; {{posts.*}} and {{company.posts}} are parts of
    them, numbered the same."""
    icp, profile, lead = ctx.icp, ctx.profile or {}, ctx.lead
    first, last = lead.get("first_name") or "", lead.get("last_name") or ""
    if not first and not last and lead.get("company"):
        first = f"{search_name(lead['company'])} team"  # a company's website address: "Hi Quick Transfers team,"
    out = {
        "icp.name": icp.get("name") or "",
        "icp.industry": icp.get("industry") or "",
        "icp.places": ", ".join(icp.get("places") or []),
        "icp.titles": ", ".join(profile.get("titles") or []),
        "icp.description": profile.get("description") or "",
        "icp.pains": profile.get("pains") or "",
        "icp.offer": profile.get("offer") or "",
        "icp.proof": profile.get("proof") or "",
        "icp.cta": profile.get("cta") or "",
        "icp.tone": profile.get("tone") or "",
        # The example library replaced the ICP's single example email; the old name still works.
        "icp.exampleEmail": format_samples(ctx.samples),
        "icp.wordsToAvoid": profile.get("words_to_avoid") or "",
        "icp.hookGuidance": profile.get("hook_guidance") or "",
        "lead.firstName": first,
        "lead.lastName": last,
        "lead.fullName": f"{first} {last}".strip(),
        "lead.title": lead.get("title") or "",
        "lead.seniority": lead.get("seniority") or "",
        "lead.headline": lead.get("headline") or "",
        "lead.location": lead.get("location") or "",
        "lead.company": lead.get("company") or "",
        "lead.website": lead.get("website") or "",
        "lead.linkedinUrl": lead.get("linkedin_url") or "",
        "lead.instagramUrl": lead.get("instagram_url") or "",
        "lead.experience": lead.get("experience") or "",
        "lead.about": lead.get("about") or "",
        "items": format_items(items),
        "hooks": format_hooks(hooks or []),
        "hook.text": top.hook if top else "",
        "hook.summary": top.point if top else "",
        "hook.post": " ".join(chosen.text.split())[: ITEM_CHARS * 3] if chosen else "",
        "hook.source": chosen.source if chosen else (top.source if top else ""),
        "hook.url": chosen.url if chosen else "",
        "hook.date": chosen.posted_at.date().isoformat() if chosen and chosen.posted_at else "",
        "examples.samples": format_samples(ctx.samples),
        "examples.good": format_rated(ctx.examples, "good"),
        "examples.bad": format_rated(ctx.examples, "bad"),
        "email.subject": email.subject if email else "",
        "email.body": email.body if email else "",
    }
    for key, value in (lead.get("extra") or {}).items():
        out[f"lead.extra.{key}"] = str(value)
    return {
        **out,
        **ctx.company,
        **product_variables(profile.get("product")),
        # Posts last: these are the prompt's own share of posts, numbered as in {{items}}.
        "posts.linkedin": format_items(items, lambda it: it.whose == "person" and it.source == "linkedin"),
        "posts.instagram": format_items(items, lambda it: it.whose == "person" and it.source == "instagram"),
        "company.posts": format_items(items, lambda it: it.whose == "company"),
    }


def _has_material(ctx: LeadContext, items: list[Item]) -> bool:
    """Whether hook selection has anything to work from: posts, or the company's website text."""
    website = ctx.company.get("company.website", "")
    return bool(items) or bool(website and website != NO_WEBSITE)


class EmailWriter:
    def __init__(
        self, api_key: str, client: anthropic.AsyncAnthropic | None = None, concurrency: int = 3, base_url: str | None = None
    ) -> None:
        # base_url None: the SDK's own default.
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=4, timeout=180.0)
        self._gate = asyncio.Semaphore(concurrency)

    async def aclose(self) -> None:
        await self._client.close()

    async def _ask(self, spec: PromptVersionSpec, user: str, effort: str, shape: type[BaseModel], step: str, draft: Draft):
        response = await self._client.messages.parse(
            model=spec.model,
            max_tokens=max(spec.max_tokens, MIN_MAX_TOKENS),
            system=spec.system,
            output_config={"effort": effort},
            output_format=shape,
            messages=[{"role": "user", "content": user}],
        )
        draft.calls.append(Call(step, spec.model, response.usage.input_tokens, response.usage.output_tokens))
        if response.stop_reason == "refusal":
            raise _Stop(f"Claude declined the {step} step.")
        if response.stop_reason == "max_tokens":
            raise _Stop(f"Claude ran out of room in the {step} step; raise that prompt's max tokens.")
        if response.parsed_output is None:
            raise _Stop(f"Claude's {step} answer couldn't be read.")
        return response.parsed_output

    async def write(
        self,
        ctx: LeadContext,
        hook_spec: PromptVersionSpec,
        email_spec: PromptVersionSpec,
        score_spec: PromptVersionSpec | None = None,
    ) -> Draft:
        draft = Draft()
        async with self._gate:
            try:
                # Posts are newest first; each prompt gets as many as it allows.
                hook_items = ctx.items[: hook_spec.posts]
                if _has_material(ctx, hook_items):
                    choice: HookChoice = await self._ask(
                        hook_spec, render(hook_spec.user, variables(ctx, hook_items)), "low", HookChoice, "hook", draft
                    )
                    usable = []
                    for h in choice.hooks:
                        h.relevance = max(0, min(5, h.relevance))
                        # A hook must say something, and a post number must point at a post it was shown.
                        if not h.hook.strip() or (h.item is not None and not 1 <= h.item <= len(hook_items)):
                            continue
                        usable.append(h)
                    draft.hooks = usable[:MAX_HOOKS]
                    draft.flags = list(dict.fromkeys(f for f in choice.flags if f))
                    draft.top = next((h for h in draft.hooks if h.relevance > 0), None)
                    if draft.top is not None and draft.top.item is not None:
                        draft.chosen = hook_items[draft.top.item - 1]
                user = render(
                    email_spec.user,
                    variables(ctx, ctx.items[: email_spec.posts], draft.top, draft.chosen, draft.hooks),
                )
                # A prompt that doesn't place the rated examples itself gets them at the end.
                if ctx.examples and not uses_examples(email_spec.user):
                    user += "\n\n" + format_examples(ctx.examples)
                if ctx.instruction.strip():
                    user += f"\n\nAn extra instruction from the person reviewing this email: {ctx.instruction.strip()}"
                email: EmailDraft = await self._ask(email_spec, user, "medium", EmailDraft, "email", draft)
                email.subject, email.body = email.subject.strip(), email.body.strip()
                email.confidence = max(0, min(100, email.confidence))
                draft.email = email
            except _Stop as stop:
                draft.error = str(stop)
            except anthropic.APIError as exc:
                draft.error = error_message(exc)

            if draft.email is not None and score_spec is not None:
                try:
                    user = render(
                        score_spec.user,
                        variables(ctx, ctx.items[: score_spec.posts], draft.top, draft.chosen, draft.hooks, draft.email),
                    )
                    score: EmailScore = await self._ask(score_spec, user, "low", EmailScore, "score", draft)
                    score.score = max(0, min(100, score.score))
                    score.reasons = [r.strip() for r in score.reasons if r.strip()][:6]
                    draft.score = score
                except _Stop as stop:
                    draft.score_error = str(stop)
                except anthropic.APIError as exc:
                    draft.score_error = error_message(exc)
        return draft


async def write_follow_up(writer: "EmailWriter", spec: PromptVersionSpec, user: str) -> tuple[FollowUpDraft | None, list[Call], str | None]:
    """Email 2 or 3 from a filled follow-up prompt: (the follow-up or None, the Claude calls, why it failed)."""
    draft = Draft()
    try:
        async with writer._gate:
            result = await writer._ask(spec, user, "medium", FollowUpDraft, "followup", draft)
        return result, draft.calls, None
    except _Stop as exc:
        return None, draft.calls, str(exc)
    except anthropic.APIError as exc:
        return None, draft.calls, error_message(exc)


class _Stop(Exception):
    pass
