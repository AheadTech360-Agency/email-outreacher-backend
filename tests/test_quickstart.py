"""Quick Start's steps (services/quickstart.py), on an in-memory world: each step's job is faked, so no network,
no database, nothing spent."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services import quickstart

WS, ICP = uuid4(), uuid4()


class World:
    """Companies, people, leads and drafts, changed the way each endpoint would change them."""

    def __init__(self) -> None:
        self.companies = {}
        self.people = {}
        self.leads = {}
        self.emails = []
        self.posts = {}
        self.calls = []
        self.spent = 0.0
        self.finished = {}  # path -> the answer of a job this run already finished
        self.sites: set[str] = set()  # companies whose website was read
        self.sites_before: set[str] = set()  # ...before the run started
        self.contacted: set = set()  # people already emailed
        for name in ("Ink Co", "Tee Shop", "Big Box"):
            c = SimpleNamespace(id=uuid4(), name=name, domain=None, status="found", status_reason=None, score=None, researched_at=None)
            self.companies[c.id] = c

    async def job(self, session, run, path, body, label):
        self.calls.append(path)
        run.job_ids = [*run.job_ids, uuid4()]
        if path == "/pipeline/find":
            return True, {"googleMaps": {"found": 20, "added": 3, "error": None}}, None
        if path == "/pipeline/research":
            for cid in body["companyIds"]:
                c = next(c for c in self.companies.values() if str(c.id) == cid)
                c.researched_at, c.status = "now", ("not_fit" if c.name == "Big Box" else "qualified")
            self.spent += 0.01
            return True, {}, None
        if path == "/pipeline/stakeholders":
            assert body["findEmails"] is True and body["source"] == "linkedin"
            for cid in body["companyIds"]:
                c = next(c for c in self.companies.values() if str(c.id) == cid)
                risky = c.name == "Tee Shop"
                p = SimpleNamespace(id=uuid4(), company_id=c.id, lead_id=None, linkedin_url="https://linkedin.com/in/x",
                                    email=f"owner@{c.name.split()[0].lower()}.com", email_status="risky" if risky else "valid")  # fmt: skip
                self.people[p.id] = p
            self.spent += 0.05
            return True, {"results": [], "error": None}, None
        if path == "/stakeholders/add-as-leads":
            for sid in body["stakeholderIds"]:
                p = next(p for p in self.people.values() if str(p.id) == sid)
                lead = SimpleNamespace(id=uuid4(), first_name="Owner", last_name="", title="Owner", company="", email=p.email,
                                       email_status=p.email_status, status="imported", linkedin_url=p.linkedin_url,
                                       instagram_url=None, score=None)  # fmt: skip
                self.leads[lead.id] = lead
                p.lead_id = lead.id
            return True, {}, None
        if path == "/posts/fetch":
            for lid in body["leadIds"]:
                lead = next(x for x in self.leads.values() if str(x.id) == lid)
                lead.status = "enriched"
                self.posts[lead.id] = 3
            return True, {"error": None}, None
        if path == "/pipeline/enrich":
            if body.get("website"):
                for cid in body["companyIds"]:
                    self.sites.add(cid)
                return True, {"sitesRead": len(body["companyIds"]), "sitesFailed": 0}, None
            return True, {"instagram": 1, "linkedinCompanies": 1}, None
        if path == "/stakeholders/enrich":
            return True, {"linkedinPeople": len(body["stakeholderIds"])}, None
        if path == "/emails/generate":
            for lid in body["leadIds"]:
                self.emails.append(SimpleNamespace(id=uuid4(), lead_id=next(x.id for x in self.leads.values() if str(x.id) == lid)))
            return True, {"results": [{"status": "drafted"} for _ in body["leadIds"]]}, None
        raise AssertionError(path)


class Repo:
    def __init__(self, world: World) -> None:
        self.w = world

    async def new_companies(self, workspace_id, icp_id, limit):
        return [c.id for c in self.w.companies.values() if c.researched_at is None][:limit]

    async def existing_companies(self, workspace_id, icp_id, smart_list_id, limit):
        return [c.id for c in self.w.companies.values()][:limit]

    async def without_pages(self, ids):
        return [i for i in ids if str(i) not in self.w.sites]

    async def read_before(self, ids, since):
        return {i for i in ids if str(i) in self.w.sites_before}

    async def contacted(self, workspace_id, people):
        return {p.id for p in people if p.id in self.w.contacted}

    async def companies(self, ids):
        return [self.w.companies[i] for i in ids]

    async def add_website_contacts(self, workspace_id, contacts):
        for company, email in contacts:
            p = SimpleNamespace(id=uuid4(), company_id=company.id, lead_id=None, linkedin_url=None, email=email, email_status="website")
            self.w.people[p.id] = p

    async def people_at(self, workspace_id, company_ids):
        return [p for p in self.w.people.values() if p.company_id in company_ids]

    async def leads(self, ids):
        return [self.w.leads[i] for i in ids if i in self.w.leads]

    async def first_emails(self, lead_ids):
        return [e for e in self.w.emails if e.lead_id in lead_ids]

    async def post_counts(self, lead_ids):
        return {i: self.w.posts.get(i, 0) for i in lead_ids}


class Session:
    async def commit(self) -> None:
        pass


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(quickstart, "_job", w.job)

    async def none_finished(session, run, path):
        return w.finished.get(path)

    monkeypatch.setattr(quickstart, "_finished_job", none_finished)

    class Calls:
        def __init__(self, session) -> None:
            pass

        async def spent_by_jobs(self, job_ids):
            return w.spent

    monkeypatch.setattr(quickstart, "ProviderCallRepo", Calls)

    async def titles(session, run):
        return ["Owner"]

    async def sources(session, run):
        return ["linkedin"]

    monkeypatch.setattr(quickstart, "_icp_titles", titles)
    monkeypatch.setattr(quickstart, "_post_sources", sources)
    return w


def new_run(limit=5.0, **settings) -> SimpleNamespace:
    s = {"search_terms": ["screen printing"], "places": ["Austin, TX"], "radius_miles": 5, "google_requests": 4, "companies": 10,
         "people_per_company": 1, "titles": [], "find_emails": True, "leads": 20, "only_with_email": True, "posts_per_lead": 5,
         "max_post_age_days": 60, **settings}  # fmt: skip
    return SimpleNamespace(id=uuid4(), workspace_id=WS, icp_id=ICP, status="running", step="find", settings=s, progress={},
                           company_ids=[], lead_ids=[], job_ids=[], spent_usd=0, spend_limit_usd=limit, detail=None,
                           created_at=None)  # fmt: skip


def run_all(run, world) -> int:
    rounds = 0
    while asyncio.run(quickstart._step(Session(), Repo(world), run)):
        rounds += 1
        assert rounds < 50, "the run never finished"
    return rounds


def test_a_run_goes_from_google_maps_to_drafts_in_review(world) -> None:
    run = new_run()
    run_all(run, world)
    assert world.calls == ["/pipeline/find", "/pipeline/research", "/pipeline/stakeholders", "/stakeholders/add-as-leads",
                           "/posts/fetch", "/emails/generate"]  # fmt: skip
    assert run.step == "done" and run.status == "running"  # advance() marks it done
    assert run.detail.startswith("Done: 2 drafts are waiting")
    steps = run.progress
    assert steps["research"]["note"] == "2 qualified, 1 not a fit"
    assert steps["leads"]["note"] == "2 leads: 1 valid email, 1 risky"
    assert steps["write"]["done"] == 2 and all(steps[s]["state"] == "done" for s in quickstart.steps_for(run.settings))
    assert len(run.lead_ids) == 2 and len(run.company_ids) == 3


def test_it_stops_at_the_spend_limit_and_keeps_what_it_made(world) -> None:
    run = new_run(limit=0.04)
    run_all(run, world)
    assert run.status == "stopped" and "spend limit" in run.detail
    assert "/pipeline/stakeholders" in world.calls and "/emails/generate" not in world.calls


def test_no_posts_step_when_posts_per_lead_is_0(world) -> None:
    run = new_run(posts_per_lead=0)
    run_all(run, world)
    assert "/posts/fetch" not in world.calls and run.progress["posts"]["state"] == "skipped"
    assert run.detail.startswith("Done: 2 drafts")


def test_the_lead_cap_is_kept(world) -> None:
    run = new_run(leads=1)
    run_all(run, world)
    assert len(run.lead_ids) == 1 and run.detail.startswith("Done: 1 draft is waiting")


def test_a_failed_step_fails_the_run_with_the_reason(world, monkeypatch) -> None:
    async def refuse(session, run, path, body, label):
        return False, None, "GOOGLE_PLACES_API_KEY is not set"

    monkeypatch.setattr(quickstart, "_job", refuse)
    run = new_run()
    assert asyncio.run(quickstart._step(Session(), Repo(world), run)) is False
    assert run.status == "failed" and run.detail == "Find companies failed: GOOGLE_PLACES_API_KEY is not set"


def test_the_fake_repo_matches_the_real_one() -> None:
    """The steps run against the real QuickstartRepo in production: a fake that takes other arguments would hide a crash
    (as it did on 2026-11-02, when new_companies took one argument more than the steps passed)."""
    import inspect

    from crud import QuickstartRepo

    for name, fake in inspect.getmembers(Repo, inspect.isfunction):
        if name == "__init__":
            continue
        real = getattr(QuickstartRepo, name)
        assert list(inspect.signature(fake).parameters) == list(inspect.signature(real).parameters), name


def test_the_api_speaks_camel_case_settings() -> None:
    from routers.quickstart import _out

    run = new_run()
    run.created_at, run.finished_at = "2026-11-02T12:00:00Z", None
    assert _out(run, "DTF Transfers")["settings"]["searchTerms"] == ["screen printing"]


def test_a_search_the_run_already_finished_is_not_paid_for_again(world) -> None:
    """Tried again after failing just after its Google search (2026-11-02): the search's answer is reused."""
    world.finished["/pipeline/find"] = {"googleMaps": {"found": 2, "added": 2, "error": None}}
    run = new_run()
    asyncio.run(quickstart._step(Session(), Repo(world), run))
    assert "/pipeline/find" not in world.calls and run.step == "research" and len(run.company_ids) == 3


def test_a_company_not_on_linkedin_is_reached_by_the_email_on_its_website(world, monkeypatch) -> None:
    """2026-11-02: DTF shops had no one on LinkedIn but listed orders@ on their sites; the run made no leads."""
    real_job = world.job

    async def nobody_on_linkedin(session, run, path, body, label):
        if path == "/pipeline/stakeholders":
            run.job_ids = [*run.job_ids, uuid4()]
            return True, {"results": [], "error": None}, None
        return await real_job(session, run, path, body, label)

    monkeypatch.setattr(quickstart, "_job", nobody_on_linkedin)
    for c in world.companies.values():
        c.research = {"contacts": {"email": f"orders@{c.name.split()[0].lower()}.com"}}
    run = new_run()
    run_all(run, world)
    assert run.progress["people"]["note"] == "0 found on LinkedIn, 0 with an email; 2 companies reached by the email on their website"
    assert run.progress["leads"]["note"] == "2 leads: 2 valid emails, 0 risky"
    assert run.detail.startswith("Done: 2 drafts")


def test_it_waits_for_companies_to_be_approved_then_continues(world, monkeypatch) -> None:
    """Companies under the threshold wait for a person, as emails do; the run continues once they're decided."""
    real_job = world.job

    async def research_to_review(session, run, path, body, label):
        out = await real_job(session, run, path, body, label)
        if path == "/pipeline/research":
            for c in world.companies.values():
                if c.name == "Big Box":
                    c.status = "review"
        return out

    monkeypatch.setattr(quickstart, "_job", research_to_review)
    run = new_run()
    run_all(run, world)
    assert run.status == "waiting" and run.step == "research" and "approve or reject 1 company" in run.detail
    assert "/pipeline/stakeholders" not in world.calls
    # Rejected by hand, then Continue (resume sets it running again).
    next(c for c in world.companies.values() if c.name == "Big Box").status = "not_fit"
    run.status = "running"
    run_all(run, world)
    assert "/pipeline/stakeholders" in world.calls and run.detail.startswith("Done: 2 drafts")


def test_a_run_on_imported_companies_skips_the_search_and_verifying(world) -> None:
    """Imported companies are verified already and their people came with them: the run goes straight to leads,
    posts and emails, looks on LinkedIn only where no one has an email, and keeps to the people per company."""
    ink, tee, big = world.companies.values()
    for c in (ink, tee, big):
        c.status = "qualified"
    for c, emails in ((ink, ["a@ink.com", "b@ink.com"]), (tee, []), (big, ["c@big.com"])):
        for email in emails:
            p = SimpleNamespace(id=uuid4(), company_id=c.id, lead_id=None, linkedin_url=None, email=email, email_status="valid")
            world.people[p.id] = p
    run = new_run(source="existing", smart_list_id=None, search_terms=[], places=[])
    run_all(run, world)
    assert "/pipeline/find" not in world.calls and "/pipeline/research" not in world.calls
    assert world.calls.count("/pipeline/stakeholders") == 1  # only Tee Shop, which had no one with an email
    assert run.progress["find"]["note"] == "3 of your companies no one has written to yet"
    assert run.progress["people"]["note"] == "3 already here, 1 found on LinkedIn, 4 with an email"
    assert len(run.lead_ids) == 3  # one per company: Ink Co's second person waits
    assert run.detail.startswith("Done: 3 drafts")


def test_people_already_worked_on_are_reused_and_those_written_to_are_not(world) -> None:
    """Someone whose posts were read on People has a working record: the run writes to them without making another
    or reading their posts again. Someone already written to is left alone."""
    ink, tee, big = world.companies.values()
    for c in (ink, tee, big):
        c.status = "qualified"
    done_before = SimpleNamespace(id=uuid4(), first_name="Ann", last_name="", title="Owner", company="", email="ann@ink.com",
                                  email_status="valid", status="enriched", linkedin_url=None, instagram_url=None, score=None)  # fmt: skip
    written = SimpleNamespace(id=uuid4(), first_name="Bo", last_name="", title="Owner", company="", email="bo@tee.com",
                              email_status="valid", status="generated", linkedin_url=None, instagram_url=None, score=None)  # fmt: skip
    world.leads[done_before.id], world.leads[written.id] = done_before, written
    world.emails.append(SimpleNamespace(id=uuid4(), lead_id=written.id))
    for c, lead in ((ink, done_before), (tee, written)):
        p = SimpleNamespace(id=uuid4(), company_id=c.id, lead_id=lead.id, linkedin_url=None, email=lead.email, email_status="valid")
        world.people[p.id] = p
    p = SimpleNamespace(id=uuid4(), company_id=big.id, lead_id=None, linkedin_url=None, email="cy@big.com", email_status="valid")
    world.people[p.id] = p
    run = new_run(source="existing", smart_list_id=None, search_terms=[], places=[], find_people=False)
    run_all(run, world)
    assert done_before.id in run.lead_ids and written.id not in run.lead_ids and len(run.lead_ids) == 2
    assert world.calls.count("/stakeholders/add-as-leads") == 1  # only for Big Box's person
    assert world.calls.count("/posts/fetch") == 1 and run.detail.startswith("Done: 2 drafts")


def test_an_existing_run_fills_in_before_writing_and_skips_anyone_contacted(world) -> None:
    """Websites not read yet, LinkedIn only where no one has a verified email, profiles only for who's written to,
    then posts and emails. Someone already emailed is never picked."""
    ink, tee, big = world.companies.values()
    for c in (ink, tee, big):
        c.status = "qualified"
    world.sites.add(str(ink.id))  # Ink Co's site was read before
    world.sites_before.add(str(ink.id))
    emailed = SimpleNamespace(id=uuid4(), company_id=ink.id, lead_id=None, linkedin_url="https://linkedin.com/in/a", email="a@ink.com", email_status="verified")
    unverified = SimpleNamespace(id=uuid4(), company_id=big.id, lead_id=None, linkedin_url=None, email="b@big.com", email_status="extrapolated")
    ready = SimpleNamespace(id=uuid4(), company_id=ink.id, lead_id=None, linkedin_url="https://linkedin.com/in/c", email="c@ink.com", email_status="verified")
    for p in (emailed, unverified, ready):
        world.people[p.id] = p
    world.contacted.add(emailed.id)
    run = new_run(source="existing", smart_list_id=None, search_terms=[], places=[])
    run_all(run, world)
    # One LinkedIn search, for Tee Shop (no one) and Big Box (an unverified email) together; Ink Co is skipped.
    assert world.calls == ["/pipeline/enrich", "/pipeline/stakeholders", "/stakeholders/add-as-leads",
                           "/pipeline/enrich", "/stakeholders/enrich", "/posts/fetch", "/emails/generate"]  # fmt: skip
    assert run.progress["website"]["note"] == "1 read before, 2 read now"
    picked = {p.id for p in world.people.values() if p.lead_id in set(run.lead_ids)}
    assert emailed.id not in picked and ready.id in picked
    assert "1 already contacted, skipped" in run.progress["leads"]["note"]
    assert run.detail.startswith("Done: 3 drafts")


def test_a_past_run_shows_only_the_steps_it_went_through() -> None:
    from routers.quickstart import _out

    run = new_run(source="existing")
    run.status, run.progress = "done", {s: {"state": "done"} for s in ("find", "research", "people", "leads", "posts", "write")}
    run.created_at, run.finished_at = "2026-11-04T10:00:00Z", None
    assert _out(run, "Retail")["step_order"] == ["find", "research", "people", "leads", "posts", "write"]


def test_sites_a_crashed_batch_saved_are_counted_as_read_now_not_before(world) -> None:
    """2026-11-05: a batch crashed after saving some sites; when it ran again the note said "83 read before, 82 read
    now" for 100 companies that had none read before."""
    ink, tee, big = world.companies.values()
    for c in (ink, tee, big):
        c.status = "qualified"
    world.sites.add(str(ink.id))  # saved by the batch that crashed: read during this run, not before
    run = new_run(source="existing", smart_list_id=None, search_terms=[], places=[])
    run_all(run, world)
    assert run.progress["website"]["note"] == "3 read now"
