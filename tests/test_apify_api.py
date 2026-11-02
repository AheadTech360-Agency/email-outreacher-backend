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


# ---------- Posts ----------


class FakePosts:
    def __init__(self, leads) -> None:
        self.rows = {lead.id: lead for lead in leads}
        self.saved: dict = {}

    async def leads(self, ws, ids):
        return [self.rows[i] for i in ids if i in self.rows]

    async def save(self, ws, lead_id, posts):
        have = self.saved.setdefault(lead_id, {})
        new = [p for p in posts if p.url not in have]
        for p in new:
            have[p.url] = p
        return len(new)

    async def set_status(self, lead, status):
        lead.status = status

    async def list(self, ws, lead_id):
        return []


def lead(**kw):
    defaults = dict(id=uuid4(), linkedin_url=None, instagram_url=None, lead_intel_business_id=None, status="imported")
    return SimpleNamespace(**(defaults | kw))


@pytest.fixture
def posts_env(client, apify, apollo_env):  # noqa: F811
    ava = lead(linkedin_url="https://www.linkedin.com/in/ava-morgan")
    bo = lead(linkedin_url="https://linkedin.com/in/bo-chen/", lead_intel_business_id=COMPANY_ID)
    cy = lead(instagram_url="https://instagram.com/cyshop")
    nobody = lead(status="approved")
    repo = FakePosts([ava, bo, cy, nobody])
    app.dependency_overrides[get_post_repo] = lambda: repo
    return SimpleNamespace(repo=repo, ava=ava, bo=bo, cy=cy, nobody=nobody, apify=apify)


def fetch(client, *leads, **body):
    return client.post("/api/v1/posts/fetch", json={"leadIds": [str(x.id) for x in leads], **body}, headers=WS).json()


def test_fetch_posts_one_run_per_source_matched_back_to_each_lead(client, posts_env) -> None:
    e = posts_env
    e.apify.linkedin_items = [post("linkedin", "https://li/1", "ava-morgan"), post("linkedin", "https://li/2", "bo-chen")]
    e.apify.instagram_items = [post("instagram", "https://ig/1", "glowmedspa"), post("instagram", "https://ig/2", "cyshop")]
    result = fetch(client, e.ava, e.bo, e.cy, e.nobody, maxPosts=3)
    by = {r["leadId"]: r for r in result["results"]}

    assert [c[0] for c in e.apify.calls] == ["linkedin", "instagram"]
    # Bo has no Instagram of his own, so his company's account (from Lead Intel) is used.
    assert set(e.apify.calls[1][1]) == {"https://www.instagram.com/glowmedspa/", "https://www.instagram.com/cyshop/"}
    assert (by[str(e.ava.id)]["linkedin"], by[str(e.ava.id)]["instagram"]) == (1, None)
    assert (by[str(e.bo.id)]["linkedin"], by[str(e.bo.id)]["instagram"]) == (1, 1)
    assert (by[str(e.cy.id)]["linkedin"], by[str(e.cy.id)]["instagram"]) == (None, 1)
    assert (by[str(e.nobody.id)]["linkedin"], by[str(e.nobody.id)]["instagram"]) == (None, None)
    assert result["costUsd"] == 0.007 and result["error"] is None
    assert (e.ava.status, e.cy.status, e.nobody.status) == ("enriched", "enriched", "approved")


def test_refetching_stores_only_new_posts(client, posts_env) -> None:
    e = posts_env
    e.apify.linkedin_items = [post("linkedin", "https://li/1", "ava-morgan")]
    assert fetch(client, e.ava)["results"][0]["new"] == 1
    assert fetch(client, e.ava)["results"][0]["new"] == 0


def test_a_single_profile_gets_all_its_posts_even_if_authors_differ(client, posts_env) -> None:
    e = posts_env
    e.apify.linkedin_items = [post("linkedin", "https://li/9", "someone-else-reposted")]
    assert fetch(client, e.ava)["results"][0]["linkedin"] == 1


def test_no_posts_marks_no_content(client, posts_env) -> None:
    fetch(client, posts_env.ava)
    assert posts_env.ava.status == "no_content"


def test_apify_failure_is_reported_and_statuses_untouched(client, posts_env) -> None:
    e = posts_env
    e.apify.error = ApifyError("Apify says the account is out of credit", 402)
    result = fetch(client, e.cy)
    assert result["error"] == "Apify says the account is out of credit"
    assert e.cy.status == "imported"


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
