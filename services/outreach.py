"""Coldbrew's emails, sent through CheapSequencer's campaigns. Queueing approved emails starts sending: a draft
campaign is activated once they're enrolled (approved 2026-11-05: queueing means sending). Paused campaigns stay paused.

  queue       An approved first email goes into its ICP's campaign ("Coldbrew: <ICP>"): the lead is
              imported into a new list with email_1_subject and email_1_body, and the generic
              follow-ups in email_2_body to email_5_body (so a step can never go out blank), then
              the list is enrolled.
  sequence    Five emails: day 1, 3, 6, 10 and 16 (SCHEDULE). CheapSequencer stops a lead's sequence when
              they reply, bounce or unsubscribe.
  follow-ups  Every run reads each campaign's enrollments. When a lead's next email is due within
              the lead time (last send + the step's delay) and they haven't replied, bounced or
              unsubscribed, Claude writes it with the follow-up prompt and it replaces the generic
              text. Nobody who already replied costs a follow-up.
  templates   The campaigns' step text: each lead's email, then the sender's name, company and
              website from Settings.
"""

import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from crud import PRODUCT_FIELDS, exclusion_note, person_keys
from models import Email, Lead
from services import notify
from services.claude import cost_usd
from services.generation import PromptVersionSpec, Sample, format_samples, write_follow_up
from services.prompts import product_variables, render_product as render, render as render_template
from services.sending import SendingError


CAMPAIGN_PREFIX = "Coldbrew: "
# Day 1, 3, 6, 10 and 16: the days after the previous email that each step waits.
SCHEDULE = {1: 0, 2: 2, 3: 3, 4: 4, 5: 6}
FOLLOW_UPS = (2, 3, 4, 5)
DEFAULT_DELAYS = {s: SCHEDULE[s] for s in FOLLOW_UPS}
# The lead fields each follow-up's text rides in; CheapSequencer needs them to exist before an import uses them.
BODY_FIELDS = {s: (f"Email {s} body", f"email_{s}_body") for s in FOLLOW_UPS}
ENDED = {"completed", "stopped", "paused", "removed", "cancelled"}


# ---------- Pure helpers ----------


def first_name_in(text: str, first_name: str) -> str:
    """The generic follow-ups say {{first_name}}; CheapSequencer may not resolve tags inside a field's
    value, so the name is filled in here. Without a name, 'Hi there'."""
    return re.sub(r"\{\{\s*first_name\s*\}\}", first_name.strip() or "there", text)


def clean_website(value: str) -> str:
    """A website as people write it: no spaces, no trailing slash. The scheme is kept if given."""
    return "".join(value.split()).rstrip("/")


def looks_like_website(value: str) -> bool:
    host = re.sub(r"^https?://", "", value, flags=re.I).split("/", 1)[0]
    return bool(re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", host, flags=re.I))


def signature(company: str, website: str) -> str:
    """What follows every email: the sender's name, then the company and its website."""
    lines = ["{{sender_name}}", company.strip(), website.strip()]
    return "\n".join(ln for ln in lines if ln)


def step_body(step: int, company: str, website: str) -> str:
    return f"{{{{email_{step}_body}}}}\n\n{signature(company, website)}"


def lead_row(lead: Lead, subject: str, body: str, fallbacks: dict[int, str]) -> dict[str, str]:
    """One CSV row for CheapSequencer: built-in lead fields plus the email fields (subject, then bodies 1 to 5)."""
    first = lead.first_name or ""
    return {
        "email": (lead.email or "").strip().lower(),
        "first_name": first,
        "last_name": lead.last_name or "",
        "company_name": lead.company or "",
        "job_title": lead.title or "",
        "website": lead.website or "",
        "linkedin_profile": lead.linkedin_url or "",
        "email_1_subject": subject,
        "email_1_body": body,
        **{f"email_{s}_body": first_name_in(fallbacks[s], first) for s in FOLLOW_UPS},
    }


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Progress:
    status: str
    sent: int
    last_sent_at: datetime | None
    stop_reason: str | None


def progress(enrollment: dict[str, Any]) -> Progress:
    """Where a lead stands, from a CheapSequencer enrollment."""
    reason = next((name for key, name in (("replied_at", "replied"), ("bounced_at", "bounced"), ("unsubscribed_at", "unsubscribed"))
                   if enrollment.get(key)), None)  # fmt: skip
    if not reason and enrollment.get("failure"):
        reason = "failed"
    return Progress(
        status=str(enrollment.get("status") or "unknown"),
        sent=int(enrollment.get("sent_count") or 0),
        last_sent_at=_when(enrollment.get("last_sent_at")),
        stop_reason=reason,
    )


def due_follow_up(p: Progress, delays: dict[int, int], now: datetime, lead_hours: int) -> int | None:
    """The follow-up (2 to 5) to write now: the lead is still active with no reply, the previous email
    has gone out, and the next one is due within `lead_hours`."""
    if p.status != "active" or p.stop_reason or p.last_sent_at is None:
        return None
    step = p.sent + 1
    if step not in FOLLOW_UPS:
        return None
    due = p.last_sent_at + timedelta(days=delays.get(step, DEFAULT_DELAYS[step]))
    return step if now >= due - timedelta(hours=lead_hours) else None


def delays_of(campaign: dict[str, Any]) -> dict[int, int]:
    steps = campaign.get("steps") or []
    out = dict(DEFAULT_DELAYS)
    for index, s in enumerate(steps[: len(SCHEDULE)], start=1):
        if index > 1 and isinstance(s.get("delay_days"), int):
            out[index] = s["delay_days"]
    return out


def key_for(*parts: Any) -> str:
    """A stable idempotency key: retrying the same work never repeats it in CheapSequencer."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:24]
    return f"coldbrew-{parts[0]}-{digest}"


# ---------- Wiring ----------


@dataclass
class Deps:
    workspace_id: UUID
    sending: Any  # SendingRepo
    settings: Any  # SettingsRepo
    profiles: Any  # IcpProfileRepo
    prompts: Any  # PromptRepo
    products: Any  # ProductRepo
    library: Any  # ExampleRepo
    exclusions: Any  # ExclusionRepo
    calls: Any  # ProviderCallRepo
    lead_intel: Any  # LeadIntelRepo
    sequencer: Any  # SequencerClient
    writer: Any = None  # EmailWriter, for follow-ups
    links: Any = None  # IcpCampaignRepo
    notify: Any = None  # async (lead) -> bool: the reply email; None means services.notify.reply_received


async def campaign_for_icps(deps: Deps, icp_ids: set[UUID]) -> dict[UUID, tuple[str, str]]:
    """Each ICP's campaign (id, name): the one chosen on its profile, else "Coldbrew: <ICP name>"."""
    campaigns = await deps.sequencer.campaigns()
    by_name = {c.get("name"): c for c in campaigns}
    by_id = {c.get("id"): c for c in campaigns}
    profiles = await deps.profiles.get_many(deps.workspace_id, list(icp_ids))
    links = await deps.links.all() if deps.links else {}
    out: dict[UUID, tuple[str, str]] = {}
    for icp_id in icp_ids:
        chosen = by_id.get(getattr(profiles.get(icp_id), "sequencer_campaign_id", None)) or by_id.get(links.get(icp_id))
        if chosen is None:
            icp = await deps.lead_intel.get_icp(icp_id)
            chosen = by_name.get(f"{CAMPAIGN_PREFIX}{icp['name']}") if icp else None
        if chosen:
            out[icp_id] = (chosen["id"], chosen.get("name") or "")
    return out


@dataclass
class CampaignOutcome:
    status: str  # created | renamed | linked | unchanged | skipped
    campaign: str
    reason: str | None = None

    @property
    def note(self) -> str:
        said = {
            "created": f'Campaign "{self.campaign}" created in CheapSequencer as a draft, copied from an existing one.',
            "renamed": f'Its campaign was renamed to "{self.campaign}".',
            "linked": f'Linked to the campaign "{self.campaign}".',
            "unchanged": f'Its campaign is "{self.campaign}".',
        }
        return said.get(self.status) or f"Campaign not set up: {self.reason}"


# Above what the mailboxes can send together (16 x 18 a day = 288), so each mailbox's own daily limit is what
# holds sending back, not the campaign.
CAMPAIGN_DAILY_CAP = 300
CAMPAIGN_START_HOUR = 8
CAMPAIGN_END_HOUR = 18
CAMPAIGN_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


async def configure_new_campaign(sequencer: Any, campaign_id: str, name: str) -> None:
    campaign, etag = await sequencer.campaign(campaign_id)
    inherited = campaign.get("sending_days") or {}
    days = {day: {"enabled": inherited.get(day, {}).get("enabled", index < 5),
                  "start_hour": CAMPAIGN_START_HOUR, "end_hour": CAMPAIGN_END_HOUR}
            for index, day in enumerate(CAMPAIGN_WEEKDAYS)}
    await sequencer.configure_new_campaign(campaign_id, name, CAMPAIGN_DAILY_CAP, days, etag,
        key_for("new-campaign-defaults", campaign_id, name, str(CAMPAIGN_START_HOUR), str(CAMPAIGN_END_HOUR), etag))



async def ensure_campaign(sequencer: Any, links: Any, icp_id: UUID, icp_name: str) -> CampaignOutcome:
    """Give an ICP its "Coldbrew: <ICP>" campaign: rename the linked one if the ICP was renamed, link one
    that already has the name, or make a draft copy of another Coldbrew campaign (steps, mailboxes,
    schedule, signature; no leads). Never activates anything."""
    wanted = f"{CAMPAIGN_PREFIX}{icp_name.strip()}"
    campaigns = [c for c in await sequencer.campaigns() if c.get("status") != "archived"]
    by_id = {c["id"]: c for c in campaigns}
    linked = by_id.get((await links.all()).get(icp_id))
    if linked:
        if linked.get("name") == wanted:
            return CampaignOutcome("unchanged", wanted)
        if linked.get("status") not in ("draft", "paused"):
            return CampaignOutcome("skipped", linked.get("name") or "", "it's sending, so it keeps its name. Pause it in CheapSequencer and save the ICP again to rename it.")
        if (linked.get("name") or "").endswith(" (copy)"):
            await configure_new_campaign(sequencer, linked["id"], wanted)
        else:
            _, etag = await sequencer.campaign(linked["id"])
            await sequencer.rename(linked["id"], wanted, etag, key_for("rename", linked["id"], wanted))
        return CampaignOutcome("renamed", wanted)
    if named := next((c for c in campaigns if c.get("name") == wanted), None):
        await links.link(icp_id, named["id"])
        return CampaignOutcome("linked", wanted)
    templates = sorted((c for c in campaigns if (c.get("name") or "").startswith(CAMPAIGN_PREFIX)), key=lambda c: c.get("name") or "")
    if not templates:
        return CampaignOutcome("skipped", wanted, "there's no Coldbrew campaign in CheapSequencer to copy the settings from.")
    # The minute is in the key: a retry doesn't copy twice, but a later restore gets a new copy, not the archived one.
    copy = await sequencer.duplicate(templates[0]["id"], key_for("duplicate", icp_id, templates[0]["id"], f"{datetime.now(UTC):%Y%m%d%H%M}"))
    await links.link(icp_id, copy["id"])  # linked first, so a failed rename is finished by the next save, not copied again
    await configure_new_campaign(sequencer, copy["id"], wanted)
    return CampaignOutcome("created", wanted)


# Enrollments in these states are over; anything else is a lead still in (or waiting for) the sequence.
ENROLLMENT_DONE = {"completed", "finished", "stopped", "removed", "cancelled", "replied", "bounced", "unsubscribed", "failed"}


class CampaignBusy(Exception):
    def __init__(self, campaign: str, leads: int) -> None:
        super().__init__(campaign)
        self.campaign, self.leads = campaign, leads

    @property
    def message(self) -> str:
        return (f'{self.leads} lead{"" if self.leads == 1 else "s"} {"is" if self.leads == 1 else "are"} still in "{self.campaign}". '
                "Archiving it now would stop their emails, so wait until they finish, or remove them in CheapSequencer first.")  # fmt: skip


async def retire_campaign(sequencer: Any, links: Any, icp_id: UUID) -> str | None:
    """Archive the ICP's campaign and drop the link. Refuses (CampaignBusy) while any lead is still in it.
    Returns the archived campaign's name, or None when the ICP had no campaign."""
    campaign_id = (await links.all()).get(icp_id)
    if campaign_id is None:
        return None
    found = next((c for c in await sequencer.campaigns() if c["id"] == campaign_id and c.get("status") != "archived"), None)
    if found is None:
        await links.unlink(icp_id)
        return None
    name = found.get("name") or ""
    live = [e for e in await sequencer.enrollments(campaign_id) if str(e.get("status") or "").lower() not in ENROLLMENT_DONE]
    if live:
        raise CampaignBusy(name, len(live))
    _, etag = await sequencer.campaign(campaign_id)
    await sequencer.archive(campaign_id, etag, key_for("archive", campaign_id))
    await links.unlink(icp_id)
    return name


@dataclass
class QueueOutcome:
    email_id: UUID
    status: str  # queued | skipped
    reason: str | None = None
    campaign: str | None = None


RISKY = "Risky email address (the domain accepts any address, so it may bounce). Confirm it separately to queue it."
WEBSITE = "A general address from their website (like orders@), not a person's. Confirm it separately to queue it."
# Addresses that need their own OK to queue, and why.
CONFIRM_FIRST = {"risky": RISKY, "website": WEBSITE}


def day(when: datetime) -> str:
    return f"{when:%b} {when.day}, {when:%Y}"


async def queue(deps: Deps, email_ids: list[UUID], risky_ok: set[UUID] = frozenset()) -> list[QueueOutcome]:
    """Put approved first emails into their ICP's campaign, then activate it if it's still a draft. An email to a
    risky address is left out unless a person confirmed that one (`risky_ok`)."""
    settings = await deps.settings.sending(deps.workspace_id)
    if not settings["sender_company"].strip() or not settings["sender_website"].strip():
        raise SendingError("Add the sender's company and website in Settings, under Sending, before queueing emails.")
    pairs = await deps.sending.to_queue(deps.workspace_id, email_ids)
    found = {e.id for e, _ in pairs}
    outcomes = [QueueOutcome(i, "skipped", "Not an approved first email, or already queued") for i in email_ids if i not in found]
    is_excluded = await deps.exclusions.matcher()

    # Nobody is queued twice: not an address queued before (under any lead, ICP or workspace, even one since
    # deleted), and not one that's twice in this batch.
    address = lambda lead: (lead.email or "").strip().lower()  # noqa: E731
    before = await deps.sending.queued_before(sorted({address(lead) for _, lead in pairs if lead.email}))
    seen: set[str] = set()
    groups: dict[UUID, list[tuple[Email, Lead]]] = {}
    for email, lead in pairs:
        if lead.email and address(lead) in before:
            outcomes.append(QueueOutcome(email.id, "skipped", f"{lead.email} was already queued on {day(before[address(lead)])}. Nobody is queued twice."))
        elif lead.email and address(lead) in seen:
            outcomes.append(QueueOutcome(email.id, "skipped", f"{lead.email} is in this batch twice; it's queued once."))
        elif not lead.email:
            outcomes.append(QueueOutcome(email.id, "skipped", "The lead has no email address"))
        elif lead.email_status in CONFIRM_FIRST and email.id not in risky_ok:
            outcomes.append(QueueOutcome(email.id, "skipped", CONFIRM_FIRST[lead.email_status]))
        elif hit := is_excluded(person_keys(lead.email, lead.domain)):
            outcomes.append(QueueOutcome(email.id, "skipped", exclusion_note(hit)))
        elif not lead.lead_intel_icp_id:
            outcomes.append(QueueOutcome(email.id, "skipped", "The lead has no ICP, so there's no campaign to put it in"))
        else:
            seen.add(address(lead))
            groups.setdefault(lead.lead_intel_icp_id, []).append((email, lead))
    if not groups:
        return outcomes

    campaigns = await campaign_for_icps(deps, set(groups))
    for icp_id, members in groups.items():
        if icp_id not in campaigns:
            icp = await deps.lead_intel.get_icp(icp_id)
            name = icp["name"] if icp else "this ICP"
            outcomes += [QueueOutcome(e.id, "skipped", f'No CheapSequencer campaign named "{CAMPAIGN_PREFIX}{name}"') for e, _ in members]
            continue
        campaign_id, campaign_name = campaigns[icp_id]
        # Claimed before the import: if another queue run took an address in the meantime, it's left out here.
        claimed = await deps.sending.claim(deps.workspace_id, campaign_id, [(e, address(lead)) for e, lead in members])
        outcomes += [QueueOutcome(e.id, "skipped", f"{lead.email} was just queued by someone else. Nobody is queued twice.")
                     for e, lead in members if address(lead) not in claimed]  # fmt: skip
        members = [(e, lead) for e, lead in members if address(lead) in claimed]
        if not members:
            continue
        ids = sorted(str(e.id) for e, _ in members)
        fallbacks = {s: settings[f"followup_fallback_{s}"] for s in FOLLOW_UPS}
        rows = [lead_row(lead, e.subject, e.edited_body or e.body, fallbacks) for e, lead in members]
        try:
            # The list's key and name depend only on which emails these are, so a retry reuses the same list.
            # Each import gets a fresh key: re-importing only updates the same leads by email.
            list_key = key_for("queue-list", *ids)  # not "list": keys from before this fix had timestamped names
            list_id = await deps.sequencer.create_list(f"{campaign_name} batch {list_key[-8:]}", list_key)
            await deps.sequencer.import_rows(list_id, rows, key_for("import", *ids, uuid4().hex))
            await deps.sequencer.enroll(campaign_id, list_id, key_for("enroll", campaign_id, *ids))
        except SendingError as exc:
            await deps.sending.release({address(lead) for _, lead in members})  # not sent: they can be queued later
            outcomes += [QueueOutcome(e.id, "skipped", exc.message) for e, _ in members]
            continue
        await deps.sending.mark_queued([e for e, _ in members], campaign_id, list_id)
        # Enrolled first, then launched: a failed launch never releases the address claim or reports these as
        # unqueued, which could enroll them twice.
        launch_note = None
        try:
            campaign, _ = await deps.sequencer.campaign(campaign_id)
            if campaign.get("status") == "draft":
                await deps.sequencer.activate(campaign_id, key_for("activate", campaign_id, *ids))
            elif campaign.get("status") != "active":
                launch_note = f"Queued, but {campaign_name} is {campaign.get('status') or 'inactive'}: resume it in CheapSequencer to send."
        except SendingError as exc:
            launch_note = f"Queued, but the campaign couldn't start: {exc.message}. They stay enrolled; don't queue them again."
        outcomes += [QueueOutcome(e.id, "queued", launch_note, campaign_name) for e, _ in members]
    return outcomes


@dataclass
class TemplateOutcome:
    campaign: str
    status: str  # updated | unchanged | skipped
    reason: str | None = None


async def sync_templates(deps: Deps) -> list[TemplateOutcome]:
    """Give every Coldbrew campaign its five steps on the schedule (day 1, 3, 6, 10, 16), each with the
    signature (sender name, company, website). Steps it already has keep their ids and settings; missing
    ones are added like step 2 (in the same thread). Only draft or paused campaigns can be changed; an
    active one is reported, not touched. The lead fields emails 4 and 5 ride in are created first."""
    settings = await deps.settings.sending(deps.workspace_id)
    company, website = settings["sender_company"], settings["sender_website"]
    out: list[TemplateOutcome] = []
    try:
        await ensure_body_fields(deps.sequencer)
    except SendingError as exc:
        return [TemplateOutcome("Lead fields", "skipped", exc.message)]
    for summary in await deps.sequencer.campaigns():
        name = summary.get("name") or ""
        if not name.startswith(CAMPAIGN_PREFIX):
            continue
        campaign, etag = await deps.sequencer.campaign(summary["id"])
        if campaign.get("status") not in ("draft", "paused"):
            out.append(TemplateOutcome(name, "skipped", "It's sending. Pause it in CheapSequencer to change its text."))
            continue
        steps = campaign.get("steps") or []
        wanted = [step_body(i, company, website) for i in SCHEDULE]
        if [s.get("body_text") for s in steps] == wanted and [s.get("delay_days") for s in steps] == list(SCHEDULE.values()):
            out.append(TemplateOutcome(name, "unchanged"))
            continue
        new_steps = campaign_steps(steps, wanted)
        try:
            await deps.sequencer.set_steps(summary["id"], new_steps, etag, key_for("steps", summary["id"], *wanted))
            out.append(TemplateOutcome(name, "updated"))
        except SendingError as exc:
            out.append(TemplateOutcome(name, "skipped", exc.message))
    return out


def campaign_steps(steps: list[dict[str, Any]], bodies: list[str]) -> list[dict[str, Any]]:
    """The campaign's steps as they should be: one per email on SCHEDULE. A step it has keeps its id, thread
    mode and plain-text setting; a missing one copies step 2's (a reply in the same thread)."""
    like = steps[1] if len(steps) > 1 else {}
    out = []
    for i, body in zip(SCHEDULE, bodies, strict=True):
        have = steps[i - 1] if i <= len(steps) else None
        source = have or like
        step = {k: source[k] for k in ("thread_mode", "send_as_plain_text") if k in source}
        if have and "id" in have:
            step["id"] = have["id"]
        if i > 1:
            step.setdefault("thread_mode", "reply_to_previous")
        step["delay_days"], step["body_text"] = SCHEDULE[i], body
        if i == 1:
            step["subject"] = (have or {}).get("subject") or "{{email_1_subject}}"
        out.append(step)
    return out


async def ensure_body_fields(sequencer: Any) -> list[str]:
    """Create the lead fields emails 2 to 5 ride in (Email 4 body, Email 5 body...) where CheapSequencer
    doesn't have them yet. Returns the tags it created."""
    tags = {f.get("tag") for f in await sequencer.custom_fields()}
    made = []
    for name, tag in BODY_FIELDS.values():
        if tag not in tags:
            await sequencer.create_custom_field(name, "textarea", key_for("field", tag))
            made.append(tag)
    return made


@dataclass
class FollowUpRun:
    checked: int = 0
    written: int = 0
    ended: int = 0
    cost_usd: float = 0.0
    notes: list[str] = field(default_factory=list)


async def _variables(deps: Deps, email: Email | None, lead: Lead, step: int, earlier: dict[int, str]) -> dict[str, str]:
    """A follow-up prompt's placeholders. With no first email (LinkedIn and Instagram campaigns), its parts are empty,
    and social.earlier holds what this campaign already sent them."""
    icp_id = lead.lead_intel_icp_id
    icp = (await deps.lead_intel.get_icp(icp_id)) if icp_id else None
    profile = (await deps.profiles.get_many(deps.workspace_id, [icp_id])).get(icp_id) if icp_id else None
    product = await deps.products.get(deps.workspace_id, profile.product_id) if profile and profile.product_id else None
    samples = [Sample(s.subject, s.body, s.note) for s in await deps.library.for_icp(deps.workspace_id, icp_id, 3)] if icp_id else []
    hooks = await deps.sending.hooks(lead.id)
    first, last = lead.first_name or "", lead.last_name or ""
    out = {
        "lead.firstName": first, "lead.lastName": last, "lead.fullName": f"{first} {last}".strip(),
        "lead.title": lead.title or "", "lead.company": lead.company or "", "lead.website": lead.website or "",
        "icp.name": (icp or {}).get("name") or "", "icp.pains": getattr(profile, "pains", "") or "",
        "icp.offer": getattr(profile, "offer", "") or "", "icp.proof": getattr(profile, "proof", "") or "",
        "icp.cta": getattr(profile, "cta", "") or "", "icp.tone": getattr(profile, "tone", "") or "",
        "icp.wordsToAvoid": getattr(profile, "words_to_avoid", "") or "",
        "hooks": "\n".join(f"{h.rank}. {h.hook or h.point} ({h.source})" for h in hooks) or "(none)",
        "hook.text": (email.hook if email else "") or "",
        "examples.samples": format_samples(samples),
        "email1.subject": email.subject if email else "", "email1.body": (email.edited_body or email.body) if email else "",
        "email2.body": earlier.get(2, ""), "email3.body": earlier.get(3, ""), "email4.body": earlier.get(4, ""),
        "followup.step": str(step), "social.step": str(step),
        "social.earlier": "\n\n".join(f"Message {n}:\n{text}" for n, text in sorted(earlier.items())) or "(nothing yet: this is the first)",
    }  # fmt: skip
    out.update(product_variables(None if product is None else {k: getattr(product, k, [] if k in ("custom_fields", "notes") else "") for k in PRODUCT_FIELDS}))
    return out


async def run_follow_ups(deps: Deps, now: datetime | None = None) -> FollowUpRun:
    """Read every campaign's enrollments, record where each lead stands, and write the follow-ups that
    are nearly due for leads who haven't replied."""
    now = now or datetime.now(UTC)
    run = FollowUpRun()
    pairs = await deps.sending.in_sequence(deps.workspace_id)
    if not pairs:
        return run
    settings = await deps.settings.sending(deps.workspace_id)
    lead_hours = settings["followup_lead_hours"]

    enrollments: dict[str, dict[str, dict[str, Any]]] = {}
    delays: dict[str, dict[int, int]] = {}
    for campaign_id in {e.sequencer_campaign_id for e, _ in pairs}:
        try:
            campaign, _ = await deps.sequencer.campaign(campaign_id)
            rows = await deps.sequencer.enrollments(campaign_id)
        except SendingError as exc:
            run.notes.append(exc.message)
            continue
        delays[campaign_id] = delays_of(campaign)
        enrollments[campaign_id] = {str(((r.get("lead") or {}).get("email") or "")).lower(): r for r in rows}
    try:
        mailboxes = {m["id"]: m.get("email") for m in await deps.sequencer.inboxes() if m.get("id")}
    except SendingError as exc:
        mailboxes = {}
        run.notes.append(exc.message)

    existing = await deps.sending.follow_ups([lead.id for _, lead in pairs])
    to_write: dict[str, list[tuple[Email, Lead, int, str, Any]]] = {}
    newly_replied: list[Lead] = []
    for email, lead in pairs:
        row = enrollments.get(email.sequencer_campaign_id, {}).get((lead.email or "").lower())
        if row is None:
            continue
        run.checked += 1
        p = progress(row)
        email.sequencer_lead_id = (row.get("lead") or {}).get("id") or email.sequencer_lead_id
        if inbox_id := row.get("assigned_inbox_id"):
            email.sequencer_inbox_id = inbox_id
            email.sequencer_inbox_email = mailboxes.get(inbox_id) or email.sequencer_inbox_email
        email.sequence_status, email.sequence_sent_count = p.status, p.sent
        # A reply seen here first (the webhook missed it, or hasn't come yet) is emailed once, after the save below.
        # One the webhook already recorded has its stop reason set, so it isn't emailed again.
        if p.stop_reason == "replied" and email.sequence_stop_reason != "replied":
            newly_replied.append(lead)
        # A reply, bounce or unsubscribe stays once recorded (a webhook may see it before the enrollment shows it). A
        # send failure is only ever what CheapSequencer shows now: one it retried and sent doesn't end the sequence,
        # so its follow-ups are written and uploaded like anyone's (2026-11-09: 7 leads kept a passing "failed").
        kept = email.sequence_stop_reason if email.sequence_stop_reason in FINAL_STOPS else None
        email.sequence_last_sent_at, email.sequence_stop_reason = p.last_sent_at, p.stop_reason or kept
        email.sequence_synced_at, email.sequence_raw = now, row
        if p.sent >= 1 and email.status == "queued":
            email.status, email.sent_at = "sent", email.sent_at or p.last_sent_at
        for step in FOLLOW_UPS:
            later = existing.get((lead.id, step))
            if later is not None and p.sent >= step and later.status == "queued":
                later.status, later.sent_at = "sent", p.last_sent_at
        if p.status in ENDED or email.sequence_stop_reason:
            run.ended += 1
            continue
        step = due_follow_up(p, delays.get(email.sequencer_campaign_id, DEFAULT_DELAYS), now, lead_hours)
        if step is None or (lead.id, step) in existing:
            continue
        if deps.writer is None:
            run.notes.append("ANTHROPIC_API_KEY is not set, so follow-ups keep their generic text.")
            break
        version = await deps.prompts.for_icp(deps.workspace_id, f"followup{step}", None)
        if version is None:
            continue
        spec = PromptVersionSpec(version.id, version.system_prompt, version.user_prompt, version.model, version.max_tokens)
        earlier = {k: (existing[(lead.id, k)].edited_body or existing[(lead.id, k)].body) for k in range(2, step) if (lead.id, k) in existing}
        await deps.sending.load_full(email, lead)  # the check read only a few columns of each; this one is written to
        variables = await _variables(deps, email, lead, step, earlier)
        draft, calls, error = await write_follow_up(deps.writer, replace(spec, system=render_template(spec.system, variables)), render(spec.user, variables))
        cost = sum(cost_usd(c.model, c.input_tokens, c.output_tokens) for c in calls)
        if calls:
            await deps.calls.record(
                deps.workspace_id, "claude", "messages", "failed" if error else "succeeded", cost_usd=round(cost, 4),
                action="Write follow-ups", model=calls[-1].model, lead_id=lead.id,
                input_tokens=sum(c.input_tokens for c in calls), output_tokens=sum(c.output_tokens for c in calls),
            )  # fmt: skip
        run.cost_usd += cost
        if error or draft is None:
            run.notes.append(f"{lead.email}: {error} (it keeps the generic follow-up until the next run)")
            continue
        to_write.setdefault(email.sequencer_list_id, []).append((email, lead, step, draft.body, (version.id, draft.confidence, cost)))
    await deps.sending.commit()
    if newly_replied:  # the reply email names them and their company: read the rest of these few leads
        await deps.sending.load_full(*newly_replied)
    for lead in newly_replied:  # saved as replied first, so the next check can't email them again
        await (deps.notify or notify.reply_received)(lead)

    for list_id, items in to_write.items():
        rows = [{"email": (lead.email or "").lower(), f"email_{step}_body": body} for _, lead, step, body, _ in items]
        try:
            # A fresh key each time: a rewrite has different text, and re-importing only updates the same leads.
            await deps.sequencer.import_rows(list_id, rows, key_for("followup", list_id, uuid4().hex))
        except SendingError as exc:
            run.notes.append(f"Follow-ups couldn't be written to CheapSequencer: {exc.message}")
            continue
        for email, _lead, step, body, (version_id, confidence, cost) in items:
            await deps.sending.add_follow_up(email, step, body, version_id, confidence, cost)
            run.written += 1
    return run


# ---------- What was queued, sent and replied ----------


STOPPED = {"stopped", "removed", "cancelled"}
# Stops that never undo: the lead answered, the address doesn't exist, or they asked to stop.
FINAL_STOPS = {"replied", "bounced", "unsubscribed"}


def lead_state(email: Email) -> str:
    """Where a queued lead's sequence stands, from the last follow-up check."""
    if email.sequence_stop_reason:
        return email.sequence_stop_reason
    status = email.sequence_status or "queued"
    if status == "completed":
        return "finished"
    if status in STOPPED:
        return "stopped"
    if status == "paused":
        return "paused"
    return "sending" if (email.sequence_sent_count or 0) > 0 else "queued"


@dataclass
class OutboxStep:
    step: int
    subject: str
    body: str
    status: str  # waiting | sent | skipped
    personalized: bool
    sent_at: datetime | None


def outbox_steps(email: Email, lead: Lead, later: dict[int, Email], fallbacks: dict[int, str]) -> list[OutboxStep]:
    """The five emails as the lead gets them: emails 2 to 5 are the generic text until Claude writes them."""
    sent = email.sequence_sent_count or 0
    ended = lead_state(email) not in ("queued", "sending")
    steps = [OutboxStep(1, email.subject, email.edited_body or email.body, "sent" if sent >= 1 else "skipped" if ended else "waiting",
                        True, email.sent_at)]  # fmt: skip
    for step in FOLLOW_UPS:
        row = later.get(step)
        body = (row.edited_body or row.body) if row else first_name_in(fallbacks[step], lead.first_name or "")
        status = "sent" if sent >= step else "skipped" if ended else "waiting"
        steps.append(OutboxStep(step, "", body, status, row is not None, row.sent_at if row else None))
    return steps


def _name_of(value: Any) -> str:
    return (value.get("name") or "") if isinstance(value, dict) else str(value or "")


def thread_summary(thread: dict[str, Any], campaign: str) -> dict[str, Any]:
    """The fields of a CheapSequencer inbox thread that the Replies page shows."""
    mailbox, lead = thread.get("mailbox") or {}, thread.get("lead") or {}
    return {
        "id": thread["id"], "subject": thread.get("subject") or "(no subject)", "preview": thread.get("preview") or "",
        "mailbox": mailbox.get("address") or "", "lead_email": (lead.get("email") or "").lower(), "lead_name": lead.get("name") or "",
        "campaign": campaign or _name_of(thread.get("sequence")), "last_message_at": _when(thread.get("last_message_at")),
        "message_count": int(thread.get("message_count") or 0), "inbound_count": int(thread.get("inbound_count") or 0),
        "unread_count": int(thread.get("unread_count") or 0), "classification": _name_of(thread.get("reply_classification")) or None,
    }  # fmt: skip


def message_summary(message: dict[str, Any]) -> dict[str, Any]:
    sender = message.get("from") or {}
    return {
        "id": str(message.get("id") or ""), "direction": message.get("direction") or "",
        "from_name": sender.get("name") or "", "from_address": sender.get("address") or "",
        "to": [t.get("address") or "" for t in message.get("to") or [] if isinstance(t, dict)],
        "sent_at": _when(message.get("sent_at")), "subject": message.get("subject") or "",
        "body": message.get("body_text") or message.get("preview") or "",
    }  # fmt: skip
