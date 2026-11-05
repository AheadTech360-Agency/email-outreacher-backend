"""CheapSequencer and CheapInboxes reads survive a network blip: a connection that couldn't be made is tried
once more. Writes are never retried there. No network: the transport is faked."""

import asyncio

import httpx
import pytest

from services import sending
from services.sending import SendingError, SequencerClient


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(sending, "RETRY_AFTER_SECONDS", 0)


def client(fail_times: int, method_seen: list) -> SequencerClient:
    def handler(request: httpx.Request) -> httpx.Response:
        method_seen.append(request.method)
        if len(method_seen) <= fail_times:
            raise httpx.ConnectError("blip", request=request)
        return httpx.Response(200, json={"data": [{"id": "c1"}], "has_more": False})

    return SequencerClient("k", transport=httpx.MockTransport(handler))


def test_a_read_is_tried_again_after_a_blip() -> None:
    seen: list = []
    assert asyncio.run(client(1, seen).get("/campaigns")) == {"data": [{"id": "c1"}], "has_more": False}
    assert seen == ["GET", "GET"]


def test_a_read_that_keeps_failing_says_so() -> None:
    seen: list = []
    with pytest.raises(SendingError, match="Couldn't reach CheapSequencer: ConnectError"):
        asyncio.run(client(5, seen).get("/campaigns"))
    assert len(seen) == 2  # once more, not forever


def test_a_write_is_never_retried() -> None:
    seen: list = []
    c = client(1, seen)
    with pytest.raises(SendingError):
        asyncio.run(c._write("POST", "/campaigns", "k1", {}))
    assert seen == ["POST"]



def test_activate_waits_for_the_command_and_uses_its_key():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("Idempotency-Key")))
        if request.method == "POST":
            return httpx.Response(202, json={"data": {"action": {"id": "a1", "status": "pending", "retry_after_seconds": 0.001}}})
        return httpx.Response(200, json={"data": {"id": "a1", "status": "completed"}})

    c = SequencerClient("fake", transport=httpx.MockTransport(handler))
    asyncio.run(c.activate("c1", "launch-batch-1"))
    assert seen == [("POST", "/v1/campaigns/c1/activate", "launch-batch-1"), ("GET", "/v1/api-actions/a1", None)]



def test_new_campaign_defaults_patch_has_etag_and_preserves_other_fields():
    import json
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": {"id": "c1"}})
    c = SequencerClient("fake", transport=httpx.MockTransport(handler))
    days = {"monday": {"enabled": True, "start_hour": 8, "end_hour": 18}}
    asyncio.run(c.configure_new_campaign("c1", "Coldbrew: New ICP", 300, days, "etag-current", "defaults-new-icp"))
    request = seen[0]
    assert request.method == "PATCH" and request.url.path == "/v1/campaigns/c1"
    assert request.headers["If-Match"] == "etag-current"
    assert request.headers["Idempotency-Key"] == "defaults-new-icp"
    assert json.loads(request.content) == {"name": "Coldbrew: New ICP", "daily_send_cap": 300, "sending_days": days}
