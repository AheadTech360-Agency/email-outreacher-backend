"""Sign-in and access: tokens signed with a key made here (no Supabase), and fake user storage.
No network, no cost."""

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

import services.auth as auth
from config import get_settings
from dependencies import forget_sign_ins, get_current_user, get_supabase_admin, get_user_repo, get_workspace_repo
from main import app
from services.auth import AuthError, CurrentUser, verify
from services.supabase_admin import SupabaseAdminError
from tests.conftest import WORKSPACE_ID

ISSUER = "https://example.supabase.co/auth/v1"
KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


def token(key=KEY, **claims) -> str:
    now = int(time.time())
    body = {"sub": str(uuid4()), "email": "Ava@Example.com", "aud": "authenticated", "iss": ISSUER, "iat": now, "exp": now + 3600}
    return jwt.encode(body | claims, key, algorithm="ES256", headers={"kid": "k1"})


@pytest.fixture
def jwks(monkeypatch):
    """Supabase's key endpoint, answered with this test's public key."""
    monkeypatch.setattr(auth, "_jwks", lambda url: SimpleNamespace(get_signing_key_from_jwt=lambda t: SimpleNamespace(key=KEY.public_key())))


def run(coro):
    return asyncio.run(coro)


def test_small_clock_differences_are_allowed(jwks) -> None:
    # Issued "2 seconds from now" by a clock slightly ahead of ours: still accepted.
    assert run(verify(token(iat=int(time.time()) + 2), "jwks", ISSUER)).email == "ava@example.com"


def test_valid_token(jwks) -> None:
    sub = uuid4()
    claims = run(verify(token(sub=str(sub)), "jwks", ISSUER))
    assert claims.auth_user_id == sub and claims.email == "ava@example.com"


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (lambda: token(exp=int(time.time()) - 120), "expired"),
        (lambda: token(OTHER_KEY), "isn't valid"),
        (lambda: token(aud="anon"), "isn't valid"),
        (lambda: token(iss="https://other.supabase.co/auth/v1"), "isn't valid"),
        (lambda: token(email=""), "no account email"),
        (lambda: "not-a-token", "isn't valid"),
    ],
)
def test_bad_tokens(jwks, bad, message) -> None:
    with pytest.raises(AuthError, match=message):
        run(verify(bad(), "jwks", ISSUER))


# ---------- Users and access through the API ----------


class FakeUsers:
    def __init__(self) -> None:
        self.rows: dict[UUID, SimpleNamespace] = {}
        self.ws: dict[UUID, list[UUID]] = {}
        self.fail_create = False

    def add(self, email, role="sales", status="active", auth_user_id=None, workspaces=()):
        row = SimpleNamespace(id=uuid4(), auth_user_id=auth_user_id, email=email, name=email.split("@")[0], role=role,
                              status=status, last_active_at=None, created_at=datetime.now(UTC), permissions={})  # fmt: skip
        self.rows[row.id] = row
        self.ws[row.id] = list(workspaces)
        return row

    async def for_sign_in(self, auth_user_id, email):
        row = next((r for r in self.rows.values() if r.auth_user_id == auth_user_id), None)
        if row is None:
            row = next((r for r in self.rows.values() if r.email == email), None)
            if row is None or row.auth_user_id is not None:
                return None
            row.auth_user_id = auth_user_id
        if row.status == "invited":
            row.status = "active"
        return row

    async def workspace_ids(self, user_id):
        return self.ws.get(user_id, [])

    async def members(self):
        return self.ws

    async def list(self):
        return list(self.rows.values())

    async def get(self, user_id):
        return self.rows.get(user_id)

    async def by_email(self, email):
        return next((r for r in self.rows.values() if r.email == email), None)

    async def create(self, name, email, role, workspace_ids, auth_user_id=None, permissions=None):
        if self.fail_create:
            raise RuntimeError("database down")
        row = self.add(email, role, "invited", auth_user_id=auth_user_id, workspaces=workspace_ids)
        row.name, row.permissions = name, permissions or {}
        return row

    async def update(self, user, values, workspace_ids):
        for k, v in values.items():
            setattr(user, k, v)
        if workspace_ids is not None:
            self.ws[user.id] = workspace_ids
        return user

    async def delete(self, user):
        self.rows.pop(user.id)


class FakeWorkspaces:
    def __init__(self) -> None:
        self.rows = [SimpleNamespace(id=WORKSPACE_ID, name="AheadTech360", description="", created_at=datetime.now(UTC)),
                     SimpleNamespace(id=uuid4(), name="Other", description="", created_at=datetime.now(UTC))]  # fmt: skip

    async def list(self):
        return self.rows


class FakeSupabaseAdmin:
    """Supabase Auth's admin API: the sign-ins that exist, by email."""

    def __init__(self) -> None:
        self.accounts: dict[str, UUID] = {}
        self.passwords: dict[str, str] = {}
        self.deleted: list[UUID] = []
        self.error: SupabaseAdminError | None = None

    async def create_user(self, email, password):
        if self.error:
            raise self.error
        if email in self.accounts:
            return None
        self.accounts[email], self.passwords[email] = uuid4(), password
        return self.accounts[email]

    async def delete_user(self, user_id):
        self.deleted.append(user_id)


@pytest.fixture
def signed(jwks, monkeypatch):
    """A client that signs in for real: the token is checked, the user looked up in FakeUsers."""
    monkeypatch.setattr(get_settings(), "supabase_url", "https://example.supabase.co")
    users, workspaces = FakeUsers(), FakeWorkspaces()
    app.dependency_overrides[get_user_repo] = lambda: users
    app.dependency_overrides[get_workspace_repo] = lambda: workspaces
    supabase = FakeSupabaseAdmin()
    app.dependency_overrides[get_supabase_admin] = lambda: supabase
    app.dependency_overrides.pop(get_current_user, None)
    forget_sign_ins()
    yield SimpleNamespace(client=TestClient(app), users=users, workspaces=workspaces, supabase=supabase)
    app.dependency_overrides.clear()
    forget_sign_ins()


def as_(user, **headers):
    return {"Authorization": f"Bearer {token(sub=str(user.auth_user_id), email=user.email)}", **headers}


def test_requests_need_a_session(signed) -> None:
    c = signed.client
    assert c.get("/api/v1/health").status_code == 200  # the one open route
    assert c.get("/api/v1/workspaces").json()["detail"] == "Sign in to continue."
    assert c.get("/api/v1/workspaces", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert c.get("/api/v1/workspaces", headers={"Authorization": f"Bearer {token(OTHER_KEY)}"}).status_code == 401


def test_an_account_nobody_added_is_refused(signed) -> None:
    """Supabase sign-in is shared with Lead Intel: an account there (or one made by signing up) isn't a Coldbrew user."""
    newcomer = SimpleNamespace(auth_user_id=uuid4(), email="new@example.com")
    for path in ("/api/v1/me", "/api/v1/workspaces", "/api/v1/icps"):
        r = signed.client.get(path, headers=as_(newcomer))
        assert r.status_code == 403 and "hasn't been added" in r.json()["detail"]
    assert all(r.email != "new@example.com" for r in signed.users.rows.values())  # nothing was created for it


def test_an_added_email_cant_be_taken_by_a_second_account(signed) -> None:
    row = signed.users.add("ava@example.com", auth_user_id=uuid4())
    other = SimpleNamespace(auth_user_id=uuid4(), email=row.email)
    assert signed.client.get("/api/v1/me", headers=as_(other)).status_code == 403


def test_added_by_email_links_on_first_sign_in(signed) -> None:
    row = signed.users.add("research@aheadtech360.com", role="admin", status="invited")
    auth_id = uuid4()
    me = signed.client.get("/api/v1/me", headers=as_(SimpleNamespace(auth_user_id=auth_id, email=row.email))).json()
    assert me["id"] == str(row.id) and me["role"] == "admin" and me["status"] == "active" and row.auth_user_id == auth_id
    # Admins open every workspace.
    assert len(signed.client.get("/api/v1/workspaces", headers=as_(row)).json()) == 2


def test_members_see_only_their_workspaces_and_deactivated_are_refused(signed) -> None:
    member = signed.users.add("sam@example.com", auth_user_id=uuid4(), workspaces=[WORKSPACE_ID])
    assert [w["name"] for w in signed.client.get("/api/v1/workspaces", headers=as_(member)).json()] == ["AheadTech360"]
    member.status = "deactivated"
    forget_sign_ins()  # what the admin's change does (see the sign-in cache test)
    r = signed.client.get("/api/v1/workspaces", headers=as_(member))
    assert r.status_code == 403 and "deactivated" in r.json()["detail"]


def test_admins_manage_users_with_guards(signed) -> None:
    admin = signed.users.add("boss@example.com", role="admin", auth_user_id=uuid4())
    c, h = signed.client, as_(admin)
    added = c.post("/api/v1/users", headers=h, json={"name": "Ava", "email": "Ava@Example.com", "role": "management",
                                                     "workspaceIds": [str(WORKSPACE_ID)], "password": PASSWORD})  # fmt: skip
    assert added.status_code == 201 and added.json()["email"] == "ava@example.com" and added.json()["status"] == "invited"
    assert c.post("/api/v1/users", headers=h, json={"email": "ava@example.com", "password": PASSWORD}).status_code == 409
    assert c.post("/api/v1/users", headers=h, json={"email": "not an email", "password": PASSWORD}).status_code == 422
    ava = added.json()["id"]
    moved = c.patch(f"/api/v1/users/{ava}", headers=h, json={"role": "sales", "workspaceIds": []}).json()
    assert (moved["role"], moved["workspaceIds"]) == ("sales", [])
    # You can't demote or deactivate yourself, and the last admin can't be lost.
    assert c.patch(f"/api/v1/users/{admin.id}", headers=h, json={"role": "sales"}).status_code == 409
    assert c.patch(f"/api/v1/users/{admin.id}", headers=h, json={"name": "The Boss"}).json()["name"] == "The Boss"
    assert c.delete(f"/api/v1/users/{admin.id}", headers=h).status_code == 409
    assert c.delete(f"/api/v1/users/{ava}", headers=h).status_code == 204
    assert [u["email"] for u in c.get("/api/v1/users", headers=h).json()] == ["boss@example.com"]


def test_only_admins_change_workspaces_and_management_changes_icps(signed) -> None:
    sales = signed.users.add("s@example.com", auth_user_id=uuid4(), workspaces=[WORKSPACE_ID])
    assert signed.client.post("/api/v1/workspaces", headers=as_(sales), json={"name": "Mine"}).status_code == 403
    assert signed.client.put(f"/api/v1/workspaces/{WORKSPACE_ID}/members", headers=as_(sales), json={"userIds": []}).status_code == 403
    icp = {"name": "X", "industry": "Y"}
    assert signed.client.post("/api/v1/icps", headers=as_(sales), json=icp).json()["detail"].startswith("Only Management and admins can do that")


def test_current_user_can_open() -> None:
    sales = CurrentUser(uuid4(), "s@x.com", "S", "sales", [WORKSPACE_ID])
    assert sales.can_open(WORKSPACE_ID) and not sales.can_open(uuid4())
    assert CurrentUser(uuid4(), "a@x.com", "A", "admin").can_open(uuid4())


def test_a_sign_in_is_looked_up_once_a_minute_and_again_after_an_admin_change(signed, monkeypatch) -> None:
    ava = signed.users.add("ava@example.com", auth_user_id=uuid4(), workspaces=[WORKSPACE_ID])
    lookups = []
    real = signed.users.for_sign_in

    async def counted(*args):
        lookups.append(args)
        return await real(*args)

    monkeypatch.setattr(signed.users, "for_sign_in", counted)
    for _ in range(3):
        assert signed.client.get("/api/v1/me", headers=as_(ava)).status_code == 200
    assert len(lookups) == 1
    admin = signed.users.add("boss@example.com", role="admin", auth_user_id=uuid4())
    r = signed.client.patch(f"/api/v1/users/{ava.id}", headers=as_(admin), json={"status": "deactivated"})
    assert r.status_code == 200
    assert signed.client.get("/api/v1/me", headers=as_(ava)).status_code == 403  # refused straight away


# ---------- Adding someone makes their sign-in ----------

PASSWORD = "correct-horse-battery"


def test_adding_someone_makes_their_sign_in_and_they_can_sign_in(signed) -> None:
    admin = signed.users.add("boss@example.com", role="admin", auth_user_id=uuid4())
    r = signed.client.post("/api/v1/users", headers=as_(admin),
                           json={"name": "Ava", "email": "ava@example.com", "role": "sales", "workspaceIds": [str(WORKSPACE_ID)], "password": PASSWORD})  # fmt: skip
    assert r.status_code == 201 and r.json()["signIn"] == "created"
    assert signed.supabase.passwords["ava@example.com"] == PASSWORD
    # Their first sign-in is already linked to the new account.
    ava = SimpleNamespace(auth_user_id=signed.supabase.accounts["ava@example.com"], email="ava@example.com")
    me = signed.client.get("/api/v1/me", headers=as_(ava)).json()
    assert me["id"] == r.json()["id"] and me["status"] == "active"


def test_someone_with_a_lead_intel_sign_in_keeps_it(signed) -> None:
    admin = signed.users.add("boss@example.com", role="admin", auth_user_id=uuid4())
    signed.supabase.accounts["li@example.com"] = existing = uuid4()
    r = signed.client.post("/api/v1/users", headers=as_(admin), json={"name": "Li", "email": "li@example.com", "role": "admin", "password": PASSWORD})
    assert r.status_code == 201 and r.json()["signIn"] == "existing"
    assert "li@example.com" not in signed.supabase.passwords  # their password wasn't touched
    li = SimpleNamespace(auth_user_id=existing, email="li@example.com")
    assert signed.client.get("/api/v1/me", headers=as_(li)).json()["id"] == r.json()["id"]  # linked by email


def test_nothing_is_saved_when_supabase_refuses(signed) -> None:
    admin = signed.users.add("boss@example.com", role="admin", auth_user_id=uuid4())
    signed.supabase.error = SupabaseAdminError("Supabase couldn't make the account: Password is too weak", 422)
    r = signed.client.post("/api/v1/users", headers=as_(admin), json={"name": "Ava", "email": "ava@example.com", "password": PASSWORD})
    assert r.status_code == 422 and "too weak" in r.json()["detail"]
    assert [u.email for u in signed.users.rows.values()] == ["boss@example.com"]


def test_a_new_sign_in_is_removed_again_if_saving_the_user_fails(signed) -> None:
    admin = signed.users.add("boss@example.com", role="admin", auth_user_id=uuid4())
    signed.users.fail_create = True
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/v1/users", headers=as_(admin), json={"name": "Ava", "email": "ava@example.com", "password": PASSWORD})
    assert r.status_code == 500 and signed.supabase.deleted == [signed.supabase.accounts["ava@example.com"]]


def test_passwords_must_be_long_and_only_admins_add_people(signed) -> None:
    admin = signed.users.add("boss@example.com", role="admin", auth_user_id=uuid4())
    short = signed.client.post("/api/v1/users", headers=as_(admin), json={"email": "ava@example.com", "password": "short"})
    assert short.status_code == 422 and signed.supabase.accounts == {}
    sales = signed.users.add("s@example.com", auth_user_id=uuid4(), workspaces=[WORKSPACE_ID])
    assert signed.client.post("/api/v1/users", headers=as_(sales), json={"email": "x@example.com", "password": PASSWORD}).status_code == 403
