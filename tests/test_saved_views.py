from types import SimpleNamespace as NS
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from database import get_session
from dependencies import get_current_user
from main import app
from services.auth import CurrentUser
from tests.conftest import ADMIN, WORKSPACE_ID


def row(**values):
    return NS(**({"id": uuid4(), "workspace_id": WORKSPACE_ID, "user_id": ADMIN.id,
                 "name": "Retail", "page": "people", "visibility": "personal", "filters": {"query": "retail"}} | values))


class Session:
    def __init__(self, rows=(), target=None):
        self.rows, self.target, self.queries = list(rows), target, []
        self.commits, self.added, self.deleted = 0, [], []
    async def scalars(self, query): self.queries.append(query); return self.rows
    async def scalar(self, query):
        self.queries.append(query)
        return WORKSPACE_ID if "eo_workspaces" in str(query) else self.target
    def add(self, value): value.id = uuid4(); self.added.append(value)
    async def delete(self, value): self.deleted.append(value)
    async def commit(self): self.commits += 1
    async def refresh(self, value): pass


def setup(session, user=None):
    app.dependency_overrides[get_session] = lambda: session
    if user: app.dependency_overrides[get_current_user] = lambda: user
    return {"X-Workspace-Id": str(WORKSPACE_ID)}


def sql(query):
    return str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def sales():
    return CurrentUser(uuid4(), "sales@example.com", "Sales", "sales", (WORKSPACE_ID,))


def payload(**changes):
    return {"page": "people", "name": "Retail", "filters": {"query": "retail"}, **changes}


def test_reads_scope_to_workspace_page_and_personal_owner(client):
    session = Session([row(), row(visibility="workspace")])
    response = client.get("/api/v1/saved-views?page=people", headers=setup(session))
    assert response.status_code == 200 and len(response.json()) == 2
    query = sql(session.queries[0])
    assert str(WORKSPACE_ID) in query and str(ADMIN.id) in query and "people" in query
    assert "visibility = 'workspace' OR" in query


def test_sales_can_read_shared_views_but_not_manage_them(client):
    user = sales()
    session = Session([row(user_id=user.id), row(visibility="workspace")])
    result = client.get("/api/v1/saved-views?page=people", headers=setup(session, user))
    assert result.status_code == 200
    assert [v["canManage"] for v in result.json()] == [True, False]


def test_sales_can_save_personal_views_and_owner_is_from_session(client):
    user, session = sales(), Session()
    response = client.post("/api/v1/saved-views", headers=setup(session, user), json=payload(name="  Retail  "))
    assert response.status_code == 200 and response.json()["name"] == "Retail"
    assert session.added[0].user_id == user.id and session.added[0].workspace_id == WORKSPACE_ID
    assert str(user.id) in sql(session.queries[1]) and session.commits == 1


def test_sales_cannot_save_or_delete_shared_views_even_directly(client):
    user, session = sales(), Session(target=row(visibility="workspace"))
    headers = setup(session, user)
    assert client.post("/api/v1/saved-views", headers=headers, json=payload(visibility="workspace")).status_code == 403
    assert not session.queries
    assert client.delete(f"/api/v1/saved-views/{session.target.id}", headers=headers).status_code == 403
    assert not session.deleted and not session.commits


def test_manager_can_update_shared_view_without_reassigning_creator(client):
    manager = CurrentUser(uuid4(), "manager@example.com", "Manager", "management", (WORKSPACE_ID,))
    old = row(visibility="workspace")
    session = Session([old])
    response = client.post("/api/v1/saved-views", headers=setup(session, manager), json=payload(visibility="workspace", filters={"query":"new"}))
    assert response.status_code == 200 and old.filters == {"query":"new"}
    assert old.user_id == ADMIN.id and not session.added


def test_limit_allows_updates_but_refuses_new_views(client):
    session = Session([row(name=f"View {i}") for i in range(20)])
    headers = setup(session)
    assert client.post("/api/v1/saved-views", headers=headers, json=payload()).status_code == 409
    assert client.post("/api/v1/saved-views", headers=headers, json=payload(name="VIEW 0")).status_code == 200


def test_delete_hides_other_users_private_views_and_checks_workspace(client):
    session = Session()
    headers = setup(session)
    target = uuid4()
    assert client.delete(f"/api/v1/saved-views/{target}", headers=headers).status_code == 404
    query = sql(session.queries[0])
    assert str(ADMIN.id) in query and str(WORKSPACE_ID) in query and str(target) in query
    session.target = row()
    assert client.delete(f"/api/v1/saved-views/{session.target.id}", headers=headers).status_code == 204
    assert session.deleted == [session.target] and session.commits == 1


def test_invalid_payload_and_missing_or_forbidden_workspace(client):
    session = Session()
    headers = setup(session)
    for changes in [{"name":" "}, {"visibility":"public"}, {"filters":{"query":3}}, {"page":"../people"}, {"filters":{"q":"x"*2001}}]:
        assert client.post("/api/v1/saved-views", headers=headers, json=payload(**changes)).status_code == 422
    assert client.get("/api/v1/saved-views?page=people").status_code == 400
    setup(session, sales())
    assert client.get("/api/v1/saved-views?page=people", headers={"X-Workspace-Id":str(uuid4())}).status_code == 403
    assert not session.queries
