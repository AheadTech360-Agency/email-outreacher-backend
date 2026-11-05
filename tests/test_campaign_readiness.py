import asyncio
from types import SimpleNamespace

from main import app
from routers.sending import _deps
from services.campaign_readiness import assess
from services.sending import SendingError


def campaign(**extra):
    return {"id": "c1", "name": "Coldbrew: Retail", "status": "draft", "timezone": "America/Chicago",
        "steps": [{"body_text": "Hi {{first_name}}"}], **extra}


def test_unknown_provider_fields_are_never_reported_as_ready():
    out = assess(campaign(), 3)
    checks = {c["key"]: c for c in out["checks"]}
    assert checks["leads"]["status"] == checks["steps"]["status"] == "pass"
    assert checks["window"]["status"] == checks["senders"]["status"] == "unknown"


def test_missing_leads_copy_senders_and_window_are_clear():
    out = assess(campaign(steps=[], sending_days={}, sender_selection={"mode": "manual", "inbox_ids": []}), 0)
    assert out["blocking"] == 4
    assert all(c["status"] == "fail" for c in out["checks"])


def test_paused_campaign_has_explicit_blocker_and_disabled_days_dont_pass():
    out = assess(campaign(status="paused", sending_days={"monday": {"enabled": False, "start_hour": 8, "end_hour": 18}}), 2)
    assert any(c["key"] == "state" and c["status"] == "fail" for c in out["checks"])
    assert any(c["key"] == "window" and c["status"] == "fail" for c in out["checks"])


def test_all_active_variants_need_copy():
    out = assess(campaign(steps=[{"body_text": "Mirror", "variants": [{"body_text": "First"}, {"body_text": ""}]}]), 1)
    assert any(c["key"] == "steps" and c["status"] == "fail" for c in out["checks"])


class ReadOnlySequencer:
    def __init__(self):
        self.calls = []
    async def campaigns(self):
        self.calls.append("campaigns")
        return [campaign(), {"id": "external", "name": "Other campaign"}]
    async def campaign(self, campaign_id):
        self.calls.append("campaign")
        return campaign(), "etag"
    async def enrollments(self, campaign_id):
        self.calls.append("enrollments")
        return [{"id": "e1"}]


def test_readiness_endpoint_only_reads_and_checks_campaign_scope(client):
    seq = ReadOnlySequencer()
    app.dependency_overrides[_deps] = lambda: SimpleNamespace(sequencer=seq)
    result = client.get("/api/v1/sending/campaigns/c1/readiness")
    assert result.status_code == 200 and result.json()["enrolled"] == 1
    assert seq.calls == ["campaigns", "campaign", "enrollments"]
    seq.calls.clear()
    assert client.get("/api/v1/sending/campaigns/external/readiness").status_code == 404
    assert seq.calls == ["campaigns"]


def test_campaign_errors_surface_specific_requirements():
    import httpx
    from services.sending import SequencerClient
    def handler(request):
        return httpx.Response(409, json={"error": {"message": "Not ready", "details": ["missing_sending_window", "missing_sending_inboxes"]}})
    client = SequencerClient("fake", transport=httpx.MockTransport(handler))
    try:
        asyncio.run(client.rename("c1", "Coldbrew: Retail", "etag", "test-key-readiness"))
        assert False, "the write should fail"
    except SendingError as exc:
        assert "missing sending window" in exc.message and "missing sending inboxes" in exc.message
