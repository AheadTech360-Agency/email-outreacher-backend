"""Pipeline endpoints with fake repos and providers. No network, no cost."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dependencies import (
    get_company_repo,
    get_optional_apify,
    get_optional_apollo,
    get_optional_scorer,
    get_provider_call_repo,
    get_stakeholder_repo,
    get_website_reader,
)
from main import app
from services.apollo import ApolloError, ApolloOrganization, FoundPerson
from tests.conftest import COMPANY_ID, ICP_ID, WORKSPACE_ID
from tests.test_stakeholders_api import FakeCalls, FakeStakeholders

WS = {"X-Workspace-Id": str(WORKSPACE_ID)}


class FakeCompanies:
    """Same dedupe rules as CompanyRepo, in memory."""

    def __init__(self) -> None:
        self.rows: dict = {}

    async def workspace_exists(self, ws):
        return ws == WORKSPACE_ID

    def _row(self, **kw):
        base = dict(id=uuid4(), workspace_id=WORKSPACE_ID, icp_id=ICP_ID, lead_intel_business_id=None, apollo_org_id=None,
                    google_place_id=None, phone=None, domain=None, website_url=None, linkedin_url=None, facebook_url=None, twitter_url=None,
                    instagram_url=None, city=None, state=None, status_reason=None, overridden=False, rule_fit=None,
                    rule_detail=None, score=None, score_detail=None, research=None, researched_at=None, raw=None,
                    created_at=datetime.now(UTC))  # fmt: skip
        row = SimpleNamespace(**(base | kw))
        self.rows[row.id] = row
        return row

    async def add_lead_intel(self, ws, icp_id, companies):
        added = upgraded = had = 0
        for c in companies:
            if any(r.lead_intel_business_id == c["id"] for r in self.rows.values()):
                had += 1
            elif dup := next((r for r in self.rows.values() if c.get("domain") and r.domain == c["domain"]), None):
                dup.source, dup.lead_intel_business_id, dup.status = "lead_intel", c["id"], "qualified"
                upgraded += 1
            else:
                self._row(source="lead_intel", lead_intel_business_id=c["id"], name=c["name"], domain=c["domain"], status="qualified")
                added += 1
        return added, upgraded, had

    async def add_apollo(self, ws, icp_id, orgs):
        added = dupes = 0
        for o in orgs:
            if any(r.apollo_org_id == o.apollo_id or (o.domain and r.domain == o.domain) for r in self.rows.values()):
                dupes += 1
            else:
                self._row(source="apollo", apollo_org_id=o.apollo_id, name=o.name, domain=o.domain, status="found")
                added += 1
        return added, dupes

    async def add_google_maps(self, ws, icp_id, places, added_ids=None):
        added = dupes = 0
        for place, domain in places:
            if any(r.google_place_id == place.place_id or (domain and r.domain == domain) for r in self.rows.values()):
                dupes += 1
            else:
                row = self._row(source="google_maps", google_place_id=place.place_id, name=place.name, domain=domain,
                                website_url=place.website_url, phone=place.phone, status="found")  # fmt: skip
                if added_ids is not None:
                    added_ids.append(row.id)
                added += 1
        return added, dupes

    async def list(self, ws, icp_id, status, light=False):
        return [r for r in self.rows.values() if status in (None, r.status)]

    async def get(self, ws, cid):
        return self.rows.get(cid)

    async def get_many(self, ws, ids):
        return [self.rows[i] for i in ids if i in self.rows]

    async def save(self, company):
        pass


class FakeApollo:
    def __init__(self, error: ApolloError | None = None) -> None:
        self.error = error
        self.asked: dict = {}

    async def search_organizations(self, **kw):
        if self.error:
            raise self.error
        self.asked = kw
        return [
            ApolloOrganization("o1", "Glow Med Spa", "GlowMedSpa.com", None, None, None, None, {}),  # same as Lead Intel
            ApolloOrganization("o2", "Ink Co", None, "https://www.inkco.com", None, None, None, {}),
        ], 57

    async def search_people(self, domain, titles, seniorities, per_page=25, **kw):
        self.people_domain = domain
        return [FoundPerson("p1", "Ava", "Mo***n", "Owner", True, None, {})], 3


@pytest.fixture
def env(client):
    repo, calls, people = FakeCompanies(), FakeCalls(), FakeStakeholders()
    apollo = FakeApollo()
    app.dependency_overrides |= {
        get_company_repo: lambda: repo,
        get_provider_call_repo: lambda: calls,
        get_stakeholder_repo: lambda: people,
        get_optional_apollo: lambda: apollo,
        get_optional_apify: lambda: None,
        get_optional_scorer: lambda: None,
        get_website_reader: lambda: None,
    }
    return SimpleNamespace(repo=repo, calls=calls, people=people, apollo=apollo)


def find(client, **body):
    return client.post("/api/v1/pipeline/find", json={"icpId": str(ICP_ID), **body}, headers=WS)


def test_find_from_both_sources_dedupes_by_domain(client, env) -> None:
    result = find(client).json()
    assert result["leadIntel"] == {
        "found": 1, "added": 1, "duplicates": 0, "upgraded": 0, "totalAvailable": 1, "error": None,
        "searches": None, "costUsd": None, "notes": [], "excluded": 0, "companyIds": [],
    }  # fmt: skip
    # Apollo's Glow Med Spa has the same domain as the Lead Intel company: a duplicate, not a second row.
    assert (result["apollo"]["found"], result["apollo"]["added"], result["apollo"]["duplicates"]) == (2, 1, 1)
    assert result["apollo"]["totalAvailable"] == 57
    # Defaults come from the ICP: its search terms and places.
    assert env.apollo.asked["keywords"] == ["med spa"] and env.apollo.asked["locations"] == ["Austin, TX"]
    rows = client.get("/api/v1/pipeline/companies", headers=WS).json()
    assert {(r["name"], r["source"], r["status"], r["domain"]) for r in rows} == {
        ("Glow Med Spa", "lead_intel", "qualified", "glowmedspa.com"),
        ("Ink Co", "apollo", "found", "inkco.com"),
    }
    again = find(client).json()
    assert again["leadIntel"]["added"] == 0 and again["apollo"]["added"] == 0


def test_apollo_found_first_becomes_the_lead_intel_company(client, env) -> None:
    find(client, sources=["apollo"])
    result = find(client, sources=["lead_intel"]).json()["leadIntel"]
    assert result["upgraded"] == 1 and result["added"] == 0
    glow = next(r for r in env.repo.rows.values() if r.domain == "glowmedspa.com")
    assert glow.source == "lead_intel" and glow.lead_intel_business_id == COMPANY_ID and glow.status == "qualified"


def test_apollo_refusal_is_reported_and_lead_intel_still_works(client, env) -> None:
    env.apollo.error = ApolloError("not included in your Free plan", 403)
    result = find(client).json()
    assert result["apollo"]["error"] == "not included in your Free plan" and result["leadIntel"]["added"] == 1


def test_find_needs_a_real_icp_and_workspace(client, env) -> None:
    assert client.post("/api/v1/pipeline/find", json={"icpId": str(uuid4())}, headers=WS).status_code == 404
    other = {"X-Workspace-Id": str(uuid4())}
    assert client.post("/api/v1/pipeline/find", json={"icpId": str(ICP_ID)}, headers=other).status_code == 404


def test_override_sticks(client, env) -> None:
    find(client)
    ink = next(r for r in env.repo.rows.values() if r.name == "Ink Co")
    out = client.patch(f"/api/v1/pipeline/companies/{ink.id}", json={"status": "qualified"}, headers=WS).json()
    assert out["status"] == "qualified" and out["overridden"] and out["statusReason"].startswith("Approved")


def test_stakeholders_only_for_qualified_companies(client, env) -> None:
    find(client)
    ids = [str(r.id) for r in env.repo.rows.values()]
    result = client.post("/api/v1/pipeline/stakeholders", json={"companyIds": ids}, headers=WS).json()
    by = {r["name"]: r for r in result["results"]}
    assert by["Glow Med Spa"]["status"] == "searched" and by["Glow Med Spa"]["found"] == 1
    assert by["Ink Co"]["status"] == "skipped" and by["Ink Co"]["reason"] == "Not qualified yet"
    assert env.apollo.people_domain == "glowmedspa.com"
    [person] = env.people.rows.values()
    assert person.organization_name == "Glow Med Spa"


def test_research_skips_lead_intel_companies(client, env) -> None:
    find(client, sources=["lead_intel"])
    ids = [str(r.id) for r in env.repo.rows.values()]
    result = client.post("/api/v1/pipeline/research", json={"companyIds": ids}, headers=WS).json()
    assert result["researched"] == 0 and result["skipped"] == 1


def test_lead_intel_import_takes_only_the_picked_companies(client, env) -> None:
    none = find(client, sources=["lead_intel"], leadIntelIds=[str(uuid4())]).json()["leadIntel"]
    assert (none["found"], none["added"]) == (0, 0)
    picked = find(client, sources=["lead_intel"], leadIntelIds=[str(COMPANY_ID)]).json()["leadIntel"]
    assert (picked["found"], picked["added"]) == (1, 1)
