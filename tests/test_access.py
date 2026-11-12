"""What a Prospector may see and change (services/access.py): their own ICPs, products and smart lists and those an
admin shared with them (to use, or to edit); the companies their runs found and those in their lists, and the
people there. Everyone else sees the whole workspace, as before."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from dependencies import get_current_user, get_icp_campaign_repo, get_lead_intel_icp_writer, get_optional_sequencer, get_product_repo, get_prompt_repo, get_smart_list_repo, get_user_repo
from main import app
from services.access import Scope
from services.auth import CurrentUser
from tests.conftest import ICP_ID, WORKSPACE_ID

H = {"X-Workspace-Id": str(WORKSPACE_ID)}


@pytest.fixture
def me(client) -> CurrentUser:
    user = CurrentUser(uuid4(), "p@example.com", "Pat", "prospector", [WORKSPACE_ID])
    app.dependency_overrides[get_current_user] = lambda: user
    # Saving an ICP's profile reads prompts and products: none here (a Prospector's prompts are kept as they are).
    app.dependency_overrides[get_prompt_repo] = lambda: SimpleNamespace(get=_verify_prompt)
    app.dependency_overrides[get_product_repo] = lambda: SimpleNamespace(get=_no_product)
    # Archiving is refused before Lead Intel or CheapSequencer would be reached.
    app.dependency_overrides[get_lead_intel_icp_writer] = lambda: SimpleNamespace()
    app.dependency_overrides[get_icp_campaign_repo] = lambda: SimpleNamespace()
    app.dependency_overrides[get_optional_sequencer] = lambda: None
    return user


async def _no_product(workspace_id, product_id):
    return None


async def _verify_prompt(workspace_id, prompt_id):
    return SimpleNamespace(kind="verify")


def share(access, user, kind, resource_id, *, edit=False, owner=False):
    access.items.append(SimpleNamespace(user_id=user.id, workspace_id=WORKSPACE_ID, kind=kind, resource_id=resource_id,
                                        can_edit=edit or owner, is_owner=owner, granted_by=None))  # fmt: skip


# ---------- ICPs ----------


def test_a_prospector_sees_only_the_icps_shared_with_them_and_whether_they_may_edit(client, me, access) -> None:
    assert client.get("/api/v1/icps", headers=H).json() == []
    assert client.get(f"/api/v1/icps/{ICP_ID}", headers=H).status_code == 404
    share(access, me, "icp", ICP_ID)
    [icp] = client.get("/api/v1/icps", headers=H).json()
    assert icp["id"] == str(ICP_ID) and icp["canEdit"] is False
    assert client.get(f"/api/v1/icps/{ICP_ID}", headers=H).json()["canEdit"] is False


def test_a_shared_icp_is_changed_only_when_shared_to_edit_and_archived_only_by_its_creator(client, me, access) -> None:
    share(access, me, "icp", ICP_ID)
    assert client.put(f"/api/v1/icps/{ICP_ID}/profile", headers=H, json={}).status_code == 403
    assert client.post(f"/api/v1/icps/{ICP_ID}/archive", headers=H).status_code == 403
    access.items[0].can_edit = True
    assert client.put(f"/api/v1/icps/{ICP_ID}/profile", headers=H, json={}).status_code == 200
    assert client.post(f"/api/v1/icps/{ICP_ID}/archive", headers=H).status_code == 403  # editing isn't owning
    # An ICP's product must be one they can see.
    other = uuid4()
    r = client.put(f"/api/v1/icps/{ICP_ID}/profile", headers=H, json={"productId": str(other)})
    assert r.status_code == 404 and r.json()["detail"] == "Product not found"


def test_a_prospectors_icp_profile_keeps_its_prompts(client, me, access, profiles) -> None:
    share(access, me, "icp", ICP_ID, edit=True)
    prompt = uuid4()
    profiles.saved[(WORKSPACE_ID, ICP_ID)] = SimpleNamespace(verify_prompt_id=prompt, hook_prompt_id=None, email_prompt_id=None)
    saved = []
    real = profiles.upsert

    async def upsert(workspace_id, icp_id, values):
        saved.append(values)
        return await real(workspace_id, icp_id, values)

    profiles.upsert = upsert
    client.put(f"/api/v1/icps/{ICP_ID}/profile", headers=H, json={"verifyPromptId": str(uuid4()), "titles": ["Owner"]})
    assert saved and saved[0]["verify_prompt_id"] == prompt and saved[0]["titles"] == ["Owner"]


# ---------- Products ----------


class FakeProducts:
    def __init__(self) -> None:
        self.rows: dict = {}

    def add(self, name: str):
        now = datetime.now(UTC)
        p = SimpleNamespace(id=uuid4(), workspace_id=WORKSPACE_ID, name=name, description="", value="", proof="", offer="", cta="", url="",
                            custom_fields=[], notes=[], archived=False, created_at=now, updated_at=now)  # fmt: skip
        self.rows[p.id] = p
        return p

    async def workspace_exists(self, workspace_id):
        return True

    async def list(self, workspace_id):
        return list(self.rows.values())

    async def get(self, workspace_id, product_id):
        return self.rows.get(product_id)

    async def name_taken(self, workspace_id, name, except_id=None):
        return any(p.name == name and p.id != except_id for p in self.rows.values())

    async def create(self, workspace_id, values):
        p = self.add(values["name"])
        return p

    async def update(self, product, values):
        product.name = values["name"]
        return product

    async def delete(self, product):
        self.rows.pop(product.id)

    async def used_by(self, workspace_id, product_id):
        return []


@pytest.fixture
def products():
    repo = FakeProducts()
    app.dependency_overrides[get_product_repo] = lambda: repo
    return repo


def test_products_a_prospector_sees_and_changes(client, me, access, products) -> None:
    hidden, shared, editable = products.add("Hidden"), products.add("Shared"), products.add("Editable")
    share(access, me, "product", shared.id)
    share(access, me, "product", editable.id, edit=True)
    listed = {p["name"]: p["canEdit"] for p in client.get("/api/v1/products", headers=H).json()}
    assert listed == {"Shared": False, "Editable": True}
    body = {"name": "Renamed"}
    assert client.put(f"/api/v1/products/{hidden.id}", headers=H, json=body).status_code == 404
    assert client.put(f"/api/v1/products/{shared.id}", headers=H, json=body).status_code == 403
    assert client.put(f"/api/v1/products/{editable.id}", headers=H, json=body).status_code == 200
    assert client.delete(f"/api/v1/products/{editable.id}", headers=H).status_code == 403  # only its creator deletes it


def test_a_product_a_prospector_creates_is_theirs(client, me, access, products) -> None:
    made = client.post("/api/v1/products", headers=H, json={"name": "Mine"}).json()
    row = next(a for a in access.items if a.kind == "product")
    assert (row.user_id, str(row.resource_id), row.is_owner) == (me.id, made["id"], True)
    assert client.delete(f"/api/v1/products/{made['id']}", headers=H).status_code == 204
    assert ("product", row.resource_id) in access.forgotten


# ---------- Smart lists ----------


class FakeLists:
    def __init__(self) -> None:
        self.rows: dict = {}
        self.moved: list = []
        self.held: dict = {}
        self.session = None

    def add(self, name: str):
        lst = SimpleNamespace(id=uuid4(), workspace_id=WORKSPACE_ID, kind="company", name=name, name_key=name.lower(), icp_draft=None,
                              lead_intel_icp_id=None, created_at=datetime.now(UTC), updated_at=datetime.now(UTC))  # fmt: skip
        self.rows[lst.id] = lst
        return lst

    async def lists(self, workspace_id, kind, only=None):
        return [(lst, 0) for lst in self.rows.values() if only is None or lst.id in only]

    async def count_unsorted(self, workspace_id, kind, scope=None):
        return 0

    async def get(self, workspace_id, list_id):
        return self.rows.get(list_id)

    async def get_or_create(self, workspace_id, kind, name, key):
        return next((lst for lst in self.rows.values() if lst.name_key == key), None) or self.add(name)

    async def memberships(self, workspace_id, kind):
        return self.held

    async def move(self, workspace_id, kind, ids, list_id):
        self.moved.append((ids, list_id))
        return len(ids)


@pytest.fixture
def lists(monkeypatch):
    repo = FakeLists()
    app.dependency_overrides[get_smart_list_repo] = lambda: repo

    async def visible(self, session, kind, ids):
        return [i for i in ids if i in VISIBLE]

    monkeypatch.setattr(Scope, "visible_members", visible)
    return repo


VISIBLE: set = set()


def test_a_prospector_sees_only_their_lists(client, me, access, lists) -> None:
    hidden, shared = lists.add("Hidden"), lists.add("Shared")
    share(access, me, "smart_list", shared.id)
    listed = client.get("/api/v1/smart-lists?kind=company", headers=H).json()["lists"]
    assert [(x["name"], x["canEdit"]) for x in listed] == [("Shared", False)]
    assert client.get(f"/api/v1/smart-lists/{hidden.id}", headers=H).status_code == 404
    assert client.patch(f"/api/v1/smart-lists/{shared.id}", headers=H, json={"name": "X"}).status_code == 403
    assert client.delete(f"/api/v1/smart-lists/{shared.id}", headers=H).status_code == 403


def test_moving_only_what_they_see_into_a_list_they_may_edit_and_out_of_none_they_cant(client, me, access, lists) -> None:
    hidden, shared, mine = lists.add("Hidden"), lists.add("Shared"), lists.add("Mine")
    share(access, me, "smart_list", shared.id)
    share(access, me, "smart_list", mine.id, owner=True)
    seen, unseen, in_shared = uuid4(), uuid4(), uuid4()
    VISIBLE.clear()
    VISIBLE.update({seen, in_shared})
    lists.held = {in_shared: shared.id}
    move = lambda to, ids: client.post("/api/v1/smart-lists/move", headers=H, json={"kind": "company", "toListId": str(to), "memberIds": [str(i) for i in ids]})  # noqa: E731
    assert move(hidden.id, [seen]).status_code == 404
    assert move(shared.id, [seen]).status_code == 403
    assert move(mine.id, [seen, unseen, in_shared]).status_code == 200
    assert lists.moved == [([seen], mine.id)]  # not one they can't see, nor one in a list they can't edit
    # A new list by a name someone else's list has: refused, not merged into it.
    r = client.post("/api/v1/smart-lists/move", headers=H, json={"kind": "company", "newListName": "Hidden", "memberIds": [str(seen)]})
    assert r.status_code == 409
    r = client.post("/api/v1/smart-lists/move", headers=H, json={"kind": "company", "newListName": "Fresh", "memberIds": [str(seen)]})
    assert r.status_code == 200 and any(a.kind == "smart_list" and a.is_owner and a.resource_id == r.json()["toListId"] or
                                        str(a.resource_id) == r.json()["toListId"] for a in access.items)  # fmt: skip


# ---------- Companies and people ----------


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect()))


def test_companies_and_people_a_prospector_sees(access) -> None:
    me = CurrentUser(uuid4(), "p@example.com", "Pat", "prospector", [WORKSPACE_ID])
    scope = Scope(me, WORKSPACE_ID, access)
    companies = _sql(scope.companies())
    assert "eo_quickstart_runs.created_by" in companies and "unnest(" in companies and "eo_access.kind" in companies
    people = _sql(scope.people())
    assert "eo_stakeholders.company_id" in people and "eo_stakeholders.id IN" in people
    # Everyone else: no limit at all.
    assert Scope(CurrentUser(uuid4(), "m@example.com", "M", "management", [WORKSPACE_ID]), WORKSPACE_ID, access).companies() is None


def test_a_prospector_reads_posts_only_for_a_person(client, me) -> None:
    assert client.get("/api/v1/posts", headers=H).status_code == 422


# ---------- Sharing (admins) ----------


class FakeUsers:
    def __init__(self, user) -> None:
        self.user = user

    async def get(self, user_id):
        return self.user if user_id == self.user.id else None

    async def workspace_ids(self, user_id):
        return [WORKSPACE_ID]


def test_admins_share_items_with_a_prospector_and_their_own_stay_theirs(client, access, products) -> None:
    pat = SimpleNamespace(id=uuid4(), role="prospector")
    app.dependency_overrides[get_user_repo] = lambda: FakeUsers(pat)
    mine = uuid4()
    share(access, pat, "product", mine, owner=True)
    product = products.add("Shared")
    body = {"grants": [{"kind": "icp", "resourceId": str(ICP_ID), "workspaceId": str(WORKSPACE_ID), "canEdit": True},
                       {"kind": "product", "resourceId": str(product.id), "workspaceId": str(WORKSPACE_ID)}]}  # fmt: skip
    out = client.put(f"/api/v1/users/{pat.id}/access", json=body)
    assert out.status_code == 200, out.text
    got = {(g["kind"], g["resourceId"]): (g["canEdit"], g["isOwner"]) for g in out.json()}
    assert got == {("product", str(mine)): (True, True), ("icp", str(ICP_ID)): (True, False), ("product", str(product.id)): (False, False)}
    # Sharing again replaces what was shared, never what they made.
    assert len(client.put(f"/api/v1/users/{pat.id}/access", json={"grants": []}).json()) == 1
    # Only in a workspace they're in, and only things that exist.
    other_ws = {"grants": [{"kind": "icp", "resourceId": str(ICP_ID), "workspaceId": str(uuid4())}]}
    assert client.put(f"/api/v1/users/{pat.id}/access", json=other_ws).status_code == 422
    missing = {"grants": [{"kind": "product", "resourceId": str(uuid4()), "workspaceId": str(WORKSPACE_ID)}]}
    assert client.put(f"/api/v1/users/{pat.id}/access", json=missing).status_code == 422
    pat.role = "sales"
    assert client.put(f"/api/v1/users/{pat.id}/access", json={"grants": []}).status_code == 422


def test_only_admins_share(client, me) -> None:
    assert client.get(f"/api/v1/users/{me.id}/access").status_code == 403
