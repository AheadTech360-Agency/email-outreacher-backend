"""Background jobs: long-running endpoints only run in the worker, started with POST /jobs (background.py). No Redis, no worker,
no paid calls: the queue and the job table are faked, and the worker's in-process call runs on its own."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

import background
from dependencies import get_job_queue, get_job_repo, refuse_inline, require_api_secret
from main import app
from tests.conftest import ADMIN, WORKSPACE_ID

H = {"X-Workspace-Id": str(WORKSPACE_ID)}


class FakeJobs:
    def __init__(self) -> None:
        self.rows: dict = {}

    async def create(self, workspace_id, user_id, kind, label, params):
        job = SimpleNamespace(id=uuid4(), workspace_id=workspace_id, created_by=user_id, kind=kind, label=label, params=params,
                              status="queued", step=None, detail=None, result=None, created_at=datetime.now(UTC),
                              started_at=None, finished_at=None)  # fmt: skip
        self.rows[job.id] = job
        return job

    async def recent(self, workspace_id, user_id, limit=50):
        return [row for row in self.rows.values() if row.workspace_id == workspace_id and (user_id is None or row.created_by == user_id)][:limit]

    async def get(self, job_id):
        return self.rows.get(job_id)

    async def finish(self, job_id, status, result, detail):
        job = self.rows[job_id]
        job.status, job.result, job.detail, job.finished_at = status, result, detail, datetime.now(UTC)


@pytest.fixture
def jobs(client):
    fake, queued = FakeJobs(), []
    app.dependency_overrides[get_job_repo] = lambda: fake
    app.dependency_overrides[get_job_queue] = lambda: queued.append
    return SimpleNamespace(client=client, rows=fake.rows, queued=queued)


def test_long_running_endpoints_refuse_to_run_inside_a_request(client) -> None:
    app.dependency_overrides.pop(refuse_inline)
    r = client.post("/api/v1/pipeline/research", headers=H, json={"companyIds": []})
    assert r.status_code == 409 and "POST /api/v1/jobs" in r.json()["detail"]
    assert client.get("/api/v1/sources").status_code == 200  # everything else is untouched


def test_a_job_is_recorded_and_handed_to_the_worker(jobs) -> None:
    r = jobs.client.post("/api/v1/jobs", headers=H, json={"path": "/pipeline/research", "body": {"companyIds": ["x"]}})
    assert r.status_code == 202
    job = r.json()
    assert (job["status"], job["label"]) == ("queued", "Researching companies")
    stored = next(iter(jobs.rows.values()))
    assert jobs.queued == [stored.id] and stored.params == {"path": "/pipeline/research", "body": {"companyIds": ["x"]}}
    assert stored.created_by == ADMIN.id and stored.workspace_id == WORKSPACE_ID


def test_only_long_running_endpoints_can_be_jobs(jobs) -> None:
    for path in ("/users", "/pipeline/research?x=1", "/icps"):
        assert jobs.client.post("/api/v1/jobs", headers=H, json={"path": path}).status_code == 422
    assert jobs.queued == []


def test_without_the_queue_the_job_fails_at_once(jobs) -> None:
    def down(_):
        raise ConnectionError("redis")

    app.dependency_overrides[get_job_queue] = lambda: down
    r = jobs.client.post("/api/v1/jobs", headers=H, json={"path": "/emails/generate", "body": {"leadIds": []}})
    assert r.status_code == 503 and "Redis" in r.json()["detail"]
    assert next(iter(jobs.rows.values())).status == "failed"


def test_a_finished_job_hands_back_what_the_endpoint_answered(jobs) -> None:
    job_id = jobs.client.post("/api/v1/jobs", headers=H, json={"path": "/posts/fetch", "body": {}}).json()["id"]
    row = jobs.rows[next(iter(jobs.rows))]
    row.status, row.result = "done", {"statusCode": 200, "body": {"fetched": 3}}
    out = jobs.client.get(f"/api/v1/jobs/{job_id}", headers=H).json()
    assert (out["status"], out["statusCode"], out["result"]) == ("done", 200, {"fetched": 3})


def test_a_stuck_job_says_so(jobs) -> None:
    jobs.client.post("/api/v1/jobs", headers=H, json={"path": "/posts/fetch", "body": {}})
    row = next(iter(jobs.rows.values()))
    row.created_at -= timedelta(minutes=1)
    assert "worker isn't running" in jobs.client.get(f"/api/v1/jobs/{row.id}", headers=H).json()["step"]
    row.status, row.started_at = "running", datetime.now(UTC) - timedelta(hours=3)
    out = jobs.client.get(f"/api/v1/jobs/{row.id}", headers=H).json()
    assert out["status"] == "failed" and "Start it again" in out["detail"]


def test_someone_elses_job_is_not_found(jobs) -> None:
    jobs.client.post("/api/v1/jobs", headers=H, json={"path": "/posts/fetch", "body": {}})
    row = next(iter(jobs.rows.values()))
    row.created_by = uuid4()
    from dependencies import get_current_user

    sales = SimpleNamespace(**{**ADMIN.__dict__, "role": "sales", "workspace_ids": [WORKSPACE_ID]})
    sales.is_admin = False
    sales.can_open = lambda w: w == WORKSPACE_ID
    app.dependency_overrides[get_current_user] = lambda: sales
    assert jobs.client.get(f"/api/v1/jobs/{row.id}", headers=H).status_code == 404
    row.workspace_id = uuid4()
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    assert jobs.client.get(f"/api/v1/jobs/{row.id}", headers=H).status_code == 404  # another workspace's


def test_the_worker_runs_the_endpoint_as_its_starter_and_past_the_guards(client) -> None:
    """The worker's in-process call: no browser secret and no session token, yet signed in as the job's
    starter, and allowed through the long-running guard. Bad input comes back as the endpoint's own answer."""
    for dep in (refuse_inline, require_api_secret):
        app.dependency_overrides.pop(dep)
    from database import get_session
    from dependencies import get_current_user

    app.dependency_overrides.pop(get_current_user)
    app.dependency_overrides[get_session] = lambda: None  # never reached: the input is refused first
    context = background.JobContext(uuid4(), ADMIN)
    status, body = asyncio.run(background._call(context, "/stakeholders/reveal", {"nope": 1}, WORKSPACE_ID))
    assert status == 422  # the endpoint's own validation: not 401 (signed in) or 409 (allowed to run)
    assert background._detail(body, status).startswith("stakeholderIds")
    assert background.current_job.get() is None  # nothing leaks out of the call


def test_a_job_that_runs_out_of_time_is_stopped_and_recorded(monkeypatch) -> None:
    """Enforced by the runner itself: Celery's own time limits do nothing on Windows (--pool=solo)."""
    from contextlib import asynccontextmanager

    from config import get_settings

    finished = {}
    job = SimpleNamespace(created_by=ADMIN.id, workspace_id=WORKSPACE_ID, params={"path": "/posts/fetch", "body": {}})

    class Jobs:
        def __init__(self, _session):
            pass

        async def start(self, _id):
            return job

        async def finish(self, _id, status, result, detail):
            finished.update(status=status, result=result, detail=detail)

    @asynccontextmanager
    async def session():
        yield None

    async def user(_users, _id):
        return ADMIN

    async def slow(*_args):
        await asyncio.sleep(5)

    async def no_dispose():
        return None

    monkeypatch.setattr(background, "JobRepo", Jobs)
    monkeypatch.setattr(background, "get_sessionmaker", lambda: session)
    monkeypatch.setattr(background, "_user", user)
    monkeypatch.setattr(background, "_call", slow)
    monkeypatch.setattr(background, "get_engine", lambda: SimpleNamespace(dispose=no_dispose))
    monkeypatch.setattr(get_settings(), "job_time_limit_seconds", 0.2)
    asyncio.run(background.run_job(uuid4()))
    assert finished["status"] == "failed" and "ran out of time" in finished["detail"]
    assert finished["result"]["statusCode"] == 504



def test_job_progress_counts_are_returned(jobs):
    jobs.client.post("/api/v1/jobs", headers=H, json={"path": "/posts/fetch", "body": {}})
    row = next(iter(jobs.rows.values()))
    row.status, row.done, row.total = "running", 7, 20
    row.started_at = datetime.now(UTC)
    detail = jobs.client.get(f"/api/v1/jobs/{row.id}", headers=H).json()
    assert detail["done"] == 7 and detail["total"] == 20
    recent = jobs.client.get("/api/v1/jobs", headers=H).json()
    assert recent[0]["done"] == 7 and recent[0]["total"] == 20


def test_recent_jobs_hide_large_results_and_other_workspaces(jobs):
    jobs.client.post("/api/v1/jobs", headers=H, json={"path": "/posts/fetch", "body": {}})
    row = next(iter(jobs.rows.values()))
    row.result = {"statusCode": 200, "body": {"secret": "large result"}}
    assert jobs.client.get("/api/v1/jobs", headers=H).json()[0]["result"] is None
    row.workspace_id = uuid4()
    assert jobs.client.get("/api/v1/jobs", headers=H).json() == []


def test_recent_jobs_only_show_starters_jobs_to_non_admins(jobs):
    jobs.client.post("/api/v1/jobs", headers=H, json={"path": "/posts/fetch", "body": {}})
    row = next(iter(jobs.rows.values()))
    row.created_by = uuid4()
    from dependencies import get_current_user
    sales = SimpleNamespace(**{**ADMIN.__dict__, "role": "sales", "workspace_ids": [WORKSPACE_ID]})
    sales.is_admin = False
    sales.can_open = lambda w: w == WORKSPACE_ID
    app.dependency_overrides[get_current_user] = lambda: sales
    assert jobs.client.get("/api/v1/jobs", headers=H).json() == []
