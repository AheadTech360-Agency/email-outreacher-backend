"""CheapSequencer (sending) and CheapInboxes (mailboxes and domains).

CheapSequencer sends the campaigns through 16 Google mailboxes and keeps them warmed up; CheapInboxes
sells and runs those mailboxes and their domains. CheapInboxes is only read. CheapSequencer is read, and
written in exactly these ways: lists, CSV imports of leads (with each lead's emails in custom fields),
enrolling a list into a campaign, the step text and name of a draft or paused campaign, a draft copy
of a campaign for a new ICP, and archiving the campaign of an archived or deleted ICP once it's empty. Nothing here can
activate a campaign or send an email: that stays a person's decision in CheapSequencer.

Docs: https://docs.cheapsequencer.com/llms.txt and https://api.cheapinboxes.com/v1/openapi.json.
"""

import asyncio
import csv
import io
from typing import Any
from urllib.parse import quote

import httpx

SEQUENCER_URL = "https://api.cheapsequencer.com/v1"
INBOXES_URL = "https://api.cheapinboxes.com"


# How long to wait before trying a read again after the connection couldn't be made.
RETRY_AFTER_SECONDS = 1.0

class SendingError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _message(service: str, response: httpx.Response) -> str:
    try:
        body = response.json()
        detail = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("message")
    except ValueError:
        detail = None
    if response.status_code in (401, 403):
        return f"{service} rejected the API key. Check it in the backend's .env."
    if response.status_code == 429:
        return f"{service}'s rate limit was reached. Try again in a minute."
    return f"{service} answered {response.status_code}: {detail or 'no details'}"


class _Client:
    service = ""

    def __init__(self, base_url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._client = httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _read(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """A GET, tried once more when the connection couldn't be made at all (a network blip): nothing
        reached the service, so trying again can't do anything twice. Writes are never retried here."""
        for attempt in range(2):
            try:
                return await self._client.get(path, params=params or None)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt:
                    raise SendingError(f"Couldn't reach {self.service}: {exc.__class__.__name__}") from exc
                await asyncio.sleep(RETRY_AFTER_SECONDS)
            except httpx.HTTPError as exc:
                raise SendingError(f"Couldn't reach {self.service}: {exc.__class__.__name__}") from exc
        raise AssertionError("unreachable")

    async def get(self, path: str, **params: Any) -> Any:
        response = await self._read(path, params)
        if response.status_code >= 400:
            raise SendingError(_message(self.service, response), response.status_code)
        return response.json()


class SequencerClient(_Client):
    service = "CheapSequencer"

    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None, base_url: str = SEQUENCER_URL) -> None:
        super().__init__(base_url, api_key, transport)

    async def all(self, path: str) -> list[dict[str, Any]]:
        """Every row of a list endpoint, following its cursor."""
        rows: list[dict[str, Any]] = []
        cursor = None
        for _ in range(50):
            body = await self.get(path, limit=100, **({"cursor": cursor} if cursor else {}))
            rows += body.get("data") or []
            meta = body.get("meta") or {}
            cursor = meta.get("next_cursor")
            if not meta.get("has_more") or not cursor:
                break
        return rows

    async def warmup_inboxes(self) -> list[dict[str, Any]]:
        """Each mailbox with its limits, warmup state and today's sends and placements."""
        return await self.all("/warmup/inboxes")

    async def warmup(self) -> dict[str, Any]:
        return (await self.get("/warmup")).get("data") or {}

    async def usage(self) -> dict[str, Any]:
        return (await self.get("/usage")).get("data") or {}

    async def campaigns(self) -> list[dict[str, Any]]:
        return await self.all("/campaigns")

    async def campaign(self, campaign_id: str) -> tuple[dict[str, Any], str]:
        """A campaign with its steps, and its ETag for an update."""
        response = await self._read(f"/campaigns/{campaign_id}")
        if response.status_code >= 400:
            raise SendingError(_message(self.service, response), response.status_code)
        body = response.json().get("data") or {}
        return body, response.headers.get("ETag") or body.get("etag") or ""

    async def enrollments(self, campaign_id: str) -> list[dict[str, Any]]:
        """Each lead's place in the campaign: status, sent_count, last_sent_at, replied_at..."""
        return await self.all(f"/campaigns/{campaign_id}/enrollments")

    async def inboxes(self) -> list[dict[str, Any]]:
        """The sending mailboxes (id, email, display_name...)."""
        return await self.all("/inboxes")

    async def threads(self, campaign_id: str) -> list[dict[str, Any]]:
        """A campaign's conversations in the unified inbox, newest first (WarmUp mail left out)."""
        rows: list[dict[str, Any]] = []
        cursor = None
        for _ in range(10):
            body = await self.get("/inbox/threads", sequence_id=campaign_id, include_warmup="false", limit=100,
                                  **({"cursor": cursor} if cursor else {}))  # fmt: skip
            rows += body.get("data") or []
            meta = body.get("meta") or {}
            cursor = meta.get("next_cursor")
            if not meta.get("has_more") or not cursor:
                break
        return rows

    async def thread(self, thread_id: str) -> dict[str, Any]:
        """One conversation and its messages. Reading it doesn't mark it read."""
        body = await self.get(f"/inbox/threads/{quote(thread_id, safe='')}")
        return body.get("data") or body

    # ---- Writes: never activate, never send. ----

    async def _write(self, method: str, path: str, key: str, body: Any = None, etag: str | None = None) -> dict[str, Any]:
        headers = {"Idempotency-Key": key[:255]}
        if etag:
            headers["If-Match"] = etag
        try:
            response = await self._client.request(method, path, json=body if body is not None else {}, headers=headers)
        except httpx.HTTPError as exc:
            raise SendingError(f"Couldn't reach {self.service}: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise SendingError(_message(self.service, response), response.status_code)
        return response.json().get("data") or {}

    async def _wait(self, action: dict[str, Any], what: str, seconds: int = 180) -> dict[str, Any]:
        """Poll a queued command until it finishes."""
        for _ in range(seconds // 2):
            if action.get("status") in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(action.get("retry_after_seconds") or 2)
            action = (await self.get(f"/api-actions/{action['id']}")).get("data") or action
        if action.get("status") != "completed":
            error = action.get("error") or {}
            raise SendingError(f"{what} didn't finish in CheapSequencer ({action.get('status')}): {error.get('message') or 'no details'}")
        return action

    async def create_list(self, name: str, key: str) -> str:
        created = await self._write("POST", "/lists", key, {"name": name[:120]})
        return (created.get("list") or created)["id"]

    async def import_rows(self, list_id: str, rows: list[dict[str, str]], key: str) -> dict[str, Any]:
        """Upsert leads by email from rows of lead fields (built-in names or custom field tags), into a
        list. Re-importing a lead updates it, which is how a follow-up's text is written."""
        if not rows:
            return {}
        columns = list(dict.fromkeys(c for row in rows for c in row))
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        data = out.getvalue().encode()
        up = await self._write("POST", "/imports/upload-url", f"{key}-url",
                               {"list_id": list_id, "filename": "coldbrew.csv", "content_type": "text/csv", "size_bytes": len(data)})  # fmt: skip
        upload, import_id = up["upload"], up["import"]["id"]
        try:
            # The signed upload URL is its own credential, on a storage host that isn't CheapSequencer: a
            # client with no default headers, so our API key never goes there (the host also refuses it).
            async with httpx.AsyncClient(timeout=60, transport=self._transport) as storage:
                put = await storage.request(upload.get("method", "PUT"), upload["url"], content=data,
                                            headers=up.get("headers") or {"Content-Type": "text/csv"})  # fmt: skip
        except httpx.HTTPError as exc:
            raise SendingError(f"Couldn't upload the CSV to CheapSequencer: {exc.__class__.__name__}") from exc
        if put.status_code >= 400:
            raise SendingError(f"CheapSequencer's upload answered {put.status_code}.")
        await self._write("POST", f"/imports/{import_id}/mapping-suggestion", f"{key}-suggest")
        await self._write("POST", f"/imports/{import_id}/mapping", f"{key}-map",
                          {"mapping": {c: c for c in columns}, "has_header": True, "delimiter": ","})  # fmt: skip
        processed = await self._write("POST", f"/imports/{import_id}/process", f"{key}-process")
        await self._wait(processed.get("action") or processed, "The import")
        return (await self.get(f"/imports/{import_id}")).get("data") or {}

    async def enroll(self, campaign_id: str, list_id: str, key: str) -> None:
        """Enroll a list's leads into a campaign. A draft or paused campaign doesn't send them."""
        queued = await self._write("POST", f"/campaigns/{campaign_id}/enrollments", key, {"list_ids": [list_id]})
        await self._wait(queued.get("action") or queued, "Adding the leads to the campaign")

    async def duplicate(self, campaign_id: str, key: str) -> dict[str, Any]:
        """A draft copy of a campaign (steps, settings and mailboxes, no leads). The same key never makes a second copy."""
        copied = await self._write("POST", f"/campaigns/{campaign_id}/duplicate", key)
        return copied.get("sequence") or copied.get("campaign") or copied  # answered as {"sequence": {...}, "subsequences": [...]}

    async def rename(self, campaign_id: str, name: str, etag: str, key: str) -> dict[str, Any]:
        """Rename a draft or paused campaign."""
        return await self._write("PATCH", f"/campaigns/{campaign_id}", key, {"name": name[:200]}, etag)

    async def set_daily_cap(self, campaign_id: str, cap: int, etag: str, key: str) -> dict[str, Any]:
        """The most a draft or paused campaign sends a day, across its mailboxes."""
        return await self._write("PATCH", f"/campaigns/{campaign_id}", key, {"daily_send_cap": cap}, etag)

    async def archive(self, campaign_id: str, etag: str, key: str) -> None:
        """Archive a campaign (CheapSequencer keeps its history). Only called once no lead is left in it."""
        await self._write("DELETE", f"/campaigns/{campaign_id}", key, None, etag)

    async def set_steps(self, campaign_id: str, steps: list[dict[str, Any]], etag: str, key: str) -> dict[str, Any]:
        """Replace a draft or paused campaign's step copy (ids echoed, so the steps are kept)."""
        return await self._write("PATCH", f"/campaigns/{campaign_id}", key, {"steps": steps}, etag)


class InboxesClient(_Client):
    service = "CheapInboxes"

    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None, base_url: str = INBOXES_URL) -> None:
        super().__init__(base_url, api_key, transport)

    async def domains(self) -> list[dict[str, Any]]:
        return (await self.get("/v1/domains")).get("domains") or []

    async def dns_health(self, domain_id: str) -> dict[str, Any]:
        body = await self.get(f"/v1/domains/{domain_id}/dns-health")
        return body.get("data", body)
