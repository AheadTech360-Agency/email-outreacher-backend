"""Reads answered from memory: a page asking for what it asked for a moment ago gets it without a trip to the
database, which is the slow part (each round trip is hundreds of milliseconds from far away).

What's cached: successful JSON answers to GET requests under /api/v1, for RESPONSE_CACHE_SECONDS (60).
The key is the exact session token, workspace and URL, so an answer only ever goes back to the session
that got it, after the same frontend-secret and origin checks a request passes (dependencies.secret_refusal).

When it's cleared: every change made through Coldbrew (any request that isn't a read) and every finished
background job empties it, so nothing changed here shows stale. Changes made elsewhere (Lead Intel's own
app, the follow-up check in the worker) show within RESPONSE_CACHE_SECONDS. A request sent with
X-Coldbrew-Fresh (a page's Refresh) skips it. Each API process keeps its own; a change in one process
reaches another within RESPONSE_CACHE_SECONDS.
"""

import hashlib
import json
import time
from collections import OrderedDict

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from config import get_settings
from dependencies import secret_refusal

PREFIX = "/api/v1/"
# Live by nature: a job being waited on, and the liveness check.
# Live progress, written by the worker in another process: always read fresh.
NEVER = ("/api/v1/jobs/", "/api/v1/health", "/api/v1/quickstart/")
MAX_ENTRIES = 500
MAX_BODY = 2_000_000

_entries: OrderedDict[tuple, tuple[float, bytes, bytes]] = OrderedDict()
_version = 0


def clear() -> None:
    """Something changed: no answer from before it is used again."""
    global _version
    _version += 1
    _entries.clear()


def _reports_failure(body: bytes) -> bool:
    """An answer that says an outside service failed (`error` or `errors` filled in, as on Mailboxes when
    CheapSequencer can't be reached) is never kept: the next read should try again, not repeat the failure."""
    try:
        data = json.loads(body)
    except ValueError:
        return True
    return isinstance(data, dict) and bool(data.get("error") or data.get("errors"))


class ResponseCache:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(PREFIX):
            return await self.app(scope, receive, send)
        if scope["method"] != "GET":
            started = False

            async def clear_on_answer(message: Message) -> None:
                # The change is saved by the time the answer starts, and the browser's next read can only
                # come after it arrives: clearing here, not later, means that read never gets an old answer.
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                    clear()
                await send(message)

            try:
                return await self.app(scope, receive, clear_on_answer)
            finally:
                if not started:
                    clear()

        seconds = get_settings().response_cache_seconds
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        token = headers.get("authorization", "")
        if not seconds or path.startswith(NEVER) or not token or secret_refusal(headers) is not None:
            return await self.app(scope, receive, send)  # refused, or never cached: the normal way

        version = _version
        key = (
            version, hashlib.sha256(token.encode()).hexdigest(), headers.get("x-workspace-id", ""),
            path, scope.get("query_string", b""),
        )  # fmt: skip
        hit = _entries.get(key)
        if hit and not headers.get("x-coldbrew-fresh") and time.monotonic() - hit[0] < seconds:
            _entries.move_to_end(key)
            _, content_type, body = hit
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", content_type), (b"content-length", str(len(body)).encode())]})  # fmt: skip
            await send({"type": "http.response.body", "body": body})
            return

        start: dict = {}
        chunks: list[bytes] = []

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    keep()  # before the last bytes go out, so the browser's next read finds it
            await send(message)

        def keep() -> None:
            content_type = dict(start.get("headers", [])).get(b"content-type", b"")
            body = b"".join(chunks)
            # Only a clean JSON answer, and only if nothing changed while it was being made.
            if start.get("status") == 200 and content_type.startswith(b"application/json") and len(body) <= MAX_BODY and version == _version and not _reports_failure(body):
                _entries[key] = (time.monotonic(), content_type, body)
                if len(_entries) > MAX_ENTRIES:
                    _entries.popitem(last=False)

        await self.app(scope, receive, capture)
