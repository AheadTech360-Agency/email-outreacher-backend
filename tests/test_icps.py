from uuid import uuid4

import pytest

from tests.conftest import ICP_ID, WORKSPACE_ID

WS = {"X-Workspace-Id": str(WORKSPACE_ID)}


def test_list_icps_without_profile(client) -> None:
    response = client.get("/api/v1/icps", headers=WS)
    assert response.status_code == 200
    [icp] = response.json()
    assert icp["id"] == str(ICP_ID)
    assert icp["searchTerms"] == ["med spa"]
    assert icp["companyCount"] == 1
    assert icp["profile"] is None
    # Every Lead Intel field comes through, camelCased.
    assert (icp["sources"], icp["anyOf"], icp["noneOf"], icp["instagramAnyOf"]) == (
        ["website_text", "instagram_bio"], ["med spa"], ["school"], ["botox"],
    )  # fmt: skip
    assert (icp["requireAll"], icp["minFollowers"], icp["maxFollowers"], icp["requiresWebsite"], icp["minReviews"]) == (
        False, 500, None, True, 10,
    )  # fmt: skip


def test_get_unknown_icp_is_404(client) -> None:
    assert client.get(f"/api/v1/icps/{uuid4()}").status_code == 404


def test_save_profile_then_read_it_back(client) -> None:
    body = {"titles": ["Owner", "Founder"], "offer": "More bookings", "enabledSources": ["instagram"]}
    saved = client.put(f"/api/v1/icps/{ICP_ID}/profile", json=body, headers=WS)
    assert saved.status_code == 200
    assert saved.json()["titles"] == ["Owner", "Founder"]

    profile = client.get(f"/api/v1/icps/{ICP_ID}", headers=WS).json()["profile"]
    assert profile["offer"] == "More bookings"
    assert profile["enabledSources"] == ["instagram"]


def test_profiles_are_per_workspace(client) -> None:
    client.put(f"/api/v1/icps/{ICP_ID}/profile", json={"offer": "x"}, headers=WS)
    assert client.get(f"/api/v1/icps/{ICP_ID}", headers={"X-Workspace-Id": str(uuid4())}).json()["profile"] is None
    assert client.get(f"/api/v1/icps/{ICP_ID}").json()["profile"] is None


def test_save_profile_needs_a_workspace(client) -> None:
    assert client.put(f"/api/v1/icps/{ICP_ID}/profile", json={}).status_code == 400
    other = {"X-Workspace-Id": str(uuid4())}
    assert client.put(f"/api/v1/icps/{ICP_ID}/profile", json={}, headers=other).status_code == 404


def test_save_profile_for_unknown_icp_is_404(client) -> None:
    assert client.put(f"/api/v1/icps/{uuid4()}/profile", json={}, headers=WS).status_code == 404


class FakeIcpWriter:
    def __init__(self) -> None:
        self.added: list[dict] = []
        self.updated, self.deleted, self.businesses = [], [], 0

    async def name_taken(self, name):
        return name.lower() in {"med spas", *(a["name"].lower() for a in self.added)}

    async def add_icp(self, created_by, values):
        from datetime import UTC, datetime

        row = {"id": uuid4(), "created_by": created_by, "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC), **values}
        self.added.append(row)
        return row

    async def update_icp(self, icp_id, values):
        self.updated.append((icp_id, values))
        return {"id": icp_id, "name": "Med spas", **values}

    async def businesses_in(self, icp_id):
        return self.businesses

    async def delete_icp(self, icp_id):
        self.deleted.append(icp_id)
        return True


@pytest.fixture
def writer(client, monkeypatch):
    from config import get_settings
    from dependencies import get_lead_intel_icp_writer
    from main import app

    fake = FakeIcpWriter()
    app.dependency_overrides[get_lead_intel_icp_writer] = lambda: fake
    monkeypatch.setattr(get_settings(), "lead_intel_icp_created_by", uuid4())
    return fake


NEW_ICP = {
    "name": "  Sign shops ",
    "industry": "Printing",
    "priority": 30,
    "sources": ["website_text", "instagram_bio", "website_text"],
    "anyOf": ["sign shop", " Sign Shop ", "", "vinyl signs"],
    "noneOf": ["school"],
    "minFollowers": 100,
    "maxFollowers": 50000,
    "searchTerms": ["sign shop"],
    "places": ["Chicago"],
}


def test_create_icp_with_lead_intel_fields(client, writer) -> None:
    created = client.post("/api/v1/icps", json=NEW_ICP)
    assert created.status_code == 201, created.text
    icp = created.json()
    assert (icp["name"], icp["industry"], icp["priority"], icp["companyCount"], icp["profile"]) == ("Sign shops", "Printing", 30, 0, None)
    assert icp["sources"] == ["website_text", "instagram_bio"]
    assert icp["anyOf"] == ["sign shop", "vinyl signs"]  # trimmed, blanks and repeats dropped
    [row] = writer.added
    assert row["created_by"] is not None and row["min_followers"] == 100


def test_creating_an_icp_sets_up_its_campaign(client, writer) -> None:
    from dependencies import get_optional_sequencer
    from main import app
    from services.sending import SendingError

    class Down:
        async def campaigns(self):
            raise SendingError("Couldn't reach CheapSequencer: ConnectError")

    app.dependency_overrides[get_optional_sequencer] = lambda: Down()
    created = client.post("/api/v1/icps", json=NEW_ICP)
    # The ICP is still made; the campaign problem is reported, not raised.
    assert created.status_code == 201 and len(writer.added) == 1
    assert created.json()["campaignNote"].startswith("Campaign not set up: Couldn't reach CheapSequencer")


def test_create_icp_validation(client, writer) -> None:
    assert client.post("/api/v1/icps", json={**NEW_ICP, "name": "Med Spas"}).status_code == 409
    assert client.post("/api/v1/icps", json={**NEW_ICP, "industry": "  "}).status_code == 422
    assert client.post("/api/v1/icps", json={**NEW_ICP, "sources": ["tiktok"]}).status_code == 422
    assert client.post("/api/v1/icps", json={**NEW_ICP, "minFollowers": 10, "maxFollowers": 5}).status_code == 422
    assert writer.added == []


def test_create_icp_needs_created_by(client, writer, monkeypatch) -> None:
    from config import get_settings

    monkeypatch.setattr(get_settings(), "lead_intel_icp_created_by", None)
    assert client.post("/api/v1/icps", json=NEW_ICP).status_code == 503


# ---------- Archiving and deleting ----------


class Campaigns:
    """CheapSequencer's campaigns and enrollments, faked."""

    def __init__(self, enrolled=()):
        self.list = [{"id": "c1", "name": "Coldbrew: Med spas", "status": "draft", "steps": []}]
        self.enrolled = [{"status": s} for s in enrolled]
        self.archived, self.copied = [], []

    async def campaigns(self):
        return [c for c in self.list if c["id"] not in self.archived]

    async def enrollments(self, campaign_id):
        return self.enrolled

    async def campaign(self, campaign_id):
        return next(c for c in self.list if c["id"] == campaign_id), "etag"

    async def archive(self, campaign_id, etag, key):
        self.archived.append(campaign_id)

    async def duplicate(self, campaign_id, key):
        copy = {"id": "c2", "name": "copy", "status": "draft"}
        self.list.append(copy)
        self.copied.append(campaign_id)
        return copy

    async def rename(self, campaign_id, name, etag, key):
        next(c for c in self.list if c["id"] == campaign_id)["name"] = name

    async def configure_new_campaign(self, campaign_id, name, cap, sending_days, etag, key):
        row = next(c for c in self.list if c["id"] == campaign_id)
        row.update(name=name, daily_send_cap=cap, sending_days=sending_days)

    async def set_daily_cap(self, campaign_id, cap, etag, key):
        next(c for c in self.list if c["id"] == campaign_id)["daily_send_cap"] = cap


@pytest.fixture
def sequencer(no_real_sending):
    from dependencies import get_optional_sequencer
    from main import app

    fake = Campaigns()
    no_real_sending.rows[ICP_ID] = "c1"
    app.dependency_overrides[get_optional_sequencer] = lambda: fake
    return fake


def test_archiving_turns_the_icp_off_and_archives_its_empty_campaign(client, writer, sequencer, no_real_sending) -> None:
    sequencer.enrolled = [{"status": "completed"}, {"status": "replied"}]
    r = client.post(f"/api/v1/icps/{ICP_ID}/archive")
    assert r.status_code == 200, r.text
    assert writer.updated == [(ICP_ID, {"enabled": False})] and sequencer.archived == ["c1"]
    assert ICP_ID not in no_real_sending.rows and "was archived" in r.json()["campaignNote"]


def test_archiving_waits_while_leads_are_mid_sequence(client, writer, sequencer) -> None:
    sequencer.enrolled = [{"status": "active"}, {"status": "active"}, {"status": "completed"}]
    r = client.post(f"/api/v1/icps/{ICP_ID}/archive")
    assert r.status_code == 409 and r.json()["detail"].startswith('2 leads are still in "Coldbrew: Med spas"')
    assert writer.updated == [] and sequencer.archived == []


def test_restoring_turns_it_on_and_gives_it_a_new_campaign(client, writer, sequencer, no_real_sending) -> None:
    no_real_sending.rows.clear()
    sequencer.list.append({"id": "t", "name": "Coldbrew: Other", "status": "draft"})
    sequencer.list[0]["status"] = "archived"
    r = client.post(f"/api/v1/icps/{ICP_ID}/restore")
    assert r.status_code == 200, r.text
    assert writer.updated == [(ICP_ID, {"enabled": True})] and sequencer.copied == ["t"]
    assert no_real_sending.rows == {ICP_ID: "c2"} and "created" in r.json()["campaignNote"]


def test_deleting_an_unused_icp(client, writer, sequencer, no_real_sending) -> None:
    assert client.delete(f"/api/v1/icps/{ICP_ID}").status_code == 204
    assert writer.deleted == [ICP_ID] and sequencer.archived == ["c1"] and no_real_sending.forgotten == [ICP_ID]


def test_an_icp_with_lead_intel_businesses_is_not_deleted(client, writer, sequencer, no_real_sending) -> None:
    writer.businesses = 12
    r = client.delete(f"/api/v1/icps/{ICP_ID}")
    assert r.status_code == 409
    assert r.json()["detail"] == "Med spas has 12 Lead Intel businesses in it, so it can't be deleted. Archive it instead."
    assert writer.deleted == [] and sequencer.archived == [] and no_real_sending.detached == []


def test_an_icp_with_emails_out_is_not_deleted(client, writer, sequencer, no_real_sending) -> None:
    no_real_sending.sending_count = 4
    r = client.delete(f"/api/v1/icps/{ICP_ID}")
    assert r.status_code == 409 and r.json()["detail"].startswith("4 emails for Med spas are queued or sent")
    assert writer.deleted == [] and sequencer.archived == []


def test_deleting_an_icp_keeps_its_companies_with_no_icp(client, writer, sequencer, no_real_sending) -> None:
    """Companies and people under it in Coldbrew don't stop the delete: they're kept, under "No ICP"."""
    no_real_sending.in_use = {"leads": 3, "companies": 40}
    assert client.delete(f"/api/v1/icps/{ICP_ID}").status_code == 204
    assert writer.deleted == [ICP_ID] and no_real_sending.detached == [ICP_ID]


def test_deleting_needs_a_manager(client, writer) -> None:
    from dependencies import get_current_user
    from main import app
    from services.auth import CurrentUser

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(uuid4(), "s@x.com", "S", "sales", [WORKSPACE_ID])
    assert client.delete(f"/api/v1/icps/{ICP_ID}").status_code == 403
    assert client.post(f"/api/v1/icps/{ICP_ID}/archive").status_code == 403
    assert writer.deleted == [] and writer.updated == []
