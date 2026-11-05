"""Quick Start: one ICP from a Google Maps search to drafts waiting for review, in the background.

Each call to `advance` does one batch of one step as an ordinary job (background.run_job: the same endpoint the
pages use, run as the person who started the run, with their role and workspace checks), records what it made, and
says whether there's more. The Celery task (tasks.quickstart_step) then queues the next batch, so other people's jobs
get their turn in between. Spend is the total of the run's jobs' paid calls; the run stops at its limit.

Nothing is queued for sending: the run ends with drafts in review, approved by a person.

A run on existing companies (source "existing": imported ones, say) skips the search, and fills in only what's
missing before writing:

  find      the ICP's companies no one has written to yet, those with someone emailable first
  research  verifies only those never verified (imported ones count as verified)
  website   reads the websites not read yet (free: a plain request, Jina when a site needs a browser)
  people    LinkedIn at companies where no one has a verified email
  leads     the people to write to, never anyone already contacted
  profiles  the LinkedIn profiles of those people and the LinkedIn page and Instagram of their companies, where
            not read yet: only for who gets an email, so nothing is paid for anyone else
  posts     their company's LinkedIn and Instagram posts and their own LinkedIn posts
  write     their emails, left for review

A Google Maps run has no website or profiles step: verifying already reads the website and Instagram.

  find      Google Maps search for the ICP's terms around the places; the newest unresearched companies are kept
  research  verify them (website, Instagram, the ICP's rule, Claude's score), 10 at a time
  people    LinkedIn profiles at the qualified ones, with their work emails, 10 companies at a time
  leads     the people found become leads (only those with an email, unless asked otherwise)
  posts     their recent posts, 25 leads at a time (skipped when posts per lead is 0)
  write     their emails, 25 at a time
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

import background
from config import get_settings
from crud import IcpProfileRepo, JobRepo, ProviderCallRepo, QuickstartRepo
from database import get_engine, get_sessionmaker
from models import Job, QuickstartRun

log = logging.getLogger("coldbrew.quickstart")

STEPS = ["find", "research", "website", "people", "leads", "profiles", "posts", "write"]
LABELS = {
    "find": "Find companies", "research": "Verify companies", "website": "Read websites", "people": "Fetch LinkedIn profiles",
    "leads": "Make leads", "profiles": "Fill in profiles", "posts": "Read their posts", "write": "Write emails",
}  # fmt: skip
EXISTING_ONLY = ("website", "profiles")
WEBSITE_BATCH = 25
VERIFIED = ("verified", "valid")  # Apollo's word, and the LinkedIn finder's


def steps_for(settings: dict[str, Any]) -> list[str]:
    """The steps a run goes through: a Google Maps run has no website or profiles step."""
    return STEPS if settings.get("source") == "existing" else [s for s in STEPS if s not in EXISTING_ONLY]
RESEARCH_BATCH, PEOPLE_BATCH, POSTS_BATCH, WRITE_BATCH = 10, 10, 25, 25
CRASHED = "Something went wrong running it. The worker's log has the details."


async def advance(run_id: UUID) -> bool:
    """Do the run's next batch. True when there's more to do."""
    minutes = get_settings().job_time_limit_seconds // 60 + 10
    async with get_sessionmaker()() as session:
        repo = QuickstartRepo(session)
        run = await repo.claim(run_id, minutes)
        if run is None:
            return False  # finished, stopped, or another worker has it
        try:
            more = await _step(session, repo, run)
        except Exception:  # noqa: BLE001 - recorded on the run, so the page stops waiting
            log.exception("quick start %s crashed in %s", run_id, run.step)
            await session.rollback()
            run = await session.get(QuickstartRun, run_id, populate_existing=True)
            _fail(run, run.step, CRASHED)
            more = False
        run.busy_until = None
        if not more and run.status == "running":
            run.status = "done"
        if run.status not in ("running", "waiting"):
            run.finished_at = datetime.now(UTC)
        await repo.save(run)
        more = more and run.status == "running"
    await get_engine().dispose()
    return more


async def _step(session: Any, repo: QuickstartRepo, run: QuickstartRun) -> bool:
    run.spent_usd = await ProviderCallRepo(session).spent_by_jobs(list(run.job_ids))
    if float(run.spent_usd) >= float(run.spend_limit_usd):
        run.status = "stopped"
        run.detail = f"Stopped at the spend limit of ${float(run.spend_limit_usd):.2f}. What it made so far is below."
        _set(run, run.step, state="failed", note="Stopped at the spend limit")
        return False
    _set(run, run.step, state="running")
    await session.commit()
    return await {"find": _find, "research": _research, "website": _website, "people": _people, "leads": _leads,
                  "profiles": _profiles, "posts": _posts, "write": _write}[run.step](session, repo, run)  # fmt: skip


# ---------- Steps ----------


async def _find(session: Any, repo: QuickstartRepo, run: QuickstartRun) -> bool:
    s = run.settings
    if s.get("source") == "existing":
        list_id = UUID(s["smart_list_id"]) if s.get("smart_list_id") else None
        run.company_ids = await repo.existing_companies(run.workspace_id, run.icp_id, list_id, s["companies"])
        _set(run, "find", state="done", done=len(run.company_ids), total=s["companies"],
             note=f"{len(run.company_ids)} of your companies no one has written to yet")  # fmt: skip
        if not run.company_ids:
            return _finish(run, "Every company for this ICP already has someone written to.")
        return _next(run, "research")
    body = {"icpId": str(run.icp_id), "sources": ["google_maps"], "googleMaps": {
        "searchTerms": s["search_terms"], "locations": s["places"], "radiusMiles": s["radius_miles"], "maxSearches": s["google_requests"]}}  # fmt: skip
    # A search this run already finished (it failed just after, say) isn't paid for again.
    done = await _finished_job(session, run, "/pipeline/find")
    ok, out, detail = (True, done, None) if done is not None else await _job(session, run, "/pipeline/find", body, "Quick Start: finding companies")
    maps = (out or {}).get("googleMaps") or {}
    if not ok or maps.get("error"):
        return _fail(run, "find", detail or maps.get("error"))
    run.company_ids = await repo.new_companies(run.workspace_id, run.icp_id, s["companies"])
    note = f"{maps.get('found', 0)} found on Google Maps, {maps.get('added', 0)} new. {len(run.company_ids)} to verify."
    _set(run, "find", state="done", done=len(run.company_ids), total=s["companies"], note=note)
    if not run.company_ids:
        return _finish(run, "No new companies to verify. Try other search terms or places.")
    return _next(run, "research")


async def _research(session: Any, repo: QuickstartRepo, run: QuickstartRun) -> bool:
    companies = await repo.companies(list(run.company_ids))
    # Only new ones: imported companies count as verified already, and a decided company isn't paid for again.
    todo = [c.id for c in companies if c.researched_at is None and c.status == "found"][:RESEARCH_BATCH]
    if todo:
        ok, _, detail = await _job(session, run, "/pipeline/research", {"companyIds": [str(i) for i in todo]}, "Quick Start: verifying companies")
        if not ok:
            return _fail(run, "research", detail)
        companies = await repo.companies(list(run.company_ids))
    done = [c for c in companies if c.researched_at is not None or c.status != "found"]
    qualified = sum(c.status == "qualified" for c in companies)
    not_fit = sum(c.status == "not_fit" for c in companies)
    waiting = sum(c.status == "review" for c in companies)
    unfinished = sum(c.status == "error" for c in companies)
    note = f"{qualified} qualified, {not_fit} not a fit" + (f", {waiting} to approve" if waiting else "") + (
        f", {unfinished} couldn't finish" if unfinished else "")  # fmt: skip
    left = len(companies) - len(done)
    _set(run, "research", state="running" if left and todo else "done", done=len(done), total=len(companies), note=note)
    if left and todo:
        return True
    if waiting:
        # Like email review: companies under the threshold wait for a person. The run picks up again when they
        # press Continue on its page (resume), and waits again if any are still undecided.
        _set(run, "research", state="waiting", note=note)
        run.status = "waiting"
        run.detail = f"Waiting for you to approve or reject {waiting} {'company' if waiting == 1 else 'companies'}, then press Continue."
        return False
    if not qualified:
        return _finish(run, "None of the companies qualified for this ICP, so there's no one to write to.")
    return _next(run, "website" if run.settings.get("source") == "existing" else "people")


async def _website(session: Any, repo: QuickstartRepo, run: QuickstartRun) -> bool:
    """Read the websites of the qualified companies that haven't been read yet. Free."""
    tried = set(run.progress.get("_sited", []))
    companies = await repo.companies(list(run.company_ids))
    qualified = [c.id for c in companies if c.status == "qualified"]
    unread = set(await repo.without_pages(qualified))
    todo = [i for i in qualified if i in unread and str(i) not in tried][:WEBSITE_BATCH]
    read, failed = run.progress.get("_sites", [0, 0])
    if todo:
        ok, out, detail = await _job(session, run, "/pipeline/enrich", {"companyIds": [str(i) for i in todo], "website": True},
                                     "Quick Start: reading websites")  # fmt: skip
        if not ok:
            return _fail(run, "website", detail)
        read, failed = read + (out or {}).get("sitesRead", 0), failed + (out or {}).get("sitesFailed", 0)
        run.progress = {**run.progress, "_sited": sorted(tried | {str(i) for i in todo}), "_sites": [read, failed]}
    tried = set(run.progress.get("_sited", []))
    unread = set(await repo.without_pages(qualified))
    left = [i for i in qualified if i in unread and str(i) not in tried]
    # Counted from the pages saved, not added up from each batch: a batch that crashed and was tried again had
    # already saved some sites, which were then counted as read before this run (2026-11-05).
    already = len(await repo.read_before(qualified, run.created_at))
    read = len(qualified) - len(unread) - already
    failed = sum(1 for i in qualified if i in unread and str(i) in tried)
    note = ", ".join(x for x in (f"{already} read before" if already else "", f"{read} read now" if read else "",
                                 f"{failed} couldn't be reached" if failed else "") if x) or "No websites to read"  # fmt: skip
    _set(run, "website", state="running" if left else "done", done=len(qualified) - len(left), total=len(qualified), note=note)
    return True if left else _next(run, "people")


async def _people(session: Any, repo: QuickstartRepo, run: QuickstartRun) -> bool:
    s = run.settings
    searched = set(run.progress.get("_searched", []))
    companies = await repo.companies(list(run.company_ids))
    qualified = [c.id for c in companies if c.status == "qualified"]
    if "_had" not in run.progress:
        # Who was here before LinkedIn was asked (imported with their company, say), so the note can tell them apart.
        here = await repo.people_at(run.workspace_id, list(run.company_ids))
        run.progress = {**run.progress, "_had": sorted(str(p.id) for p in here)}
    if s.get("source") == "existing":
        # Their people are usually here already (imported with them): LinkedIn only where no one has a verified
        # email, and only when asked.
        emailed = {p.company_id for p in await repo.people_at(run.workspace_id, qualified) if p.email and p.email_status in VERIFIED}
        lookup = [i for i in qualified if i not in emailed] if s.get("find_people", True) else []
        searched |= {str(i) for i in qualified if i not in lookup}
        run.progress = {**run.progress, "_searched": sorted(searched)}
    batch = [i for i in qualified if str(i) not in searched][:PEOPLE_BATCH]
    if batch:
        titles = s.get("titles") or await _icp_titles(session, run)
        body = {"companyIds": [str(i) for i in batch], "source": "linkedin", "titles": titles,
                "maxPerCompany": s["people_per_company"], "findEmails": s["find_emails"]}  # fmt: skip
        ok, out, detail = await _job(session, run, "/pipeline/stakeholders", body, "Quick Start: finding people")
        if not ok or (out or {}).get("error"):
            return _fail(run, "people", detail or (out or {}).get("error"))
        run.progress = {**run.progress, "_searched": sorted(searched | {str(i) for i in batch})}
    people = await repo.people_at(run.workspace_id, list(run.company_ids))
    left = len([i for i in qualified if str(i) not in set(run.progress.get("_searched", []))])
    if not left:
        # Small shops are often not on LinkedIn but publish an address on their site (orders@, info@), which
        # research already read: that's the contact for a qualified company where no one with an email was found.
        emailed = {p.company_id for p in people if p.email}
        fallback = [(c, email) for c in companies
                    if c.status == "qualified" and c.id not in emailed and (email := _site_email(c))]  # fmt: skip
        if fallback:
            await repo.add_website_contacts(run.workspace_id, fallback)
            people = await repo.people_at(run.workspace_id, list(run.company_ids))
    had = set(run.progress.get("_had", []))
    already = [p for p in people if str(p.id) in had]
    found = [p for p in people if str(p.id) not in had and p.email_status != "website"]
    from_site = sum(1 for p in people if str(p.id) not in had and p.email_status == "website")
    parts = [f"{len(already)} already here"] if already else []
    parts.append(f"{len(found)} found on LinkedIn")
    if s["find_emails"] or already:
        parts.append(f"{sum(1 for p in [*already, *found] if p.email)} with an email")
    note = ", ".join(parts)
    if from_site:
        note += f"; {from_site} compan{'ies' if from_site != 1 else 'y'} reached by the email on {'their' if from_site != 1 else 'its'} website"
    _set(run, "people", state="running" if left else "done", done=len(qualified) - left, total=len(qualified), note=note)
    if left:
        return True
    if not people:
        return _finish(run, "No one was found on LinkedIn at the qualified companies, and none of their websites lists an email.")
    return _next(run, "leads")


def _site_email(company: Any) -> str | None:
    research = getattr(company, "research", None) or {}
    return (research.get("contacts") or {}).get("email")


async def _leads(session: Any, repo: QuickstartRepo, run: QuickstartRun) -> bool:
    s = run.settings
    people = await repo.people_at(run.workspace_id, list(run.company_ids))
    room = s["leads"] - len(run.lead_ids)
    # Someone with a working record already (their posts read, or got ready on People) is reused, unless an email
    # was already written for them; only the others need one made.
    drafted = {e.lead_id for e in await repo.first_emails([p.lead_id for p in people if p.lead_id])}
    taken = set(run.lead_ids)
    contacted = await repo.contacted(run.workspace_id, people)
    eligible = [p for p in people if p.lead_id not in drafted and p.lead_id not in taken and p.id not in contacted
                and (p.email or not s["only_with_email"]) and (p.email or p.linkedin_url)]  # fmt: skip
    # No more than the people per company asked for (imported companies can hold many): verified emails first,
    # then any email.
    per: dict[Any, int] = {}
    picked = []
    for p in sorted(eligible, key=lambda p: (p.email_status not in VERIFIED, not p.email)):
        if per.get(p.company_id, 0) < s["people_per_company"]:
            per[p.company_id] = per.get(p.company_id, 0) + 1
            picked.append(p)
    picked = picked[: max(room, 0)]
    new = [p for p in picked if p.lead_id is None]
    if new:
        ok, _, detail = await _job(session, run, "/stakeholders/add-as-leads", {"stakeholderIds": [str(p.id) for p in new]},
                                   "Quick Start: making leads")  # fmt: skip
        if not ok:
            return _fail(run, "leads", detail)
        people = await repo.people_at(run.workspace_id, list(run.company_ids))
    chosen = {p.id for p in picked}
    lead_ids = list(dict.fromkeys([*run.lead_ids, *(p.lead_id for p in people if p.id in chosen and p.lead_id)]))[: s["leads"]]
    run.lead_ids = lead_ids
    leads = await repo.leads(lead_ids)
    valid = sum(1 for lead in leads if lead.email and lead.email_status in (*VERIFIED, "website"))
    risky = sum(1 for lead in leads if lead.email and lead.email_status == "risky")
    no_email = sum(1 for lead in leads if not lead.email)
    skipped = len([p for p in people if p.id in contacted])
    note = f"{len(leads)} leads: {valid} valid email{'s' if valid != 1 else ''}, {risky} risky" + (f", {no_email} without one" if no_email else "")
    if skipped:
        note += f". {skipped} already contacted, skipped"
    _set(run, "leads", state="done", done=len(leads), total=s["leads"], note=note)
    if not leads:
        return _finish(run, "No one had an email address, so no leads were made." if s["only_with_email"] else "No leads were made.")
    if s.get("source") == "existing":
        return _next(run, "profiles")
    return _after_profiles(run)


def _after_profiles(run: QuickstartRun) -> bool:
    if run.settings["posts_per_lead"] == 0:
        _set(run, "posts", state="skipped", note="Posts per lead is 0")
        return _next(run, "write")
    return _next(run, "posts")


async def _profiles(session: Any, repo: QuickstartRepo, run: QuickstartRun) -> bool:
    """LinkedIn profiles of the people being written to, and their companies' LinkedIn pages and Instagram, where
    not read yet (the enrich endpoints skip what's there, so nothing is bought twice)."""
    people = [p for p in await repo.people_at(run.workspace_id, list(run.company_ids)) if p.lead_id in set(run.lead_ids)]
    done = dict(run.progress.get("_profiled", {}))
    if "companies" not in done:
        ids = sorted({str(p.company_id) for p in people if p.company_id})
        out = {}
        if ids:
            ok, out, detail = await _job(session, run, "/pipeline/enrich", {"companyIds": ids, "instagram": True, "linkedin": True},
                                         "Quick Start: company profiles")  # fmt: skip
            if not ok:
                return _fail(run, "profiles", detail)
        done["companies"] = [(out or {}).get("instagram", 0), (out or {}).get("linkedinCompanies", 0)]
        run.progress = {**run.progress, "_profiled": done}
        _set(run, "profiles", state="running", done=0, total=len(people),
             note=f"{done['companies'][0]} Instagram and {done['companies'][1]} LinkedIn company pages read")  # fmt: skip
        return True
    if "people" not in done:
        ids = [str(p.id) for p in people if p.linkedin_url]
        out = {}
        if ids:
            ok, out, detail = await _job(session, run, "/stakeholders/enrich", {"stakeholderIds": ids}, "Quick Start: people's profiles")
            if not ok:
                return _fail(run, "profiles", detail)
        done["people"] = (out or {}).get("linkedinPeople", 0)
        run.progress = {**run.progress, "_profiled": done}
    ig, li = done["companies"]
    note = f"{done['people']} people's LinkedIn profiles, {ig} Instagram and {li} LinkedIn company pages read; the rest were read before"
    _set(run, "profiles", state="done", done=len(people), total=len(people), note=note)
    return _after_profiles(run)


async def _posts(session: Any, repo: QuickstartRepo, run: QuickstartRun) -> bool:
    s = run.settings
    tried = set(run.progress.get("_fetched", []))
    sources = await _post_sources(session, run)
    leads = await repo.leads(list(run.lead_ids))
    # Every lead: their company's LinkedIn and Instagram are read too, not only their own (services/smart_posts.py).
    todo = [lead.id for lead in leads if str(lead.id) not in tried and lead.status in ("imported", "failed")][:POSTS_BATCH]
    if todo and sources:
        body = {"leadIds": [str(i) for i in todo], "maxPosts": s["posts_per_lead"], "maxAgeDays": s["max_post_age_days"], "sources": sources}
        ok, out, detail = await _job(session, run, "/posts/fetch", body, "Quick Start: reading posts")
        if not ok:
            return _fail(run, "posts", detail)
        run.progress = {**run.progress, "_fetched": sorted(tried | {str(i) for i in todo})}
        if (out or {}).get("error"):
            _set(run, "posts", note=f"Apify: {(out or {})['error']}")
    counts = await repo.post_counts(list(run.lead_ids))
    tried = set(run.progress.get("_fetched", []))
    left = [lead.id for lead in leads if str(lead.id) not in tried and lead.status in ("imported", "failed")]
    note = f"{sum(counts.values())} posts from {sum(1 for n in counts.values() if n)} of {len(leads)} leads"
    if not sources:
        note = "LinkedIn and Instagram are both off for this ICP"
    _set(run, "posts", state="running" if left and sources else "done", done=len(tried), total=len(leads), note=note)
    return True if left and sources else _next(run, "write")


async def _write(session: Any, repo: QuickstartRepo, run: QuickstartRun) -> bool:
    tried = set(run.progress.get("_written", []))
    leads = await repo.leads(list(run.lead_ids))
    drafted = {e.lead_id for e in await repo.first_emails(list(run.lead_ids))}
    todo = [lead.id for lead in leads if lead.id not in drafted and str(lead.id) not in tried][:WRITE_BATCH]
    reasons = list(run.progress.get("_skipped", []))
    if todo:
        ok, out, detail = await _job(session, run, "/emails/generate", {"leadIds": [str(i) for i in todo]}, "Quick Start: writing emails")
        if not ok:
            return _fail(run, "write", detail)
        reasons += [r["reason"] for r in (out or {}).get("results", []) if r.get("status") != "drafted" and r.get("reason")]
        run.progress = {**run.progress, "_written": sorted(tried | {str(i) for i in todo}), "_skipped": sorted(set(reasons))[:5]}
        drafted = {e.lead_id for e in await repo.first_emails(list(run.lead_ids))}
    left = [lead.id for lead in leads if lead.id not in drafted and str(lead.id) not in set(run.progress.get("_written", []))]
    note = f"{len(drafted)} draft{'s' if len(drafted) != 1 else ''} waiting for review" + (f". Skipped: {' '.join(sorted(set(reasons))[:2])}" if reasons else "")
    _set(run, "write", state="running" if left else "done", done=len(drafted), total=len(leads), note=note)
    if left:
        return True
    return _finish(run, f"Done: {len(drafted)} draft{'s are' if len(drafted) != 1 else ' is'} waiting for your review below.")


# ---------- Helpers ----------


async def _job(session: Any, run: QuickstartRun, path: str, body: dict[str, Any], label: str) -> tuple[bool, Any, str | None]:
    """Run one endpoint as an ordinary job, as the run's starter, and read its answer."""
    job = await JobRepo(session).create(run.workspace_id, run.created_by, path, label, {"path": path, "body": body})
    run.job_ids = [*run.job_ids, job.id]
    await session.commit()
    await background.run_job(job.id)
    job = await session.get(Job, job.id, populate_existing=True)  # the worker's answer, not the copy from before
    result = (job.result or {}) if job else {}
    return bool(job and job.status == "done"), result.get("body"), job.detail if job else "The job disappeared."


async def _finished_job(session: Any, run: QuickstartRun, path: str) -> Any | None:
    """The answer of a job of this kind this run already finished, or None."""
    if not run.job_ids:
        return None
    job = await session.scalar(
        select(Job).where(Job.id.in_(list(run.job_ids)), Job.kind == path, Job.status == "done").order_by(Job.finished_at.desc()).limit(1)
    )
    return ((job.result or {}).get("body") or {}) if job else None


async def _icp_titles(session: Any, run: QuickstartRun) -> list[str]:
    profile = (await IcpProfileRepo(session).get_many(run.workspace_id, [run.icp_id])).get(run.icp_id)
    return list(profile.titles) if profile and profile.titles else ["Owner", "Founder", "CEO", "President", "General Manager"]


async def _post_sources(session: Any, run: QuickstartRun) -> list[str]:
    profile = (await IcpProfileRepo(session).get_many(run.workspace_id, [run.icp_id])).get(run.icp_id)
    on = set(profile.enabled_sources) if profile and profile.enabled_sources else {"linkedin", "instagram"}
    return [s for s in ("linkedin", "instagram") if s in on]


def _set(run: QuickstartRun, step: str, **values: Any) -> None:
    progress = dict(run.progress)
    progress[step] = {**progress.get(step, {}), **values}
    run.progress = progress


def _next(run: QuickstartRun, step: str) -> bool:
    run.step = step
    return True


def _finish(run: QuickstartRun, detail: str) -> bool:
    run.detail = detail
    order = steps_for(run.settings)
    for step in order[order.index(run.step) + 1 :]:
        if run.progress.get(step, {}).get("state") in (None, "waiting"):
            _set(run, step, state="skipped")
    run.step = "done"
    return False


def _fail(run: QuickstartRun, step: str, detail: str | None) -> bool:
    _set(run, step, state="failed", note=detail or "It failed.")
    run.status = "failed"
    run.detail = f"{LABELS.get(step, step)} failed: {detail or 'no details'}"
    return False
