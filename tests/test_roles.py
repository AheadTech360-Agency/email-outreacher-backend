"""What each role may do, as the Roles tab says: Sales reviews, approves, queues and adds leads; Management also sets
up campaigns and spends budget; only admins change the budget, auto-send and scoring."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from dependencies import MANAGERS_ONLY, get_current_user, get_job_queue, get_job_repo, get_prompt_repo, get_settings_repo
from main import app
from services.auth import CurrentUser
from tests.conftest import WORKSPACE_ID
from tests.test_background import FakeJobs

H = {"X-Workspace-Id": str(WORKSPACE_ID)}


def as_role(role: str) -> CurrentUser:
    user = CurrentUser(uuid4(), f"{role}@example.com", role.title(), role, [WORKSPACE_ID])
    app.dependency_overrides[get_current_user] = lambda: user
    return user


# Every action that spends budget or changes the campaign setup: (method, path, body).
MANAGEMENT_ONLY = [
    ("post", "/prompts", {}), ("post", f"/prompts/{uuid4()}/versions", {}), ("patch", f"/prompts/{uuid4()}", {}),
    ("delete", f"/prompts/{uuid4()}", None),
    ("post", "/products", {}), ("put", f"/products/{uuid4()}", {}), ("delete", f"/products/{uuid4()}", None),
    ("post", "/examples", {}), ("put", f"/examples/{uuid4()}", {}), ("delete", f"/examples/{uuid4()}", None),
    ("post", f"/examples/from-email/{uuid4()}", {}),
    ("put", f"/icps/{uuid4()}/profile", {}),
    ("post", "/emails/generate", {}), ("post", "/posts/fetch", {}), ("post", "/improve/email-prompt", {}),
    ("post", "/pipeline/find", {}), ("post", "/pipeline/research", {}), ("post", "/pipeline/stakeholders", {}),
    ("patch", f"/pipeline/companies/{uuid4()}", {}),
    ("post", "/stakeholders/search", {}), ("post", "/stakeholders/search-icp", {}), ("post", "/stakeholders/search-linkedin", {}),
    ("post", "/stakeholders/reveal", {}),
    ("post", "/companies/import", {}), ("post", "/companies/remove", {}),
    ("post", "/leads/delete", {}), ("post", "/leads/move", {}),
    ("post", "/sending/follow-ups/run", {}),
    ("put", "/settings/feedback", {}), ("put", "/settings/research", {}),
    ("post", "/smart-lists/sort", {"kind": "company"}), ("post", "/smart-lists/move", {"kind": "company", "memberIds": [str(uuid4())]}), ("patch", f"/smart-lists/{uuid4()}", {}),
    ("post", f"/smart-lists/{uuid4()}/draft-icp", {}), ("post", f"/smart-lists/{uuid4()}/create-icp", {}),
    ("post", "/quickstart/runs", {}), ("post", f"/quickstart/runs/{uuid4()}/stop", {}), ("post", f"/quickstart/runs/{uuid4()}/resume", {}),
]  # fmt: skip


@pytest.mark.parametrize(("method", "path", "body"), MANAGEMENT_ONLY)
def test_sales_cant_set_up_campaigns_or_spend_budget(client, method, path, body) -> None:
    as_role("sales")
    kwargs = {"headers": H} | ({"json": body} if body is not None else {})
    r = getattr(client, method)(f"/api/v1{path}", **kwargs)
    assert r.status_code == 403 and r.json()["detail"] == MANAGERS_ONLY, (path, r.status_code)


def test_sales_paid_jobs_are_refused_before_they_are_queued_but_sending_isnt(client) -> None:
    as_role("sales")
    jobs, queued = FakeJobs(), []
    app.dependency_overrides[get_job_repo] = lambda: jobs
    app.dependency_overrides[get_job_queue] = lambda: queued.append
    for path in ("/emails/generate", "/pipeline/research", "/posts/fetch", "/stakeholders/reveal", "/pipeline/find"):
        r = client.post("/api/v1/jobs", headers=H, json={"path": path, "body": {}})
        assert r.status_code == 403 and r.json()["detail"] == MANAGERS_ONLY
    assert queued == []
    # "Send approved emails" is Sales' too.
    assert client.post("/api/v1/jobs", headers=H, json={"path": "/sending/queue", "body": {"emailIds": []}}).status_code == 202
    assert len(queued) == 1


def test_management_may_run_jobs(client) -> None:
    as_role("management")
    jobs, queued = FakeJobs(), []
    app.dependency_overrides[get_job_repo] = lambda: jobs
    app.dependency_overrides[get_job_queue] = lambda: queued.append
    assert client.post("/api/v1/jobs", headers=H, json={"path": "/emails/generate", "body": {}}).status_code == 202


# ---------- Shared settings (were kept only in each browser) ----------


class FakeSettings:
    def __init__(self) -> None:
        self.row = {"default_model": "claude-haiku-5-5", "posts_per_lead": 5, "max_post_age_days": 60, "monthly_budget_usd": 150.0,
                    "score_threshold": 80, "auto_send": False, "daily_auto_send_cap": 50, "scoring_prompt_id": None}  # fmt: skip

    async def workspace_exists(self, workspace_id) -> bool:
        return True

    async def general(self, workspace_id) -> dict:
        return dict(self.row)

    async def save_general(self, workspace_id, values) -> dict:
        self.row.update(values)
        return dict(self.row)


@pytest.fixture
def shared(client):
    repo = FakeSettings()
    score_prompt = SimpleNamespace(id=uuid4(), kind="score")

    class Prompts:
        async def get(self, workspace_id, prompt_id):
            return score_prompt if prompt_id == score_prompt.id else None

    app.dependency_overrides[get_settings_repo] = lambda: repo
    app.dependency_overrides[get_prompt_repo] = lambda: Prompts()
    return SimpleNamespace(client=client, repo=repo, score_prompt=score_prompt)


def body(**changes) -> dict:
    out = {"defaultModel": "claude-haiku-5-5", "postsPerLead": 5, "maxPostAgeDays": 60, "monthlyBudgetUsd": 150,
           "scoring": {"promptId": None, "threshold": 80, "autoSend": False, "dailyAutoSendCap": 50}}  # fmt: skip
    for key, value in changes.items():
        (out["scoring"] if key in out["scoring"] else out)[key] = value
    return out


def test_everyone_reads_the_same_saved_settings(shared) -> None:
    as_role("sales")
    got = shared.client.get("/api/v1/settings/general", headers=H).json()
    assert got["monthlyBudgetUsd"] == 150 and got["scoring"]["threshold"] == 80
    assert shared.client.put("/api/v1/settings/general", headers=H, json=body(postsPerLead=8)).status_code == 403


def test_management_changes_the_defaults_but_not_the_budget_or_scoring(shared) -> None:
    as_role("management")
    r = shared.client.put("/api/v1/settings/general", headers=H, json=body(defaultModel="claude-sonnet-5-5", postsPerLead=8))
    assert r.status_code == 200 and shared.repo.row["posts_per_lead"] == 8 and shared.repo.row["default_model"] == "claude-sonnet-5-5"
    for change in ({"monthlyBudgetUsd": 500}, {"threshold": 60}, {"autoSend": True}, {"dailyAutoSendCap": 10}):
        r = shared.client.put("/api/v1/settings/general", headers=H, json=body(postsPerLead=8, defaultModel="claude-sonnet-5-5", **change))
        assert r.status_code == 403 and "Only admins" in r.json()["detail"]
    assert shared.repo.row["monthly_budget_usd"] == 150


def test_admins_change_the_budget_and_scoring(shared) -> None:
    as_role("admin")
    r = shared.client.put("/api/v1/settings/general", headers=H,
                          json=body(monthlyBudgetUsd=500, threshold=70, promptId=str(shared.score_prompt.id)))  # fmt: skip
    assert r.status_code == 200 and r.json()["scoring"]["promptId"] == str(shared.score_prompt.id)
    assert (shared.repo.row["monthly_budget_usd"], shared.repo.row["score_threshold"]) == (500, 70)
    # Only one of this workspace's scoring prompts can be picked.
    bad = shared.client.put("/api/v1/settings/general", headers=H, json=body(promptId=str(uuid4())))
    assert bad.status_code == 422
