"""The Prospector role: only the Quick Start searches an admin allows them (LinkedIn by default), only the runs they
start, and no more than the workspace's cap per run. Inside their own run (the worker) only the search, verify, people,
posts and analysis steps get through: never email writing."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from background import JobContext, current_job
from dependencies import (
    PROSPECTOR_PAGES, PROSPECTOR_STEPS, PROSPECTORS_ONLY, get_current_user, get_icp_profile_repo, get_lead_intel_repo, get_product_repo,
    get_quickstart_queue, get_quickstart_repo, get_settings_repo, limit_prospectors,
)  # fmt: skip
from main import app
from routers import quickstart as qs_router
from services.auth import CurrentUser
from tests.conftest import WORKSPACE_ID

H = {"X-Workspace-Id": str(WORKSPACE_ID)}


def as_role(role: str) -> CurrentUser:
    user = CurrentUser(uuid4(), f"{role}@example.com", role.title(), role, [WORKSPACE_ID])
    app.dependency_overrides[get_current_user] = lambda: user
    return user


def _protected_routes() -> list[tuple[str, str]]:
    """Every endpoint under /api/v1 that needs a sign-in, from the app's OpenAPI schema (FastAPI includes routers
    lazily, so app.routes doesn't list them)."""
    out = []
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api/v1"):
            continue
        path = path.removeprefix("/api/v1")
        if path.startswith(("/health", "/webhooks")):
            continue
        out += [(m.upper(), path) for m in operations if m.upper() not in ("HEAD", "OPTIONS")]
    return sorted(set(out))


ROUTES = _protected_routes()


def _url(path: str) -> str:
    """A route's template with an id in every placeholder."""
    import re

    return "/api/v1" + re.sub(r"\{[^}]+\}", str(uuid4()), path)


@pytest.mark.parametrize(("method", "path"), [r for r in ROUTES if r not in PROSPECTOR_PAGES])
def test_a_prospector_is_refused_everything_but_quick_starts_linkedin_option(client, method, path) -> None:
    as_role("prospector")
    r = client.request(method, _url(path), headers=H, json={})
    assert r.status_code == 403 and r.json()["detail"] == PROSPECTORS_ONLY, (method, path, r.status_code)


def test_the_allowed_pages_and_steps_exist() -> None:
    # A typo here would quietly lock a Prospector out of a page or a step.
    assert PROSPECTOR_PAGES <= set(ROUTES)
    assert {("POST", p) for p in PROSPECTOR_STEPS} <= set(ROUTES)


def _gate(method: str, path: str, user: CurrentUser, in_job: bool):
    import asyncio

    request = SimpleNamespace(method=method, scope={"route": SimpleNamespace(path=f"/api/v1{path}")}, url=SimpleNamespace(path=f"/api/v1{path}"))
    token = current_job.set(JobContext(uuid4(), user)) if in_job else None
    try:
        return asyncio.run(limit_prospectors(request, user))
    finally:
        if token is not None:
            current_job.reset(token)


def test_inside_their_run_only_the_linkedin_steps_get_through() -> None:
    from fastapi import HTTPException

    user = CurrentUser(uuid4(), "p@example.com", "P", "prospector", [WORKSPACE_ID])
    for path in PROSPECTOR_STEPS:
        assert _gate("POST", path, user, in_job=True) is None
    for path in ("/emails/generate", "/emails/test-prompt", "/sending/queue", "/leads/delete", "/companies/import"):
        with pytest.raises(HTTPException) as refused:
            _gate("POST", path, user, in_job=True)
        assert refused.value.status_code == 403
    # The steps can't be called from the browser.
    with pytest.raises(HTTPException):
        _gate("POST", "/pipeline/find-linkedin", user, in_job=False)
    # Everyone else is untouched.
    assert _gate("POST", "/emails/generate", CurrentUser(uuid4(), "m@example.com", "M", "management", [WORKSPACE_ID]), in_job=False) is None


# ---------- Quick Start, as a Prospector ----------


def _run(created_by: UUID, source: str = "linkedin") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), workspace_id=WORKSPACE_ID, icp_id=uuid4(), created_by=created_by, status="done", step="analysis",
        settings={"source": source}, progress={}, spent_usd=0, spend_limit_usd=5, detail=None,
        created_at=datetime.now(UTC), finished_at=None, company_ids=[], lead_ids=[], job_ids=[], busy_until=None,
    )  # fmt: skip


class FakeRuns:
    def __init__(self, rows):
        self.rows = {r.id: r for r in rows}
        self.created = []

    async def get(self, workspace_id, run_id):
        return self.rows.get(run_id)

    async def recent(self, workspace_id, limit=20, started_by=None):
        rows = list(self.rows.values())
        if started_by is not None:
            rows = [r for r in rows if r.created_by == started_by]
        return rows

    async def create(self, workspace_id, user_id, icp_id, settings, limit):
        run = _run(user_id)
        run.settings, run.spend_limit_usd, run.status = settings, limit, "queued"
        self.created.append(run)
        return run

    async def briefs(self, company_ids, workspace_id=None):
        return []

    async def save(self, run):
        pass


class FakeLeadIntel:
    async def get_icp(self, icp_id):
        return {"name": "Apparel"}


class FakeSettings:
    async def general(self, workspace_id):
        return {"prospector_run_limit_usd": 3.0}


START_ICP = uuid4()


@pytest.fixture
def world(client, monkeypatch, access):
    me = as_role("prospector")
    access.items.append(SimpleNamespace(user_id=me.id, workspace_id=WORKSPACE_ID, kind="icp", resource_id=START_ICP, can_edit=False,
                                        is_owner=False, granted_by=None))  # fmt: skip
    mine, theirs, my_maps = _run(me.id), _run(uuid4()), _run(me.id, "google_maps")
    runs = FakeRuns([mine, theirs, my_maps])
    app.dependency_overrides[get_quickstart_repo] = lambda: runs
    app.dependency_overrides[get_lead_intel_repo] = lambda: FakeLeadIntel()
    app.dependency_overrides[get_settings_repo] = lambda: FakeSettings()
    app.dependency_overrides[get_icp_profile_repo] = lambda: SimpleNamespace(get_many=_none_profiles)
    app.dependency_overrides[get_product_repo] = lambda: SimpleNamespace()
    app.dependency_overrides[get_quickstart_queue] = lambda: (lambda run_id: None)
    monkeypatch.setattr(qs_router, "has_offer", lambda profile, product: True)
    return SimpleNamespace(client=client, me=me, mine=mine, theirs=theirs, my_maps=my_maps, runs=runs)


async def _none_profiles(workspace_id, ids):
    return {}


def test_a_prospector_sees_only_their_own_runs(world) -> None:
    c = world.client
    listed = [r["id"] for r in c.get("/api/v1/quickstart/runs", headers=H).json()]
    assert listed == [str(world.mine.id), str(world.my_maps.id)]
    assert c.get(f"/api/v1/quickstart/runs/{world.theirs.id}/briefs", headers=H).status_code == 404
    assert c.get(f"/api/v1/quickstart/runs/{world.my_maps.id}/briefs", headers=H).status_code == 200
    assert c.get(f"/api/v1/quickstart/runs/{world.mine.id}/briefs", headers=H).status_code == 200
    assert c.post(f"/api/v1/quickstart/runs/{world.theirs.id}/stop", headers=H).status_code == 404


def _start(c, **kw):
    body = {"icpId": str(START_ICP), "source": "linkedin", "searchTerms": ["custom apparel"], "places": [], "radiusMiles": 10,
            "googleRequests": 2, "companies": 5, "peoplePerCompany": 2, "titles": [], "findEmails": False, "leads": 5,
            "onlyWithEmail": False, "postsPerLead": 3, "maxPostAgeDays": 60, "spendLimitUsd": 2.5} | kw  # fmt: skip
    return c.post("/api/v1/quickstart/runs", headers=H, json=body)


def test_a_prospector_starts_linkedin_runs_within_the_cap_and_free_search_only(world) -> None:
    c = world.client
    assert _start(c, source="google_maps", places=["Austin"]).status_code == 403
    assert _start(c, webSearch="google").status_code == 403
    over = _start(c, spendLimitUsd=3.5)
    assert over.status_code == 422 and "$3.00" in over.json()["detail"]
    ok = _start(c)
    assert ok.status_code == 201, ok.text
    assert world.runs.created[0].settings["source"] == "linkedin" and world.runs.created[0].settings["web_search"] == "brave"


def test_an_admin_decides_which_searches_posts_and_analysis_a_prospector_gets(world) -> None:
    c = world.client
    # Google search allowed, LinkedIn not: only Google gets through.
    world.me.permissions = {"search": ["google_maps"], "posts": [], "analysis_reads": ["website"], "analysis_writes": ["company"]}
    assert _start(c).status_code == 403
    ok = _start(c, source="google_maps", places=["Austin"], googleRequests=2, companies=3, peoplePerCompany=1, leads=3)
    assert ok.status_code == 201, ok.text
    s = world.runs.created[-1].settings
    assert s["source"] == "google_maps" and s["prospector"] is True
    assert s["post_sources"] == [] and s["analysis_reads"] == ["website"] and s["analysis_writes"] == ["company"]
    # A Google run is held to the cap too: the most it could spend, Google's requests included.
    assert _start(c, source="google_maps", places=["Austin"], googleRequests=80, companies=25).status_code == 422
    # Both searches.
    world.me.permissions = {"search": ["google_maps", "linkedin"]}
    assert _start(c).status_code == 201
    assert world.runs.created[-1].settings["post_sources"] == ["linkedin", "instagram"]


def test_a_prospectors_stored_permissions_default_to_what_they_could_always_do() -> None:
    from schemas import permissions_of

    p = permissions_of({})
    assert p.search == ["linkedin"] and p.posts == ["linkedin", "instagram"]
    assert p.analysis_reads == ["website", "posts", "company_profile", "people_profiles"] and p.analysis_writes == ["company", "people"]
    assert permissions_of({"search": ["telepathy"]}) == p  # unreadable: the defaults
    assert permissions_of(None) == p and permissions_of({"posts": []}).posts == []


def test_a_prospector_starts_runs_only_for_an_icp_they_can_see(world) -> None:
    assert _start(world.client, icpId=str(uuid4())).status_code == 404


def test_management_isnt_held_to_the_prospector_cap(world) -> None:
    assert world.client.get("/api/v1/quickstart/limits", headers=H).json() == {"spendLimitUsd": 3.0}
    as_role("management")
    assert world.client.get("/api/v1/quickstart/limits", headers=H).json() == {"spendLimitUsd": None}
    assert _start(world.client, spendLimitUsd=50, webSearch="google").status_code == 201
    assert world.runs.created[-1].settings["prospector"] is False



# ---------- Approving their own run's companies, and its people ----------


def test_a_finished_run_goes_on_once_companies_it_left_waiting_are_approved() -> None:
    import asyncio

    from routers.quickstart import _approved_since

    done, waiting = uuid4(), uuid4()
    run = SimpleNamespace(company_ids=[done, waiting], progress={"_searched": [str(done)]})

    class Runs:
        def __init__(self, statuses):
            self.statuses = statuses

        async def companies(self, ids, brief=False):
            return [SimpleNamespace(id=i, status=self.statuses[i]) for i in ids]

    assert asyncio.run(_approved_since(Runs({done: "qualified", waiting: "review"}), run)) is False
    assert asyncio.run(_approved_since(Runs({done: "qualified", waiting: "qualified"}), run)) is True


def test_a_prospector_decides_only_companies_they_can_see(client, monkeypatch) -> None:
    from dependencies import get_company_repo
    from services.access import Scope

    as_role("prospector")
    mine, other = uuid4(), uuid4()
    decided = []

    async def visible(self, session, kind, ids):
        return [i for i in ids if i == mine]

    monkeypatch.setattr(Scope, "visible_members", visible)

    class Companies:
        session = None

        async def get_many(self, workspace_id, ids):
            decided.extend(ids)
            return []

        async def save_all(self, rows):
            pass

    app.dependency_overrides[get_company_repo] = lambda: Companies()
    body = {"companyIds": [str(other)], "status": "qualified"}
    assert client.post("/api/v1/pipeline/companies/decide", headers=H, json=body).status_code == 404
    body = {"companyIds": [str(mine), str(other)], "status": "qualified"}
    assert client.post("/api/v1/pipeline/companies/decide", headers=H, json=body).status_code == 200
    assert decided == [mine]


def test_the_analysis_reads_only_what_a_prospector_is_allowed() -> None:
    from routers.pipeline import _readable

    evidence = {
        "company": {"name": "Ink Co"}, "verdict": {}, "icp_and_product": {},
        "website": {"summary": "s"}, "website_pages": [{"url": "u"}], "contacts": {"emails": []},
        "company_listing": {"followers": 10}, "instagram": {"bio": "b"},
        "posts": [{"text": "t"}],
        "people": [{"name": "Ann", "title": "Owner", "headline": "h", "about": "a"}],
    }  # fmt: skip
    assert _readable(evidence, None) == evidence
    only_site = _readable(evidence, ["website"])
    assert {"website", "website_pages", "contacts"} <= set(only_site)
    assert not {"posts", "company_listing", "instagram"} & set(only_site)
    assert only_site["people"] == [{"name": "Ann", "title": "Owner"}]  # who they are, not their profiles
    nothing = _readable(evidence, [])
    assert set(nothing) == {"company", "verdict", "icp_and_product", "people"}
    assert _readable(evidence, ["people_profiles"])["people"] == evidence["people"]


def test_the_analysis_writes_only_the_parts_allowed(monkeypatch) -> None:
    import asyncio

    from services import briefs

    asked = []

    class Messages:
        async def parse(self, **kw):
            asked.append(kw["messages"][0]["content"])
            raise RuntimeError("stop here")

    writer = briefs.BriefWriter.__new__(briefs.BriefWriter)
    writer._client, writer._gate, writer._model = SimpleNamespace(messages=Messages()), asyncio.Semaphore(1), "m"
    for parts in (("company", "people"), ("company",), ("people",)):
        asyncio.run(writer.write({"company": {"name": "Ink Co"}}, parts))
    assert "Write only" not in asked[0]
    assert "leave people empty" in asked[1]
    assert "Write only the people part" in asked[2]


def test_permissions_are_kept_once_each_and_need_a_search_and_something_to_write() -> None:
    from pydantic import ValidationError

    from schemas import ProspectorPermissions

    assert ProspectorPermissions(search=["linkedin", "linkedin"]).search == ["linkedin"]
    for bad in ({"search": []}, {"analysis_writes": []}, {"posts": ["tiktok"]}):
        with pytest.raises(ValidationError):
            ProspectorPermissions(**bad)
