"""LinkedIn and Instagram through Unipile (services/unipile.py, routers/social.py). Unipile is faked over HTTP."""

import asyncio
import json

import httpx

from config import get_settings
from services.unipile import UnipileClient, notify_token, to_account

DSN = "https://api8.unipile.com:13851"


def client(handler) -> UnipileClient:
    return UnipileClient(DSN, "key-123", transport=httpx.MockTransport(handler))


def test_the_sign_in_link_asks_for_just_that_network() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(path=request.url.path, key=request.headers["X-API-KEY"], body=json.loads(request.content))
        return httpx.Response(200, json={"object": "HostedAuthURL", "url": "https://account.unipile.com/abc"})

    url = asyncio.run(client(handler).sign_in_link("linkedin", "ws:linkedin", "https://api.x/hook?token=t", "https://app.x/config/social"))
    body = seen["body"]
    assert url == "https://account.unipile.com/abc" and seen["path"] == "/api/v1/hosted/accounts/link" and seen["key"] == "key-123"
    assert body["type"] == "create" and body["providers"] == ["LINKEDIN"] and body["api_url"] == DSN and body["name"] == "ws:linkedin"
    assert body["notify_url"] == "https://api.x/hook?token=t" and body["success_redirect_url"] == "https://app.x/config/social"
    assert body["expiresOn"].endswith("Z")


def test_reconnecting_names_the_account() -> None:
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"url": "https://account.unipile.com/r"})

    asyncio.run(client(handler).sign_in_link("instagram", "ws:instagram", None, None, reconnect_account="acc_9"))
    assert seen["type"] == "reconnect" and seen["reconnect_account"] == "acc_9" and seen["providers"] == ["INSTAGRAM"] and "notify_url" not in seen


def test_accounts_are_read_with_their_names_usernames_and_status() -> None:
    items = [
        {"id": "li_1", "type": "LINKEDIN", "name": "Sarah Lane", "connection_params": {"im": {"id": "x", "username": "Sarah L", "publicIdentifier": "sarah-lane"}},
         "sources": [{"id": "s", "status": "OK"}]},
        {"id": "ig_1", "type": "INSTAGRAM", "name": "", "connection_params": {"im": {"id": "y", "username": "aheadtech360"}},
         "sources": [{"id": "s1", "status": "OK"}, {"id": "s2", "status": "CREDENTIALS"}]},
        {"id": "mail_1", "type": "GOOGLE_OAUTH", "name": "sarah@x.com", "connection_params": {}, "sources": []},
    ]
    pages = iter([{"object": "AccountList", "items": items[:2], "cursor": "next"}, {"object": "AccountList", "items": items[2:], "cursor": None}])
    accounts = asyncio.run(client(lambda r: httpx.Response(200, json=next(pages))).accounts())
    assert [(a.id, a.provider, a.name, a.username, a.status) for a in accounts] == [
        ("li_1", "linkedin", "Sarah Lane", "sarah-lane", "ok"),
        ("ig_1", "instagram", "aheadtech360", "aheadtech360", "credentials"),  # one source needs signing in: so does it
    ]
    assert to_account(items[2]) is None  # mail accounts aren't social ones


def test_a_refused_key_says_which_settings_to_check() -> None:
    from services.unipile import UnipileError

    try:
        asyncio.run(client(lambda r: httpx.Response(401, json={})).accounts())
    except UnipileError as exc:
        assert "UNIPILE_API_KEY" in exc.message
    else:
        raise AssertionError("expected UnipileError")


def test_the_unipile_callback_needs_our_token(client, monkeypatch) -> None:  # the API test client (conftest)
    monkeypatch.setattr(get_settings(), "unipile_api_key", "key-123")
    monkeypatch.setattr(get_settings(), "unipile_dsn", DSN)
    body = {"status": "CREATION_SUCCESS", "account_id": "li_1", "name": "00000000-0000-0000-0000-000000000000:linkedin"}
    assert client.post("/api/v1/webhooks/unipile?token=forged", json=body).status_code == 401
    assert notify_token("key-123", body["name"]) != notify_token("other-key", body["name"])


def test_without_unipile_set_up_the_page_says_so(client) -> None:
    from tests.conftest import WORKSPACE_ID

    r = client.post("/api/v1/social-accounts/connect", json={"provider": "linkedin"}, headers={"X-Workspace-Id": str(WORKSPACE_ID)})
    assert r.status_code == 503 and "UNIPILE_DSN" in r.json()["detail"]


def test_an_account_uses_its_own_limits_or_its_networks() -> None:
    from services.own_settings import Settings

    s = Settings(linkedin_daily_invites=20, linkedin_weekly_invites=100, linkedin_daily_messages=50, instagram_daily_messages=30)
    assert s.social_limits("linkedin") == {"invites_per_day": 20, "invites_per_week": 100, "messages_per_day": 50}
    assert s.social_limits("linkedin", 35, 80) == {"invites_per_day": 35, "invites_per_week": 100, "messages_per_day": 80}
    assert s.social_limits("instagram") == {"invites_per_day": 0, "invites_per_week": 0, "messages_per_day": 30}
    assert s.social_limits("instagram", None, 12)["messages_per_day"] == 12
    # Its own daily invites never go past the week's.
    assert Settings(linkedin_weekly_invites=25).social_limits("linkedin", 40)["invites_per_day"] == 25


def test_social_limits_that_contradict_each_other_are_refused(client) -> None:
    from tests.conftest import WORKSPACE_ID

    h = {"X-Workspace-Id": str(WORKSPACE_ID)}
    r = client.put("/api/v1/own-sending/settings", json={"linkedinDailyInvites": 50, "linkedinWeeklyInvites": 30}, headers=h)
    assert r.status_code == 422 and "per week" in r.json()["detail"]
    r = client.put("/api/v1/own-sending/settings", json={"socialMinGapMinutes": 20, "socialMaxGapMinutes": 5}, headers=h)
    assert r.status_code == 422 and "LinkedIn and Instagram" in r.json()["detail"]
    r = client.put("/api/v1/own-sending/settings", json={"instagramDailyMessages": 999}, headers=h)
    assert r.status_code == 422
