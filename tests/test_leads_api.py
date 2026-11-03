from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dependencies import get_lead_repo
from main import app
from services.leads import Existing, remember
from tests.conftest import ICP_ID, WORKSPACE_ID

WS = {"X-Workspace-Id": str(WORKSPACE_ID)}
CSV = {"Content-Type": "text/csv", **WS}


class FakeLeadRepo:
    def __init__(self) -> None:
        self.rows: list[SimpleNamespace] = []
        # Simulates someone else adding this email between the check and the insert.
        self.race_email: str | None = None

    async def workspace_exists(self, workspace_id):
        return workspace_id == WORKSPACE_ID

    async def queued_at(self, lead_ids):
        return {}

    async def post_counts(self, lead_ids):
        return {}

    async def find_existing(self, workspace_id, leads):
        found = Existing()
        for row in self.rows:
            remember(row.input, found)
        return found

    async def insert_many(self, workspace_id, icp_id, origin, leads):
        ids = []
        for lead in leads:
            if lead.email and lead.email == self.race_email:
                ids.append(None)
                continue
            row = SimpleNamespace(
                id=uuid4(), input=lead, lead_intel_icp_id=icp_id, origin=origin, status="imported", score=None,
                email_status=None, created_at=datetime.now(UTC), extra=lead.extra,
                **{k: getattr(lead, k) or (None if k in ("email", "linkedin_url", "instagram_url", "facebook_url") else "")
                   for k in ("first_name", "last_name", "email", "title", "company", "website", "linkedin_url",
                             "instagram_url", "facebook_url")},
                domain=lead.domain or None,
            )  # fmt: skip
            self.rows.append(row)
            ids.append(row.id)
        return ids

    async def get(self, workspace_id, lead_id):
        return next(r for r in self.rows if r.id == lead_id)

    async def list(self, workspace_id, icp_id, status, q, limit, offset):
        return self.rows[offset : offset + limit], len(self.rows)


@pytest.fixture
def leads(client):
    repo = FakeLeadRepo()
    app.dependency_overrides[get_lead_repo] = lambda: repo
    return repo


def test_template_downloads_as_csv(client) -> None:
    response = client.get("/api/v1/leads/template.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.text.splitlines()[0] == (
        "first_name,last_name,email,company,title,website,linkedin_url,instagram_url,facebook_url"
    )


def test_add_lead_then_duplicate_is_409(client, leads) -> None:
    body = {"email": "Ava@Brightloom.com", "firstName": "Ava", "icpId": str(ICP_ID)}
    created = client.post("/api/v1/leads", json=body, headers=WS)
    assert created.status_code == 201, created.text
    assert created.json()["email"] == "ava@brightloom.com"
    assert created.json()["website"] == "https://brightloom.com"
    assert created.json()["origin"] == "manual"
    assert created.json()["icpId"] == str(ICP_ID)

    again = client.post("/api/v1/leads", json={"email": "ava@brightloom.com"}, headers=WS)
    assert again.status_code == 409
    assert "same email" in again.json()["detail"]


def test_add_lead_validation(client, leads) -> None:
    assert client.post("/api/v1/leads", json={"company": "X"}, headers=WS).status_code == 422
    assert client.post("/api/v1/leads", json={"email": "a@x.com"}).status_code == 400
    assert client.post("/api/v1/leads", json={"email": "a@x.com"}, headers={"X-Workspace-Id": str(uuid4())}).status_code == 404
    assert client.post("/api/v1/leads", json={"email": "a@x.com", "icpId": str(uuid4())}, headers=WS).status_code == 404


def test_import_reports_every_row(client, leads) -> None:
    client.post("/api/v1/leads", json={"email": "liam@northpeak.io"}, headers=WS)
    csv = (
        "First Name,Last Name,Email,Company,LinkedIn,City\n"
        "Ava,Morgan,ava@brightloom.com,Brightloom,linkedin.com/in/ava,Austin\n"
        "Liam,Carter,LIAM@northpeak.io,Northpeak,,Denver\n"
        "Ava,Morgan,AVA@brightloom.com,Brightloom,,Austin\n"
        "Grace,Lee,,Harbor,,Seattle\n"
        "Bo,Chen,,Kite,https://www.linkedin.com/in/bo/,\n"
    )
    result = client.post("/api/v1/leads/import", content=csv, headers=CSV).json()
    assert (result["totalRows"], result["imported"], result["duplicatesExisting"], result["duplicatesInFile"], result["invalid"]) == (5, 2, 1, 1, 1)
    outcomes = {r["row"]: (r["outcome"], r["reason"]) for r in result["rows"]}
    assert outcomes[2][0] == "imported"
    assert outcomes[3] == ("duplicate", "Already in this workspace (same email)")
    assert outcomes[4] == ("duplicate", "Repeated in this file (same email)")
    assert outcomes[5] == ("invalid", "Needs an email or a LinkedIn URL")
    assert outcomes[6][0] == "imported"
    assert {"column": "City", "field": None} in result["columns"]
    assert leads.rows[-2].input.extra == {"City": "Austin"}
    assert leads.rows[-2].origin == "csv"


def test_dry_run_saves_nothing(client, leads) -> None:
    result = client.post("/api/v1/leads/import?dryRun=true", content="email\na@x.com\n", headers=CSV).json()
    assert result["dryRun"] and result["imported"] == 1
    assert result["rows"][0]["leadId"] is None
    assert leads.rows == []


def test_race_with_another_insert_counts_as_duplicate(client, leads) -> None:
    leads.race_email = "a@x.com"
    result = client.post("/api/v1/leads/import", content="email\na@x.com\nb@x.com\n", headers=CSV).json()
    assert result["imported"] == 1 and result["duplicatesExisting"] == 1
    assert result["rows"][0]["outcome"] == "duplicate"


def test_unreadable_csv_is_422(client, leads) -> None:
    response = client.post("/api/v1/leads/import", content="foo,bar\n1,2\n", headers=CSV)
    assert response.status_code == 422
    assert "No recognisable columns" in response.json()["detail"]


def test_list_leads(client, leads) -> None:
    client.post("/api/v1/leads", json={"email": "a@x.com"}, headers=WS)
    page = client.get("/api/v1/leads", headers=WS).json()
    assert page["total"] == 1 and page["items"][0]["email"] == "a@x.com"
    assert client.get("/api/v1/leads?status=bogus", headers=WS).status_code == 422
