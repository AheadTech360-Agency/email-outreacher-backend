"""Unipile: LinkedIn and Instagram accounts, connected through Unipile's hosted sign-in (developer.unipile.com, Hosted
Auth Wizard) and listed from its accounts API. The login stays with Unipile; Coldbrew keeps the account id.

  POST /api/v1/hosted/accounts/link  ->  {"object": "HostedAuthURL", "url": ...}   a one-time sign-in link
  GET  /api/v1/accounts               ->  {"object": "AccountList", "items": [...], "cursor": ...}
  DELETE /api/v1/accounts/{id}                                                     disconnect it

After sign-in Unipile calls our notify_url with {"status": "CREATION_SUCCESS" | "RECONNECTED", "account_id", "name"};
`name` is what we sent, so it carries the workspace, and the URL carries a token only this server can make.
"""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

PROVIDERS = {"linkedin": "LINKEDIN", "instagram": "INSTAGRAM"}
# Unipile's source statuses, as ours: ok, connecting, or needing a person (credentials: sign in again).
STATUSES = {"OK": "ok", "CONNECTING": "connecting", "CREDENTIALS": "credentials", "PERMISSIONS": "permissions", "ERROR": "error", "STOPPED": "stopped"}
LINK_MINUTES = 30
TIMEOUT_SECONDS = 20


class UnipileError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Account:
    id: str
    provider: str  # linkedin | instagram
    name: str
    username: str
    status: str


def notify_token(secret: str, name: str) -> str:
    """The token our notify_url carries for this sign-in, so a call claiming to be Unipile's must know our key."""
    return hmac.new(secret.encode(), f"unipile-notify:{name}".encode(), hashlib.sha256).hexdigest()


def to_account(item: dict[str, Any]) -> Account | None:
    """A LinkedIn or Instagram account from Unipile's list; anything else (mail, WhatsApp...) is None."""
    provider = {v: k for k, v in PROVIDERS.items()}.get(str(item.get("type") or "").upper())
    if provider is None or not item.get("id"):
        return None
    im = (item.get("connection_params") or {}).get("im") or {}
    username = im.get("publicIdentifier") or im.get("username") or ""
    statuses = [STATUSES.get(str(s.get("status") or "").upper(), "error") for s in item.get("sources") or []]
    # The worst source decides: one that needs signing in again makes the account need it.
    order = ["credentials", "permissions", "error", "stopped", "connecting", "ok"]
    status = min(statuses, key=order.index) if statuses else "ok"
    return Account(str(item["id"]), provider, str(item.get("name") or username or ""), str(username), status)


class UnipileClient:
    def __init__(self, dsn: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.dsn = dsn.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.dsn, timeout=TIMEOUT_SECONDS, transport=transport,
                                         headers={"X-API-KEY": api_key, "accept": "application/json"})  # fmt: skip

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise UnipileError(f"Couldn't reach Unipile: {exc.__class__.__name__}") from exc
        if response.status_code in (401, 403):
            raise UnipileError("Unipile refused the API key. Check UNIPILE_API_KEY and UNIPILE_DSN.", response.status_code)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail") or response.json().get("title") or ""
            except ValueError:
                detail = ""
            raise UnipileError(f"Unipile answered {response.status_code}{f': {detail}' if detail else ''}.", response.status_code)
        return response.json() if response.content else {}

    async def sign_in_link(self, provider: str, name: str, notify_url: str | None, return_url: str | None,
                           reconnect_account: str | None = None) -> str:  # fmt: skip
        """A one-time Unipile sign-in page for LinkedIn or Instagram (or to sign an account in again)."""
        body: dict[str, Any] = {
            "type": "reconnect" if reconnect_account else "create",
            "providers": [PROVIDERS[provider]],
            "api_url": self.dsn,
            "expiresOn": (datetime.now(UTC) + timedelta(minutes=LINK_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "name": name,
        }
        if reconnect_account:
            body["reconnect_account"] = reconnect_account
        if notify_url:
            body["notify_url"] = notify_url
        if return_url:
            body["success_redirect_url"] = body["failure_redirect_url"] = return_url
        data = await self._call("POST", "/api/v1/hosted/accounts/link", json=body)
        if not data.get("url"):
            raise UnipileError("Unipile didn't return a sign-in link.")
        return str(data["url"])

    async def accounts(self) -> list[Account]:
        """Every LinkedIn and Instagram account on this Unipile account."""
        out: list[Account] = []
        cursor = None
        for _ in range(20):
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = await self._call("GET", "/api/v1/accounts", params=params)
            out += [a for item in data.get("items") or [] if (a := to_account(item))]
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    async def disconnect(self, account_id: str) -> None:
        await self._call("DELETE", f"/api/v1/accounts/{quote(account_id, safe='')}")
