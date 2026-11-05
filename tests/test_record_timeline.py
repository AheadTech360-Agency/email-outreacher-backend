from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace as NS
from uuid import uuid4

from database import get_session
from main import app
from services.record_timeline import build_timeline
from tests.conftest import WORKSPACE_ID

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


def person(**extra):
    return NS(**({"id": uuid4(), "found_at": NOW-timedelta(days=3), "revealed_at": None,
        "source": "manual", "company_id": None, "lead_id": None} | extra))


def test_timeline_combines_actual_milestones_and_sending_events_in_order():
    p = person(revealed_at=NOW-timedelta(days=2))
    e = NS(id=uuid4(), step=1, subject="A subject", created_at=NOW-timedelta(days=1), approved_at=NOW-timedelta(hours=10),
           queued_at=NOW-timedelta(hours=9), sent_at=NOW-timedelta(hours=8))
    reply = NS(id=uuid4(), occurred_at=NOW, type="replied", provider="cheapsequencer", email_id=e.id)
    feedback = NS(id=uuid4(), updated_at=NOW-timedelta(hours=12), rating="good", note="Clear ask", email_id=e.id)
    events = build_timeline(p, emails=[e], feedbacks=[feedback], events=[reply])
    assert events[0]["title"] == "Sending event: replied"
    assert events[-1]["title"] == "Person added"
    assert len(events) == 8
    assert any(event["title"] == "Email rated good" and event["emailId"] == str(e.id) for event in events)


def test_absent_timestamps_are_not_invented_and_timezones_are_normalized():
    p = person(found_at=NOW.astimezone(timezone(timedelta(hours=-5))))
    events = build_timeline(p)
    assert len(events) == 1 and events[0]["at"] == NOW.isoformat()


def test_timeline_is_bounded_to_latest_100_events():
    posts = [NS(id=uuid4(), fetched_at=NOW-timedelta(minutes=i), source="linkedin", text="Post") for i in range(110)]
    events = build_timeline(person(), posts=posts)
    assert len(events) == 100 and events[0]["at"] == NOW.isoformat()


class ReadSession:
    def __init__(self, row): self.row, self.statements = row, []
    async def scalar(self, query): self.statements.append(query); return self.row


def test_person_timeline_endpoint_scopes_access_to_workspace(client):
    p = person()
    session = ReadSession(p)
    app.dependency_overrides[get_session] = lambda: session
    h = {"X-Workspace-Id": str(WORKSPACE_ID)}
    result = client.get(f"/api/v1/records/person/{p.id}/timeline", headers=h)
    assert result.status_code == 200 and result.json()["events"][0]["title"] == "Person added"
    from sqlalchemy.dialects import postgresql
    statement = str(session.statements[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert str(WORKSPACE_ID) in statement and str(p.id) in statement
    session.row = None
    assert client.get(f"/api/v1/records/person/{p.id}/timeline", headers=h).status_code == 404
