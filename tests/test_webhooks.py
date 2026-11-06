"""CheapSequencer webhooks (routers/webhooks.py, services/webhooks.py): signed deliveries only, each stored once."""

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from config import get_settings
from dependencies import get_optional_sequencer, get_sending_repo
from main import app
from services import notify
from services.webhooks import stop_of, verify

SECRET = "whsec_test"
URL = "/api/v1/webhooks/cheapsequencer"


def sign(body: bytes, secret: str = SECRET, at: int | None = None) -> str:
    at = int(time.time()) if at is None else at
    return f"t={at},v1=" + hmac.new(secret.encode(), f"{at}.".encode() + body, hashlib.sha256).hexdigest()


def event(kind: str = "message.replied", **data) -> dict:
    return {"id": data.pop("event_id", "evt_1"), "type": kind, "occurred_at": "2026-10-31T12:00:00Z", "api_version": "v1",
            "workspace_id": "ws_cs", "data": {"lead_id": "lead_cs_1", "sequence_id": "camp_1", **data}}  # fmt: skip


# ---------- The signature ----------


def test_only_a_fresh_signature_over_this_exact_body_passes() -> None:
    body, now = b'{"id":"evt_1"}', time.time()
    assert verify(SECRET, sign(body), body, now)
    assert not verify("another", sign(body), body, now)
    assert not verify(SECRET, sign(body), body + b" ", now)  # body changed after signing
    assert not verify(SECRET, sign(body, at=int(now) - 301), body, now)  # replayed later
    assert not verify(SECRET, None, body, now) and not verify(SECRET, "v1=abc", body, now)


def test_what_each_event_means() -> None:
    assert stop_of(event()).type == "replied"
    assert stop_of(event("message.bounced")).type == "bounced"
    assert stop_of(event("enrollment.stopped", reason="unsubscribed")).type == "unsubscribed"
    # A manual stop, a colleague's reply, other events: the follow-up check records those.
    assert stop_of(event("enrollment.stopped", reason="manual")) is None
    assert stop_of(event("enrollment.stopped", reason="company_replied")) is None
    assert stop_of(event("message.sent")) is None


# ---------- The endpoint ----------


class FakeSending:
    def __init__(self) -> None:
        self.email = SimpleNamespace(id=uuid4(), workspace_id=uuid4(), lead_id=uuid4(), sequence_stop_reason=None, sequencer_lead_id=None)
        self.known_lead_id: str | None = "lead_cs_1"
        self.address = "ana@shop.com"
        self.events: dict[str, str] = {}
        self.lookups: list[tuple] = []

    async def first_email_for(self, campaign_id, lead_id=None, address=None):
        self.lookups.append((campaign_id, lead_id, address))
        if address:
            return self.email if address.lower() == self.address else None  # as the real query: lower(email)
        return self.email if lead_id and lead_id == self.known_lead_id else None

    async def record_stop(self, email, stop, payload):
        if stop.event_id in self.events:
            return False
        self.events[stop.event_id] = stop.type
        email.sequence_stop_reason = email.sequence_stop_reason or stop.type
        return True

    async def has_event(self, email_id, kind):
        return kind in self.events.values()

    async def lead(self, lead_id):
        return SimpleNamespace(id=lead_id, email=self.address, first_name="Ana", last_name="", company="Shop")


class FakeSequencer:
    def __init__(self, leads: dict[str, dict]) -> None:
        self.leads = leads

    async def get(self, path: str):
        return {"data": self.leads.get(path.rsplit("/", 1)[-1], {})}


@pytest.fixture
def hooks(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "cheapsequencer_webhook_secret", SECRET)
    sending = FakeSending()
    app.dependency_overrides[get_sending_repo] = lambda: sending
    app.dependency_overrides[get_optional_sequencer] = lambda: FakeSequencer({"lead_cs_2": {"email": "Ana@Shop.com"}})
    emailed: list = []  # the reply emails sent to the team

    async def reply_received(lead, campaign=None):
        emailed.append(lead.email)
        return True

    monkeypatch.setattr(notify, "reply_received", reply_received)
    return SimpleNamespace(client=client, sending=sending, emailed=emailed)


def deliver(client, payload: dict, signature: str | None = "sign"):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "X-CheapSequencer-Event": payload.get("type", "")}
    if signature == "sign":
        headers["X-CheapSequencer-Signature"] = sign(body)
    elif signature:
        headers["X-CheapSequencer-Signature"] = signature
    return client.post(URL, content=body, headers=headers)


def test_a_reply_stops_the_sequence_once(hooks) -> None:
    r = deliver(hooks.client, event())
    assert r.status_code == 200 and r.json() == {"status": "recorded"}
    assert hooks.sending.email.sequence_stop_reason == "replied"
    # Delivered again (at least once): stored once.
    assert deliver(hooks.client, event()).json() == {"status": "duplicate"}
    assert hooks.sending.events == {"evt_1": "replied"}


def test_needs_no_sign_in_or_frontend_secret(hooks) -> None:
    """CheapSequencer can't sign in: the signature is what's checked (tests skip the frontend secret anyway,
    so this guards the route being outside the protected router)."""
    from main import protected

    assert not any(getattr(r, "path", "").startswith("/webhooks") for r in protected.routes)
    assert deliver(hooks.client, event()).status_code == 200


def test_a_bad_signature_is_refused_and_nothing_stored(hooks) -> None:
    r = deliver(hooks.client, event(), signature=sign(b"something else"))
    assert r.status_code == 401
    assert hooks.sending.events == {} and hooks.sending.email.sequence_stop_reason is None


def test_the_unsigned_check_when_subscribing_gets_a_2xx_and_does_nothing(hooks) -> None:
    r = deliver(hooks.client, event(), signature=None)
    assert r.status_code == 200 and hooks.sending.events == {} and hooks.sending.lookups == []


def test_before_the_secret_is_set_deliveries_are_accepted_but_ignored(hooks, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "cheapsequencer_webhook_secret", None)
    r = deliver(hooks.client, event())
    assert r.status_code == 200 and r.json() == {"status": "ignored"} and hooks.sending.events == {}


def test_a_lead_not_yet_matched_by_id_is_found_by_its_address(hooks) -> None:
    r = deliver(hooks.client, event(lead_id="lead_cs_2", event_id="evt_2"))
    assert r.json() == {"status": "recorded"}
    assert hooks.sending.lookups[-1] == ("camp_1", None, "Ana@Shop.com")


def test_someone_else_s_lead_is_ignored(hooks) -> None:
    assert deliver(hooks.client, event(lead_id="lead_unknown", event_id="evt_3")).json() == {"status": "ignored"}
    assert hooks.sending.events == {}


def test_other_events_are_ignored(hooks) -> None:
    assert deliver(hooks.client, event("message.sent")).json() == {"status": "ignored"}
    assert hooks.sending.lookups == []


# ---------- The reply email to the team ----------


def test_each_reply_is_emailed_once_even_when_delivered_again(hooks) -> None:
    deliver(hooks.client, event())
    deliver(hooks.client, event())  # the same delivery again
    assert hooks.emailed == ["ana@shop.com"]


def test_a_later_reply_in_the_thread_is_emailed_too(hooks) -> None:
    deliver(hooks.client, event())
    deliver(hooks.client, event(event_id="evt_9"))
    assert hooks.emailed == ["ana@shop.com", "ana@shop.com"]


def test_a_reply_the_follow_up_check_already_emailed_isnt_emailed_again(hooks) -> None:
    hooks.sending.email.sequence_stop_reason = "replied"  # the check saw it first, and emailed it
    assert deliver(hooks.client, event()).json() == {"status": "recorded"}
    assert hooks.emailed == []


def test_bounces_and_unsubscribes_arent_emailed(hooks) -> None:
    deliver(hooks.client, event("message.bounced", event_id="evt_b"))
    deliver(hooks.client, event("enrollment.stopped", reason="unsubscribed", event_id="evt_u"))
    assert hooks.emailed == []