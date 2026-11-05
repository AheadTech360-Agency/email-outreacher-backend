"""Deletion authorization, transaction boundaries, workspace scope, and cascade contracts. No network."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from database import get_session
from dependencies import get_current_user
from main import app
from models import CompanyPage, Email, EmailFeedback, Insight, Post
from routers.records import delete_records
from services.auth import CurrentUser
from tests.conftest import WORKSPACE_ID


class Session:
    def __init__(self, batches, queued=None, fail=False):
        self.batches = iter(batches)
        self.queued, self.fail = queued, fail
        self.statements = []
        self.commits = self.rollbacks = 0
    async def scalars(self, statement):
        self.statements.append(statement)
        return next(self.batches)
    async def scalar(self, statement):
        self.statements.append(statement)
        return self.queued
    async def execute(self, statement):
        self.statements.append(statement)
        if self.fail:
            raise RuntimeError("database failed")
        return SimpleNamespace(rowcount=1)
    async def commit(self):
        self.commits += 1
    async def rollback(self):
        self.rollbacks += 1


def sql(statement):
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_company_cascade_is_one_scoped_transaction():
    company_id, person_id, lead_id, business_id = [uuid4() for _ in range(4)]
    session = Session([[SimpleNamespace(id=company_id, lead_intel_business_id=business_id)],
        [SimpleNamespace(id=person_id, lead_id=lead_id)], [lead_id]])
    assert asyncio.run(delete_records(session, WORKSPACE_ID, "company", [company_id, uuid4()])) == 1
    deletes = [sql(s) for s in session.statements if sql(s).startswith("DELETE")]
    assert len(deletes) == 6
    assert all(str(WORKSPACE_ID) in statement for statement in deletes)
    for table in ["eo_stakeholders", "eo_leads", "eo_companies", "eo_workspace_companies", "eo_smart_list_members"]:
        assert any(table in statement for statement in deletes)
    assert session.commits == 1 and session.rollbacks == 0


def test_person_deletes_linked_lead_and_membership():
    person_id, lead_id = uuid4(), uuid4()
    session = Session([[SimpleNamespace(id=person_id, lead_id=lead_id)], [lead_id]])
    assert asyncio.run(delete_records(session, WORKSPACE_ID, "person", [person_id])) == 1
    deletes = [sql(s) for s in session.statements if sql(s).startswith("DELETE")]
    assert len(deletes) == 3 and all(str(WORKSPACE_ID) in s for s in deletes)


def test_email_deletes_only_email():
    email_id = uuid4()
    session = Session([[SimpleNamespace(id=email_id)]])
    assert asyncio.run(delete_records(session, WORKSPACE_ID, "email", [email_id])) == 1
    deletes = [sql(s) for s in session.statements if sql(s).startswith("DELETE")]
    assert len(deletes) == 1 and "eo_emails" in deletes[0]


def test_queued_records_are_not_deleted():
    email_id = uuid4()
    session = Session([[SimpleNamespace(id=email_id)]], queued=email_id)
    with pytest.raises(HTTPException) as error:
        asyncio.run(delete_records(session, WORKSPACE_ID, "email", [email_id]))
    assert error.value.status_code == 409
    assert not any(sql(s).startswith("DELETE") for s in session.statements)
    assert session.commits == 0 and session.rollbacks == 1


def test_failure_rolls_back_without_committing():
    email_id = uuid4()
    session = Session([[SimpleNamespace(id=email_id)]], fail=True)
    with pytest.raises(RuntimeError):
        asyncio.run(delete_records(session, WORKSPACE_ID, "email", [email_id]))
    assert session.commits == 0 and session.rollbacks == 1


def test_unknown_or_other_workspace_records_are_untouched():
    session = Session([[]])
    assert asyncio.run(delete_records(session, WORKSPACE_ID, "person", [uuid4()])) == 0
    assert len(session.statements) == 1
    assert str(WORKSPACE_ID) in sql(session.statements[0])


@pytest.mark.parametrize("model,column", [(Post,"lead_id"), (Post,"company_id"), (Insight,"lead_id"),
    (Email,"lead_id"), (EmailFeedback,"email_id"), (CompanyPage,"company_id")])
def test_dependent_records_have_database_cascades(model, column):
    assert next(iter(model.__table__.c[column].foreign_keys)).ondelete == "CASCADE"


def test_route_requires_management(client):
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(uuid4(), "sales@example.com", "Sales", "sales")
    session = Session([])
    app.dependency_overrides[get_session] = lambda: session
    response = client.post("/api/v1/records/company/delete", headers={"X-Workspace-Id": str(WORKSPACE_ID)}, json={"ids": [str(uuid4())]})
    assert response.status_code == 403 and not session.statements


def test_route_deletes_and_validates_input(client):
    email_id = uuid4()
    session = Session([[SimpleNamespace(id=email_id)]])
    app.dependency_overrides[get_session] = lambda: session
    h = {"X-Workspace-Id": str(WORKSPACE_ID)}
    response = client.post("/api/v1/records/email/delete", headers=h, json={"ids": [str(email_id), str(email_id)]})
    assert response.status_code == 200 and response.json() == {"deleted": 1}
    assert session.commits == 1
    assert client.post("/api/v1/records/email/delete", headers=h, json={"ids": []}).status_code == 422


@pytest.mark.parametrize("kind", ["company", "person", "product"])
@pytest.mark.parametrize("archived", [True, False])
def test_archive_restore_persists_workspace_scoped_flag_without_deletion(client, kind, archived):
    session = Session([])
    app.dependency_overrides[get_session] = lambda: session
    response = client.post(f"/api/v1/records/{kind}/archive", headers={"X-Workspace-Id": str(WORKSPACE_ID)},
        json={"ids": [str(uuid4())], "archived": archived})
    assert response.status_code == 200 and response.json() == {"changed": 1}
    assert session.commits == 1 and session.rollbacks == 0
    statement = sql(session.statements[0])
    assert statement.startswith("UPDATE") and str(WORKSPACE_ID) in statement
    assert "archived=" + ("true" if archived else "false") in statement
    assert not any(sql(s).startswith("DELETE") for s in session.statements)


def test_archive_failure_rolls_back(client):
    session = Session([], fail=True)
    app.dependency_overrides[get_session] = lambda: session
    with pytest.raises(RuntimeError):
        client.post("/api/v1/records/company/archive", headers={"X-Workspace-Id": str(WORKSPACE_ID)}, json={"ids": [str(uuid4())]})
    assert session.commits == 0 and session.rollbacks == 1


def test_no_email_cleanup_rechecks_database_instead_of_trusting_ui():
    session = Session([[]])
    assert asyncio.run(delete_records(session, WORKSPACE_ID, "person", [uuid4()], no_email_only=True)) == 0
    statement = sql(session.statements[0])
    assert "email IS NULL" in statement and "trim(" in statement
    assert not any(sql(s).startswith("DELETE") for s in session.statements)


def test_archiving_requires_management(client):
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(uuid4(), "sales@example.com", "Sales", "sales")
    session = Session([])
    app.dependency_overrides[get_session] = lambda: session
    response = client.post("/api/v1/records/person/archive", headers={"X-Workspace-Id": str(WORKSPACE_ID)}, json={"ids": [str(uuid4())]})
    assert response.status_code == 403 and not session.statements
