"""LinkedIn search, add-as-leads and post fetching, with fake Apify and fake repos. No network, no cost."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dependencies import get_apify, get_post_repo
from main import app
from services.apify import ApifyError, Employee, FetchedPost, RunResult
from tests.conftest import COMPANY_ID, WORKSPACE_ID
from tests.test_stakeholders_api import WS, apollo_env  # noqa: F401  (fixture)


class FakeApify:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.error: ApifyError | None = None
        self.linkedin_items: list[FetchedPost] = []
        self.instagram_items: list[FetchedPost] = []

    async def company_employees(self, company, titles, max_items, with_email=False, linkedin_url=None, domain=None):
        if self.error:
            raise self.error
        self.calls.append(("employees", company, titles, max_items, with_email))
        people = [
            Employee("e1", "Ava", "Morgan", "Owner", "https://www.linkedin.com/in/ava-morgan", "Glow Med Spa", {},
                     *(("ava@glowmedspa.com", "valid") if with_email else ())),
            Employee("e2", "Bo", "Chen", "Founder", None, "Glow Med Spa", {}),
        ]  # fmt: skip
        return people, RunResult([], 0.008, "run-e")

    async def linkedin_posts(self, urls, max_posts, since):
        self.calls.append(("linkedin", tuple(urls), max_posts))
        return self.linkedin_items, RunResult([], 0.004, "run-li")

    async def instagram_posts(self, urls, max_posts, since):
        if self.error:
            raise self.error
        self.calls.append(("instagram", tuple(urls), max_posts))
        return self.instagram_items, RunResult([], 0.003, "run-ig")


def post(source, url, owner):
    return FetchedPost(source, url, None, datetime(2026, 10, 1, tzinfo=UTC), f"text {url}", "text", False, owner, {})


@pytest.fixture
def apify(client, apollo_env):  # noqa: F811
    fake = FakeApify()
    app.dependency_overrides[get_apify] = lambda: fake
    return fake


# ---------- LinkedIn search and add as leads ----------


def test_linkedin_search_saves_full_names_by_company_name(client, apify, apollo_env) -> None:  # noqa: F811
    body = {"companyIds": [str(COMPANY_ID)], "titles": ["Owner", " "], "maxPerCompany": 5}
    result = client.post("/api/v1/stakeholders/search-linkedin", json=body, headers=WS).json()
    assert result["error"] is None and result["costUsd"] == 0.008
    assert result["results"][0]["found"] == 2 and result["results"][0]["new"] == 2
    assert apify.calls == [("employees", "Glow Med Spa", ["Owner"], 5, True)]  # emails searched for by default
    people = client.get("/api/v1/stakeholders", headers=WS).json()
    assert {(p["firstName"], p["lastName"], p["source"]) for p in people} == {
        ("Ava", "Morgan", "apify_linkedin"), ("Bo", "Chen", "apify_linkedin"),
    }  # fmt: skip


def test_linkedin_search_stops_when_apify_is_out_of_credit(client, apify, apollo_env) -> None:  # noqa: F811
    apify.error = ApifyError("Apify says the account is out of credit", 402)
    result = client.post("/api/v1/stakeholders/search-linkedin", json={"companyIds": [str(COMPANY_ID)]}, headers=WS).json()
    assert result["error"] == "Apify says the account is out of credit"
    assert result["results"][0]["status"] == "failed"


def test_add_as_leads_needs_linkedin_or_email(client, apify, apollo_env) -> None:  # noqa: F811
    client.post("/api/v1/stakeholders/search-linkedin", json={"companyIds": [str(COMPANY_ID)]}, headers=WS)
    ids = [str(r.id) for r in apollo_env.rows.rows.values()]
    result = client.post("/api/v1/stakeholders/add-as-leads", json={"stakeholderIds": ids}, headers=WS).json()
    assert result == {"added": 1, "linkedExisting": 0, "skipped": 1}  # Bo has no LinkedIn URL and no email
    [lead] = apollo_env.leads
    assert lead.linkedin_url == "https://www.linkedin.com/in/ava-morgan" and lead.email == "ava@glowmedspa.com"
    again = client.post("/api/v1/stakeholders/add-as-leads", json={"stakeholderIds": ids}, headers=WS).json()
    assert again["added"] == 0 and again["skipped"] == 2


def test_people_at_a_pipeline_company_become_leads_with_its_name_site_and_icp(client, apify, apollo_env) -> None:  # noqa: F811
    """Google Maps and Apollo companies aren't in Lead Intel: the lead takes them from the pipeline company,
    so it has an ICP and can get an email written."""
    pipeline_id, icp_id = uuid4(), uuid4()
    apollo_env.pipeline.rows[pipeline_id] = SimpleNamespace(
        id=pipeline_id, name="Chicago Shirt Prints", domain="chicagoshirtprints.com", website_url=None, icp_id=icp_id
    )
    people = [Employee("m1", "Mark", "Cordova", "Owner", "https://www.linkedin.com/in/mark-cordova", None, {})]
    asyncio.run(apollo_env.rows.save_employees(WORKSPACE_ID, None, people, pipeline_company_id=pipeline_id, icp_id=icp_id))
    ids = [str(r.id) for r in apollo_env.rows.rows.values()]
    result = client.post("/api/v1/stakeholders/add-as-leads", json={"stakeholderIds": ids}, headers=WS).json()
    assert result["added"] == 1
    [lead] = apollo_env.leads
    assert (lead.company, lead.website, apollo_env.lead_icps) == ("Chicago Shirt Prints", "https://chicagoshirtprints.com", [icp_id])


def test_unrevealed_apollo_people_cannot_be_added_without_revealing(client, apify, apollo_env) -> None:  # noqa: F811
    client.post("/api/v1/stakeholders/search", json={"companyIds": [str(COMPANY_ID)]}, headers=WS)
    ids = [str(r.id) for r in apollo_env.rows.rows.values()]
    result = client.post("/api/v1/stakeholders/add-as-leads", json={"stakeholderIds": ids}, headers=WS).json()
    assert result["added"] == 0 and result["skipped"] == 2


def test_linkedin_people_are_revealed_in_apollo_by_linkedin_url(client, apify, apollo_env) -> None:  # noqa: F811
    client.post("/api/v1/stakeholders/search-linkedin", json={"companyIds": [str(COMPANY_ID)]}, headers=WS)
    ava = next(r for r in apollo_env.rows.rows.values() if r.external_id == "e1")
    result = client.post("/api/v1/stakeholders/reveal", json={"stakeholderIds": [str(ava.id)]}, headers=WS).json()
    assert result["revealed"] == 1
    assert apollo_env.apollo.revealed_by == "https://www.linkedin.com/in/ava-morgan"


# ---------- Posts: company LinkedIn, their LinkedIn, company Instagram, until there's enough ----------


class FakePosts:
    """Leads, their companies and stored posts. A stored post is a SimpleNamespace like an eo_posts row."""

    def __init__(self, leads, companies) -> None:
        self.rows = {lead.id: lead for lead in leads}
        self.companies = companies  # lead id -> company
        self.posts: list = []

    async def leads(self, ws, ids):
        return [self.rows[i] for i in ids if i in self.rows]

    async def company_for(self, ws, lead):
        return self.companies.get(lead.id)

    def _owner(self, owner):
        kind, _, raw = owner.partition(":")
        return ("lead_id" if kind == "lead" else "company_id"), raw

    async def stored(self, ws, lead_id, company_id, limit=20):
        return [x for x in self.posts if x.lead_id == lead_id or (company_id and x.company_id == company_id)]

    async def count_source(self, ws, owner, network):
        col, raw = self._owner(owner)
        return sum(1 for x in self.posts if str(getattr(x, col)) == raw and x.source == network)

    async def save_for(self, ws, owner, found):
        col, raw = self._owner(owner)
        known = {x.url for x in self.posts}
        new = [f for f in found if f.url not in known]
        for f in new:
            self.posts.append(SimpleNamespace(
                lead_id=next((i for i in self.rows if str(i) == raw), None) if col == "lead_id" else None,
                company_id=next((c.id for c in self.companies.values() if str(c.id) == raw), None) if col == "company_id" else None,
                source=f.source, url=f.url, text=f.text, posted_at=f.posted_at, is_repost=f.is_repost,
            ))  # fmt: skip
        return len(new)

    async def set_status(self, lead, status):
        lead.status = status

    async def list(self, ws, lead_id):
        return []


class FakeJudge:
    """Claude: enough once a post mentions a launch. Records how many times it was asked."""

    def __init__(self) -> None:
        self.asked = 0
        self.messages = self

    async def parse(self, *, model, max_tokens, system, output_format, messages):
        from services.smart_posts import Enough

        self.asked += 1
        enough = "launch" in messages[0]["content"].lower()
        return SimpleNamespace(parsed_output=Enough(enough=enough, reason="a launch" if enough else "only promos"),
                               usage=SimpleNamespace(input_tokens=500, output_tokens=20))  # fmt: skip


def lead(**kw):
    defaults = dict(id=uuid4(), first_name="Ava", last_name="Morgan", title="Owner", company="", linkedin_url=None,
                    instagram_url=None, lead_intel_business_id=None, status="imported")  # fmt: skip
    return SimpleNamespace(**(defaults | kw))


def company(name, linkedin=None, instagram=None):
    return SimpleNamespace(id=uuid4(), name=name, linkedin_url=linkedin, instagram_url=instagram, research=None)


@pytest.fixture
def posts_env(client, apify, apollo_env):  # noqa: F811
    from dependencies import get_optional_anthropic

    ink = company("Ink Co", "https://www.linkedin.com/company/inkco/", "https://instagram.com/inkco")
    tee = company("Tee Shop", "https://www.linkedin.com/company/teeshop/", "https://instagram.com/teeshop")
    ava = lead(linkedin_url="https://www.linkedin.com/in/ava-morgan")
    bo = lead(first_name="Bo", linkedin_url="https://linkedin.com/in/bo-chen/")
    nobody = lead(first_name="No", status="approved")
    repo = FakePosts([ava, bo, nobody], {ava.id: ink, bo.id: tee})
    judge = FakeJudge()
    app.dependency_overrides[get_post_repo] = lambda: repo
    app.dependency_overrides[get_optional_anthropic] = lambda: judge
    return SimpleNamespace(repo=repo, ava=ava, bo=bo, nobody=nobody, ink=ink, tee=tee, apify=apify, judge=judge)


def fetch(client, *leads, **body):
    return client.post("/api/v1/posts/fetch", json={"leadIds": [str(x.id) for x in leads], **body}, headers=WS).json()


def test_company_linkedin_first_and_only_those_still_short_go_on(client, posts_env) -> None:
    """Ink Co's page has a launch: Ava is done. Tee Shop's page is only promos: Bo's own LinkedIn is read, and has one."""
    e = posts_env
    e.apify.linkedin_items = [post("linkedin", "https://li/1", "inkco"), post("linkedin", "https://li/2", "teeshop")]
    e.apify.linkedin_items[0].text = "We launch our spring collection Friday"
    e.apify.linkedin_items[1].text = "Sale ends Sunday"
    calls = []
    original = e.apify.linkedin_posts

    async def linkedin_posts(urls, max_posts, since):
        calls.append(tuple(urls))
        if len(calls) == 2:
            e.apify.linkedin_items = [post("linkedin", "https://li/3", "bo-chen")]
            e.apify.linkedin_items[0].text = "Big launch: we opened a second shop"
        return await original(urls, max_posts, since)

    e.apify.linkedin_posts = linkedin_posts
    result = fetch(client, e.ava, e.bo, e.nobody, maxPosts=3)
    by = {r["leadId"]: r for r in result["results"]}
    # Stage 1 reads both company pages in one run; stage 2 only Bo's profile; Instagram is never needed.
    assert calls == [("https://www.linkedin.com/company/inkco/", "https://www.linkedin.com/company/teeshop/"), ("https://linkedin.com/in/bo-chen/",)]
    assert not any(c[0] == "instagram" for c in e.apify.calls)
    assert (by[str(e.ava.id)]["enough"], by[str(e.ava.id)]["stoppedAt"]) == (True, "company_linkedin")
    assert (by[str(e.bo.id)]["enough"], by[str(e.bo.id)]["stoppedAt"], by[str(e.bo.id)]["linkedin"]) == (True, "person_linkedin", 1)
    assert by[str(e.nobody.id)]["companyLinkedin"] is None and e.nobody.status == "approved"
    assert e.ava.status == "enriched" and result["claudeCostUsd"] > 0 and result["error"] is None


def test_stored_posts_are_read_and_not_bought_again(client, posts_env) -> None:
    e = posts_env
    e.apify.linkedin_items = [post("linkedin", "https://li/1", "inkco")]
    e.apify.linkedin_items[0].text = "Happy holidays"
    # Research already stored Ink Co's Instagram posts: they're read, not scraped again.
    e.repo.posts.append(SimpleNamespace(lead_id=None, company_id=e.ink.id, source="instagram", url="https://ig/0",
                                        text="New launch drop today", posted_at=None, is_repost=False))  # fmt: skip
    result = fetch(client, e.ava)
    r = result["results"][0]
    # Claude reads every stored post at each check, so the stored Instagram post already gives enough.
    assert (r["enough"], r["stoppedAt"]) == (True, "company_linkedin")
    assert not any(c[0] == "instagram" for c in e.apify.calls)


def test_company_instagram_is_scraped_when_nothing_else_is_enough(client, posts_env) -> None:
    e = posts_env
    e.apify.linkedin_items = [post("linkedin", "https://li/1", "inkco")]
    e.apify.linkedin_items[0].text = "Happy holidays"
    e.apify.instagram_items = [post("instagram", "https://ig/1", "inkco")]
    e.apify.instagram_items[0].text = "Launch day for our new tees"
    r = fetch(client, e.ava)["results"][0]
    assert (r["enough"], r["stoppedAt"], r["instagram"]) == (True, "company_instagram", 1)
    assert [c[1] for c in e.apify.calls if c[0] == "instagram"] == [("https://instagram.com/inkco",)]


def test_nothing_anywhere_marks_no_content(client, posts_env) -> None:
    result = fetch(client, posts_env.ava)
    assert result["results"][0]["enough"] is False and posts_env.ava.status == "no_content"


def test_apify_failure_is_reported_and_statuses_untouched(client, posts_env) -> None:
    e = posts_env
    e.apify.linkedin_items = [post("linkedin", "https://li/1", "inkco")]
    e.apify.linkedin_items[0].text = "promo"
    e.apify.error = ApifyError("Apify says the account is out of credit", 402)
    e.bo.linkedin_url = None
    e.repo.companies.pop(e.bo.id)
    result = fetch(client, e.ava)
    assert result["error"] == "Apify says the account is out of credit" and e.ava.status == "imported"


def test_fetch_limits(client, posts_env) -> None:
    too_many = {"leadIds": [str(uuid4()) for _ in range(26)]}
    assert client.post("/api/v1/posts/fetch", json=too_many, headers=WS).status_code == 422
    assert client.post("/api/v1/posts/fetch", json={"leadIds": [str(uuid4())], "maxPosts": 21}, headers=WS).status_code == 422


def test_workspace_header_required(client, posts_env) -> None:
    assert client.post("/api/v1/posts/fetch", json={"leadIds": [str(uuid4())]}).status_code == 400
    assert WORKSPACE_ID


def test_people_found_with_an_email_become_leads_ready_to_send(client, apify, apollo_env) -> None:  # noqa: F811
    client.post("/api/v1/stakeholders/search-linkedin", json={"companyIds": [str(COMPANY_ID)]}, headers=WS)
    ava = next(r for r in apollo_env.rows.rows.values() if r.external_id == "e1")
    assert (ava.email, ava.email_status) == ("ava@glowmedspa.com", "valid")
    client.post("/api/v1/stakeholders/add-as-leads", json={"stakeholderIds": [str(ava.id)]}, headers=WS)
    [lead] = apollo_env.leads
    assert lead.email == "ava@glowmedspa.com"


def test_emails_can_be_left_out_to_pay_less(client, apify, apollo_env) -> None:  # noqa: F811
    client.post("/api/v1/stakeholders/search-linkedin", json={"companyIds": [str(COMPANY_ID)], "findEmails": False}, headers=WS)
    assert apify.calls[-1][-1] is False
