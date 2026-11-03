from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from dependencies import (
    get_current_user, get_exclusion_repo, get_icp_campaign_repo, get_icp_profile_repo, get_lead_intel_repo, get_optional_apify, get_optional_inboxes, get_optional_sequencer, refuse_inline,
    get_workspace_company_repo, require_api_secret,
)  # fmt: skip
from main import app
from services.auth import CurrentUser

NOW = datetime(2026, 10, 27, tzinfo=UTC)
ICP_ID = uuid4()
WORKSPACE_ID = uuid4()
COMPANY_ID = uuid4()
ADMIN = CurrentUser(uuid4(), "admin@example.com", "Admin", "admin")

ICP_ROW = {
    "id": ICP_ID,
    "name": "Med spas",
    "industry": "Beauty",
    "priority": 10,
    "enabled": True,
    "sources": ["website_text", "instagram_bio"],
    "any_of": ["med spa"],
    "none_of": ["school"],
    "website_any_of": [],
    "instagram_any_of": ["botox"],
    "require_all": False,
    "min_followers": 500,
    "max_followers": None,
    "search_terms": ["med spa"],
    "requires_website": True,
    "min_reviews": 10,
    "places": ["Austin, TX"],
    "company_count": 1,
    "created_at": NOW,
    "updated_at": NOW,
}

COMPANY_ROW = {
    "id": COMPANY_ID,
    "display_name": "Glow Med Spa",
    "status": "active",
    "city": "Austin",
    "state": "TX",
    "country": "us",
    "icp_id": ICP_ID,
    "icp_name": "Med spas",
    "tier": 1,
    "problem_score": 72.5,
    "website_status": "has_website",
    "domain": "glowmedspa.com",
    "created_at": NOW,
    "fields": {
        "website_url": "https://glowmedspa.com",
        "instagram_handle": "glowmedspa",
        "instagram_followers": "4210",
        "instagram_url": "https://www.instagram.com/glowmedspa/",
        "website_text": "Botox and fillers in Austin.",
    },
}


class FakeLeadIntelRepo:
    async def list_icps(self):
        return [ICP_ROW]

    async def get_icp(self, icp_id: UUID):
        return ICP_ROW if icp_id == ICP_ID else None

    async def list_companies(self, icp_id, q, limit, offset, *, no_icp=False, only_ids=None, exclude_ids=None):
        rows = [COMPANY_ROW] if icp_id in (None, ICP_ID) and not no_icp else []
        if only_ids is not None:
            rows = [r for r in rows if r["id"] in only_ids]
        if exclude_ids:
            rows = [r for r in rows if r["id"] not in exclude_ids]
        return rows, len(rows)

    async def contactable_ids(self, ids=None, icp_id=None):
        known = [COMPANY_ID]
        if icp_id is not None:
            known = known if icp_id == ICP_ID else []
        return [i for i in known if ids is None or i in ids]

    async def get_company(self, company_id: UUID):
        return COMPANY_ROW if company_id == COMPANY_ID else None


class FakeIcpProfileRepo:
    def __init__(self) -> None:
        self.saved: dict[tuple[UUID, UUID], SimpleNamespace] = {}

    async def workspace_exists(self, workspace_id):
        return workspace_id == WORKSPACE_ID

    async def get_many(self, workspace_id, icp_ids):
        return {i: self.saved[(workspace_id, i)] for i in icp_ids if (workspace_id, i) in self.saved}

    async def upsert(self, workspace_id, icp_id, values):
        self.saved[(workspace_id, icp_id)] = SimpleNamespace(lead_intel_icp_id=icp_id, updated_at=NOW, **values)
        return self.saved[(workspace_id, icp_id)]


class FakeWorkspaceCompanyRepo:
    def __init__(self) -> None:
        self.links: dict[tuple[UUID, UUID], datetime] = {}

    async def workspace_exists(self, workspace_id):
        return workspace_id == WORKSPACE_ID

    async def imported(self, workspace_id):
        return {cid: at for (ws, cid), at in self.links.items() if ws == workspace_id}

    async def add(self, workspace_id, company_ids):
        new = [cid for cid in dict.fromkeys(company_ids) if (workspace_id, cid) not in self.links]
        for cid in new:
            self.links[(workspace_id, cid)] = NOW
        return len(new)

    async def remove(self, workspace_id, company_ids):
        gone = [cid for cid in company_ids if self.links.pop((workspace_id, cid), None)]
        return len(gone)


@pytest.fixture
def workspace_companies() -> FakeWorkspaceCompanyRepo:
    return FakeWorkspaceCompanyRepo()


class FakeExclusions:
    """The do-not-contact list, in memory."""

    def __init__(self) -> None:
        self.rows: list[SimpleNamespace] = []

    async def list(self):
        return list(self.rows)

    async def add(self, entries, added_by):
        added = []
        for e in entries:
            if any((r.key_type, r.key_value) == (e["key_type"], e["key_value"]) for r in self.rows):
                continue
            row = SimpleNamespace(id=uuid4(), added_by=added_by, created_at=NOW, **{"reason": "do_not_contact", "business_name": "", "note": "", **e})
            self.rows.append(row)
            added.append(row)
        return added

    async def get(self, exclusion_id):
        return next((r for r in self.rows if r.id == exclusion_id), None)

    async def delete(self, row):
        self.rows.remove(row)

    async def matcher(self):
        rows = {(r.key_type, r.key_value): r for r in self.rows}
        return lambda keys: next((rows[k] for k in keys if k in rows), None)

    async def match(self, keys):
        return (await self.matcher())(keys)


@pytest.fixture
def exclusions() -> FakeExclusions:
    return FakeExclusions()


@pytest.fixture
def profiles() -> FakeIcpProfileRepo:
    return FakeIcpProfileRepo()


@pytest.fixture(autouse=True)
def no_real_sending(monkeypatch):
    """Tests never reach CheapSequencer or CheapInboxes: their keys are blanked and their clients are None
    unless a test fakes them over HTTP. Nothing can create, change or send anything there."""
    from config import get_settings

    monkeypatch.setattr(get_settings(), "cheapsequencer_api_key", None)
    monkeypatch.setattr(get_settings(), "cheapinboxes_api_key", None)
    app.dependency_overrides[get_optional_sequencer] = lambda: None
    app.dependency_overrides[get_optional_inboxes] = lambda: None
    # Apify too: reading prices is free, but tests never reach a real account.
    app.dependency_overrides[get_optional_apify] = lambda: None
    links = FakeIcpCampaigns()
    app.dependency_overrides[get_icp_campaign_repo] = lambda: links
    # The frontend-server secret is tested on its own (tests/test_api_secret.py); other tests skip it.
    app.dependency_overrides[require_api_secret] = lambda: None
    # Long-running endpoints only run as background jobs (tests/test_jobs.py); other tests call them directly.
    app.dependency_overrides[refuse_inline] = lambda: None
    yield links
    for dep in (get_optional_sequencer, get_optional_inboxes, get_optional_apify, get_icp_campaign_repo, require_api_secret, refuse_inline):
        app.dependency_overrides.pop(dep, None)


class FakeIcpCampaigns:
    """eo_icp_campaigns in memory."""

    def __init__(self) -> None:
        self.rows: dict[UUID, str] = {}
        self.in_use: dict[str, int] = {}  # what usage() reports
        self.forgotten: list[UUID] = []
        self.sending_count = 0  # emails queued or sent for the ICP
        self.detached: list[UUID] = []

    async def all(self) -> dict[UUID, str]:
        return dict(self.rows)

    async def link(self, icp_id: UUID, campaign_id: str) -> None:
        self.rows[icp_id] = campaign_id

    async def unlink(self, icp_id: UUID) -> None:
        self.rows.pop(icp_id, None)

    async def usage(self, icp_id: UUID) -> dict[str, int]:
        return dict(self.in_use)

    async def sending(self, icp_id: UUID) -> int:
        return self.sending_count

    async def detach(self, icp_id: UUID) -> None:
        self.detached.append(icp_id)

    async def forget(self, icp_id: UUID) -> None:
        self.rows.pop(icp_id, None)
        self.forgotten.append(icp_id)


@pytest.fixture
def client(profiles: FakeIcpProfileRepo, workspace_companies: FakeWorkspaceCompanyRepo, exclusions: FakeExclusions):
    app.dependency_overrides[get_lead_intel_repo] = FakeLeadIntelRepo
    app.dependency_overrides[get_workspace_company_repo] = lambda: workspace_companies
    app.dependency_overrides[get_icp_profile_repo] = lambda: profiles
    # Signed in as an admin, who opens every workspace. tests/test_auth.py covers the other roles.
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    app.dependency_overrides[get_exclusion_repo] = lambda: exclusions
    yield TestClient(app)
    app.dependency_overrides.clear()
