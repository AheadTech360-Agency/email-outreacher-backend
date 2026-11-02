"""Drafting emails for leads, and reviewing the drafts. Nothing here sends an email.

  generate  for each lead: hook selection on their posts, their company's posts and website, and their
            profile (the ICP's Hook Selection prompt) gives ranked hooks; the email is written from the
            top one (its Email Generation prompt); then the draft is scored against the rated examples
            (the workspace's Email Scoring prompt). Each prompt version sets how many posts it's given.
            Replaces the lead's unsent draft. Up to 25 leads at a time. Anyone on the do-not-contact
            list, or at a company on it, is skipped.
  list      drafts, with the ranked hooks, the post the top hook came from, and the score.
  update    edit the subject or body, approve, skip, or put back to draft.
  feedback  rate a draft good or bad, with tags and a note. The ICP's latest ratings are shown to
            Claude as examples when it writes the next emails (how many of each is a workspace setting).
"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from crud import (
    PRODUCT_FIELDS, CompanyRepo, EmailRepo, ExampleRepo, ExclusionRepo, FeedbackRepo, IcpProfileRepo, ProductRepo, PromptRepo,
    ProviderCallRepo, SettingsRepo, exclusion_note, person_keys,
)  # fmt: skip
from dependencies import (
    get_company_repo,
    get_email_repo,
    get_example_repo,
    get_exclusion_repo,
    get_feedback_repo,
    get_icp_profile_repo,
    get_lead_intel_repo,
    get_optional_writer,
    get_product_repo,
    get_prompt_repo,
    get_provider_call_repo,
    get_settings_repo,
    get_website_reader,
    require_manager,
    require_workspace_id,
)
from lead_intel import LeadIntelRepo
from services.identity import identity_keys
from models import Company, Email, Lead, Post, Stakeholder
from schemas import (
    EmailLeadOut,
    EmailOut,
    EmailPointOut,
    EmailPostOut,
    EmailUpdateIn,
    FeedbackIn,
    FeedbackOut,
    FeedbackStat,
    GenerateIn,
    GenerateOutcome,
    GenerateResult,
)
from services.claude import cost_usd
from services.prompts import company_variables
from services.research import _apollo_facts as apollo_facts
from services.generation import EmailWriter, Example, Item, LeadContext, PromptVersionSpec, Sample
from services.website import WebsiteReader

router = APIRouter(prefix="/emails", tags=["emails"])

LEAD_STATUS = {"draft": "generated", "edited": "generated", "approved": "approved", "queued": "approved", "skipped": "skipped"}


def _name(lead: Lead) -> str:
    return f"{lead.first_name} {lead.last_name}".strip() or (lead.email or "Unnamed lead")


def _text(value: Any) -> str:
    """A location or similar field that may come as text or as a small object."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return next((v for k in ("linkedinText", "text", "name", "full") if isinstance(v := value.get(k), str) and v), "")
    return ""


def reposted_from(post: Any) -> str:
    """Who wrote a post they only reposted ("Martin Gould, Personal Injury Attorney"), or "" for their own."""
    raw = post.raw or {}
    header = ((raw.get("header") or {}).get("text") or "").lower()
    if not (post.is_repost or raw.get("repostedBy") or "reposted" in header):
        return ""
    author = raw.get("author") or {}
    name, info = _text(author.get("name")), _text(author.get("info"))
    return ", ".join(x for x in (name, info[:100]) if x) or "someone else"


def _own_headline(posts: list[Any]) -> str:
    """Their LinkedIn headline, as their own posts show it under their name (free: it comes with the posts)."""
    for post in posts:
        if post.lead_id and post.source == "linkedin" and not reposted_from(post):
            info = _text(((post.raw or {}).get("author") or {}).get("info"))
            if info:
                return info
    return ""


def _person(lead: Lead, person: Stakeholder | None, company: Company | None, posts: list[Any] = ()) -> dict[str, Any]:
    """Everything known about the person for the {{lead.*}} placeholders: the lead, plus the record
    they were found or added as (Apollo, LinkedIn or by hand), plus the headline their posts show."""
    raw = (person.raw if person else None) or {}
    location = _text(raw.get("location")) or ", ".join(
        x for x in (raw.get("city"), raw.get("state"), raw.get("country")) if isinstance(x, str) and x
    )
    jobs = [j for j in raw.get("employment_history") or [] if isinstance(j, dict)]
    experience = "; ".join(
        f"{j.get('title') or 'Role'} at {j.get('organization_name') or 'a company'}" + (" (current)" if j.get("current") else "")
        for j in jobs[:5]
    )
    if not experience:  # a full LinkedIn profile (from the email search): position, company, how long
        roles = [j for j in raw.get("experience") or [] if isinstance(j, dict)]
        experience = "; ".join(
            f"{j.get('position') or 'Role'} at {j.get('companyName') or 'a company'}" + (f" ({j['duration']})" if j.get("duration") else "")
            for j in roles[:5]
        )
    return {
        "first_name": lead.first_name, "last_name": lead.last_name, "title": lead.title,
        "seniority": (person.seniority if person else None) or raw.get("seniority") or "",
        "headline": _text(raw.get("headline")) or _own_headline(posts),
        "location": location,
        "company": lead.company or (company.name if company else ""),
        "website": lead.website,
        "linkedin_url": lead.linkedin_url or (person.linkedin_url if person else None) or "",
        "instagram_url": lead.instagram_url or "",
        "experience": experience,
        "about": _text(raw.get("about"))[:600],
        "extra": lead.extra,
    }  # fmt: skip


def _company_evidence(
    company: Company | None, lead: Lead, fields: dict[str, Any] | None, pages: list[dict[str, str]]
) -> dict[str, Any]:
    """What we know about the lead's company: our research for Apollo companies (with the website pages
    it read), Lead Intel's stored fields (read only) for Lead Intel ones, or just what's on the lead."""
    fields = fields or {}
    research = (company.research if company else None) or {}
    site = research.get("website") or {}
    website_text = site.get("text") if site.get("outcome") == "ok" else None
    if not website_text and fields.get("site_text"):
        website_text = "\n".join(x for x in (fields.get("page_title"), " ".join(fields["site_text"].split())[:2000]) if x)
    ig = research.get("instagram")
    if ig:
        ig = {k: v for k, v in ig.items() if k != "raw"}
    elif fields.get("instagram_handle"):
        ig = {
            "handle": fields.get("instagram_handle"), "bio": fields.get("instagram_bio"),
            "category": fields.get("instagram_category"), "followers": fields.get("instagram_followers"),
        }  # fmt: skip
    socials = research.get("socials") or {
        k: v for k, v in {
            "instagram": fields.get("instagram_url"), "facebook": fields.get("facebook_url"), "tiktok": fields.get("tiktok_url"),
        }.items() if v
    }  # fmt: skip
    place = (company.city, company.state) if company else ()
    return {
        "pages": pages,
        "name": company.name if company else lead.company,
        "domain": company.domain if company else lead.domain,
        "location": ", ".join(p for p in place if p),
        "socials": socials,
        "website_text": website_text,
        "instagram": ig,
        "apollo": apollo_facts(company.raw) if company else {},
        "summary": ((company.score_detail or {}).get("summary") if company else "") or "",
    }


WRITING_PAGES = 4  # pages read from a site nobody has read yet, when writing (homepage and the most useful few)


def _researched_site(company: Company | None) -> bool:
    return bool(company and ((company.research or {}).get("website") or {}).get("outcome") == "ok")


async def _read_site(
    workspace_id: UUID, company: Company | None, lead: Lead, reader: WebsiteReader, companies: CompanyRepo,
    cache: dict[str, list[dict[str, str]]],
) -> list[dict[str, str]]:  # fmt: skip
    """Read the lead's company website for the email, keeping the pages on the company for next time."""
    domain = (company.domain if company else None) or lead.domain or lead.website
    if not domain:
        return []
    if domain not in cache:
        site = await reader.read(domain, max_pages=WRITING_PAGES)
        cache[domain] = [{"url": p["url"], "text": p.get("text") or ""} for p in site.pages] if site.reached else []
        if cache[domain] and company:
            await companies.save_pages(workspace_id, company.id, site.via or "own_crawl", site.pages)
    return cache[domain]


def _spec(version) -> PromptVersionSpec:
    return PromptVersionSpec(
        version.id, version.system_prompt, version.user_prompt, version.model, version.max_tokens, version.max_posts
    )


NO_OFFER = "This ICP has no offer or product yet. Add one on its Definition tab, so the email says what you actually sell."


def has_offer(profile: Any, product: Any) -> bool:
    """Whether the ICP says what we sell. Without it Claude makes something up (seen on 2026-10-31: "keeping event
    orders and regular jobs in one place"), so no email is written."""
    return product is not None or bool(profile and (profile.offer or "").strip())


@router.post("/generate", response_model=GenerateResult, dependencies=[Depends(require_manager)])
async def generate(
    body: GenerateIn,
    workspace_id: UUID = Depends(require_workspace_id),
    emails: EmailRepo = Depends(get_email_repo),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    profiles: IcpProfileRepo = Depends(get_icp_profile_repo),
    prompts: PromptRepo = Depends(get_prompt_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    feedback: FeedbackRepo = Depends(get_feedback_repo),
    settings: SettingsRepo = Depends(get_settings_repo),
    products: ProductRepo = Depends(get_product_repo),
    library: ExampleRepo = Depends(get_example_repo),
    writer: EmailWriter | None = Depends(get_optional_writer),
    exclusions: ExclusionRepo = Depends(get_exclusion_repo),
    companies: CompanyRepo = Depends(get_company_repo),
    reader: WebsiteReader = Depends(get_website_reader),
) -> GenerateResult:
    """Draft an email for each lead. Leads without an ICP are skipped."""
    if writer is None:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not set")
    ids = list(dict.fromkeys(body.lead_ids))
    leads = {lead.id: lead for lead in await emails.leads(workspace_id, ids)}
    is_excluded = await exclusions.matcher()
    outcomes: dict[UUID, GenerateOutcome] = {}
    jobs: list[tuple[Lead, LeadContext, PromptVersionSpec, PromptVersionSpec, PromptVersionSpec | None]] = []
    # Email scoring isn't chosen per ICP: every draft is scored with the prompt picked in Settings, else the default.
    score_version = await prompts.for_icp(workspace_id, "score", (await settings.general(workspace_id))["scoring_prompt_id"])
    score_spec = _spec(score_version) if score_version else None
    icps: dict[UUID, dict[str, Any] | None] = {}
    rated: dict[UUID, list] = {}
    counts = await settings.feedback(workspace_id)
    want_good, want_bad = counts["good_examples"], counts["bad_examples"]
    samples: dict[UUID, list[Sample]] = {}
    examples_used: dict[UUID, list[UUID]] = {}
    lead_intel_fields: dict[UUID, dict[str, Any] | None] = {}
    sites_read: dict[str, list[dict[str, str]]] = {}

    # Gather everything from the database first; only the Claude calls run side by side.
    contacted = await emails.in_sequence(workspace_id, ids)
    for lead_id in ids:
        lead = leads.get(lead_id)
        if lead is None:
            outcomes[lead_id] = GenerateOutcome(lead_id=lead_id, name=None, status="skipped", reason="Not in this workspace")
            continue
        if lead_id in contacted:
            outcomes[lead_id] = GenerateOutcome(lead_id=lead_id, name=_name(lead), status="skipped", reason="Already queued for sending")
            continue
        company = await emails.company_for(workspace_id, lead)
        keys = person_keys(lead.email, lead.domain)
        if company:
            keys += identity_keys(place_id=company.google_place_id, domain=company.domain, phone=company.phone,
                                  name=company.name, city=company.city, state=company.state)  # fmt: skip
        if hit := is_excluded(keys):
            outcomes[lead_id] = GenerateOutcome(lead_id=lead_id, name=_name(lead), status="skipped", reason=exclusion_note(hit))
            continue
        icp_id = lead.lead_intel_icp_id or (company.icp_id if company else None)
        if icp_id and icp_id not in icps:
            icps[icp_id] = await lead_intel.get_icp(icp_id)
        icp = icps.get(icp_id) if icp_id else None
        if icp is None:
            outcomes[lead_id] = GenerateOutcome(lead_id=lead_id, name=_name(lead), status="skipped", reason="No ICP for this lead")
            continue
        profile = (await profiles.get_many(workspace_id, [icp_id])).get(icp_id)
        product = await products.get(workspace_id, profile.product_id) if profile and profile.product_id else None
        if not has_offer(profile, product):
            outcomes[lead_id] = GenerateOutcome(lead_id=lead_id, name=_name(lead), status="skipped", reason=NO_OFFER)
            continue
        hook_version = await prompts.for_icp(workspace_id, "hook", profile.hook_prompt_id if profile else None)
        email_version = await prompts.for_icp(workspace_id, "email", profile.email_prompt_id if profile else None)
        hook_spec, email_spec = _spec(hook_version), _spec(email_version)
        # Lead Intel companies weren't researched here: their website and Instagram come from Lead Intel (read only).
        business_id = (company.lead_intel_business_id if company else None) or lead.lead_intel_business_id
        has_research = bool(company and company.research)
        if business_id and not has_research and business_id not in lead_intel_fields:
            found = await lead_intel.get_company(business_id)
            lead_intel_fields[business_id] = found["fields"] if found else None
        fields = lead_intel_fields.get(business_id) if business_id and not has_research else None
        posts = await emails.posts_for(
            workspace_id, lead, company, max(hook_spec.posts, email_spec.posts, score_spec.posts if score_spec else 0)
        )
        person = await emails.person_for(workspace_id, lead)
        pages = [{"url": p.url, "text": p.text} for p in await emails.pages_for(company)] if company else []
        if not pages and not (fields or {}).get("site_text") and not _researched_site(company):
            # No website text yet (a Lead Intel company, say): read it now, once per site. Free.
            pages = await _read_site(workspace_id, company, lead, reader, companies, sites_read)
        if icp_id not in rated:
            # Never the lead's own earlier draft: it's about to be replaced.
            # One extra of each, in case one is this lead's own earlier draft.
            rated[icp_id] = await feedback.examples(
                workspace_id, icp_id, want_good + 1 if want_good else 0, want_bad + 1 if want_bad else 0
            )
        if icp_id not in samples:
            samples[icp_id] = [
                Sample(s.subject, s.body, s.note) for s in await library.for_icp(workspace_id, icp_id, counts["sample_examples"])
            ]
        own = {e.id for e, _ in await emails.list(workspace_id, lead_id=lead.id)}
        picked = [f for f in rated[icp_id] if f.email_id not in own]
        picked = [f for f in picked if f.rating == "good"][:want_good] + [f for f in picked if f.rating == "bad"][:want_bad]
        ctx = LeadContext(
            icp=icp,
            profile=None if profile is None else {
                **{k: getattr(profile, k) for k in (
                    "description", "titles", "pains", "offer", "proof", "cta", "tone",
                    "words_to_avoid", "hook_guidance",
                )},
                "product": None if product is None else {k: getattr(product, k) for k in PRODUCT_FIELDS},
            },
            lead=_person(lead, person, company, posts),
            items=[
                Item(p.id, "person" if p.lead_id else "company", p.source, p.posted_at, p.text, p.url, reposted_from(p))
                for p in posts
            ],
            instruction=body.instruction,
            examples=[Example(f.rating, f.subject, f.body, f.hook, list(f.tags), f.note) for f in picked],
            company=company_variables(_company_evidence(company, lead, fields, pages)),
            samples=samples[icp_id],
        )  # fmt: skip
        examples_used[lead.id] = [f.email_id for f in picked]
        jobs.append((lead, ctx, hook_spec, email_spec, score_spec))

    drafts = await asyncio.gather(*(writer.write(ctx, hook, email, score) for _, ctx, hook, email, score in jobs))

    total = 0.0
    for (lead, ctx, hook_spec, email_spec, score_spec), draft in zip(jobs, drafts, strict=True):
        cost = 0.0
        for call in draft.calls:
            c = cost_usd(call.model, call.input_tokens, call.output_tokens)
            cost += c
            await calls.record(
                workspace_id, "claude", "messages", "succeeded", lead_id=lead.id, cost_usd=c, action="Write emails",
                model=call.model, input_tokens=call.input_tokens, output_tokens=call.output_tokens,
            )  # fmt: skip
        total += cost
        if draft.error or draft.email is None:
            if not draft.calls:
                await calls.record(workspace_id, "claude", "messages", "failed", lead_id=lead.id, action="Write emails")
            outcomes[lead.id] = GenerateOutcome(lead_id=lead.id, name=_name(lead), status="failed", reason=draft.error)
            continue
        # The ranked hooks, best first. A hook's post number is its place in the posts it was shown,
        # which are the first ones of ctx.items.
        points = [
            {
                "post_id": ctx.items[h.item - 1].post_id if h.item and 1 <= h.item <= len(ctx.items) else None,
                "point": h.point.strip(), "hook": h.hook.strip(), "source": h.source.strip(), "why": h.why.strip(),
                "relevance": h.relevance, "prompt_version_id": hook_spec.version_id, "model": hook_spec.model,
            }
            for h in draft.hooks
        ]  # fmt: skip
        flags = list(draft.flags)
        if draft.top is None:
            flags.append("no_hook")
        if score_spec is not None and draft.score is None:
            flags.append("not_scored")
        if lead.email_status == "risky":
            flags.append("risky_email")  # a catch-all domain: it may bounce, so it needs its own approval to queue
        if lead.email_status == "website":
            flags.append("website_email")  # orders@ or info@ from their site, not a person: it needs its own approval too
        row = await emails.save_draft(
            workspace_id, lead, points=points,
            email={
                "hook_prompt_version_id": hook_spec.version_id if draft.calls and draft.calls[0].step == "hook" else None,
                "email_prompt_version_id": email_spec.version_id,
                "chosen_post_id": draft.chosen.post_id if draft.chosen else None,
                "hook": draft.top.hook.strip() if draft.top else "",
                "relevance": draft.top.relevance if draft.top else 0,
                "subject": draft.email.subject, "body": draft.email.body, "confidence": draft.email.confidence,
                "flags": sorted(set(flags)), "cost_usd": round(cost, 6), "examples_used": examples_used.get(lead.id, []),
            },
            score=None if draft.score is None else {
                "score": draft.score.score, "reasons": draft.score.reasons,
                "prompt_version_id": score_spec.version_id if score_spec else None,
            },
        )  # fmt: skip
        outcomes[lead.id] = GenerateOutcome(lead_id=lead.id, name=_name(lead), status="drafted", email_id=row.id)

    results = [outcomes[i] for i in ids]
    return GenerateResult(results=results, drafted=sum(r.status == "drafted" for r in results), cost_usd=round(total, 6))


async def _out(emails: EmailRepo, rows: list[tuple[Email, Lead]], feedback: FeedbackRepo) -> list[EmailOut]:
    insights = await emails.insights([lead.id for _, lead in rows])
    ratings = await feedback.for_emails([e.id for e, _ in rows])
    posts: dict[UUID, Post] = await emails.posts([e.chosen_post_id for e, _ in rows if e.chosen_post_id])
    out = []
    for e, lead in rows:
        post = posts.get(e.chosen_post_id) if e.chosen_post_id else None
        out.append(
            EmailOut(
                id=e.id,
                lead=EmailLeadOut.model_validate(lead),
                status=e.status,
                subject=e.subject,
                body=e.edited_body or e.body,
                original_body=e.body,
                edited=e.edited_body is not None,
                hook=e.hook,
                relevance=e.relevance,
                confidence=e.confidence,
                flags=e.flags,
                chosen_post=None if post is None else EmailPostOut(
                    source=post.source, url=post.url, posted_at=post.posted_at, text=post.text,
                    whose="person" if post.lead_id else "company",
                ),
                points=[EmailPointOut.model_validate(i) for i in insights.get(lead.id, [])],
                cost_usd=float(e.cost_usd),
                created_at=e.created_at,
                updated_at=e.updated_at,
                approved_at=e.approved_at,
                feedback=FeedbackOut.model_validate(ratings[e.id]) if e.id in ratings else None,
                examples_used=len(e.examples_used or []),
                # The lead holds the score of its current draft; older, sent or skipped drafts show none.
                score=lead.score if e.status in ("draft", "edited", "approved", "queued") else None,
                score_reasons=(lead.score_reasons or []) if e.status in ("draft", "edited", "approved", "queued") else [],
            )  # fmt: skip
        )
    return out


@router.get("", response_model=list[EmailOut])
async def list_emails(
    status: str | None = Query(default=None, pattern="^(draft|edited|approved|queued|sent|skipped)$"),
    lead_id: UUID | None = Query(default=None, alias="leadId"),
    workspace_id: UUID = Depends(require_workspace_id),
    emails: EmailRepo = Depends(get_email_repo),
    feedback: FeedbackRepo = Depends(get_feedback_repo),
) -> list[EmailOut]:
    """Every generated email, with its rating if it has one."""
    return await _out(emails, await emails.list(workspace_id, status, lead_id), feedback)


@router.get("/feedback/stats", response_model=list[FeedbackStat])
async def feedback_stats(
    workspace_id: UUID = Depends(require_workspace_id), feedback: FeedbackRepo = Depends(get_feedback_repo)
) -> list[FeedbackStat]:
    """How many emails were rated good and bad, per ICP."""
    by_icp: dict[UUID | None, dict[str, int]] = {}
    for icp_id, rating, n in await feedback.stats(workspace_id):
        by_icp.setdefault(icp_id, {"good": 0, "bad": 0})[rating] = n
    return [FeedbackStat(icp_id=k, **v) for k, v in by_icp.items()]


@router.put("/{email_id}/feedback", response_model=EmailOut)
async def rate_email(
    email_id: UUID,
    body: FeedbackIn,
    workspace_id: UUID = Depends(require_workspace_id),
    emails: EmailRepo = Depends(get_email_repo),
    feedback: FeedbackRepo = Depends(get_feedback_repo),
) -> EmailOut:
    """Rate an email good or bad. Rating again replaces the old rating."""
    found = await emails.get(workspace_id, email_id)
    if not found:
        raise HTTPException(status_code=404, detail="Email not found")
    email, lead = found
    await feedback.rate(email, lead, body.rating, list(dict.fromkeys(body.tags)), body.note.strip())
    [out] = await _out(emails, [(email, lead)], feedback)
    return out


@router.delete("/{email_id}/feedback", response_model=EmailOut)
async def clear_rating(
    email_id: UUID,
    workspace_id: UUID = Depends(require_workspace_id),
    emails: EmailRepo = Depends(get_email_repo),
    feedback: FeedbackRepo = Depends(get_feedback_repo),
) -> EmailOut:
    found = await emails.get(workspace_id, email_id)
    if not found:
        raise HTTPException(status_code=404, detail="Email not found")
    await feedback.clear(email_id)
    [out] = await _out(emails, [found], feedback)
    return out


@router.patch("/{email_id}", response_model=EmailOut)
async def update_email(
    email_id: UUID,
    body: EmailUpdateIn,
    workspace_id: UUID = Depends(require_workspace_id),
    emails: EmailRepo = Depends(get_email_repo),
    feedback: FeedbackRepo = Depends(get_feedback_repo),
) -> EmailOut:
    """Edit, approve, skip, or put back to draft. Approving doesn't send anything."""
    found = await emails.get(workspace_id, email_id)
    if not found:
        raise HTTPException(status_code=404, detail="Email not found")
    email, lead = found
    if email.status == "sent":
        raise HTTPException(status_code=409, detail="This email was already sent")
    if email.status == "queued" or email.step > 1:
        raise HTTPException(status_code=409, detail="This email is queued in CheapSequencer and can't be changed here")
    if body.subject is not None:
        email.subject = body.subject.strip()
    if body.body is not None and body.body.strip() != (email.edited_body or email.body):
        email.edited_body = body.body.strip()
        if email.status == "draft":
            email.status = "edited"
    if body.status is not None:
        email.status = "edited" if body.status == "draft" and email.edited_body is not None else body.status
        email.approved_at = datetime.now(UTC) if body.status == "approved" else None
    lead.status = LEAD_STATUS[email.status]
    await emails.commit_refresh(email, lead)
    [out] = await _out(emails, [(email, lead)], feedback)
    return out
