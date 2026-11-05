"""Queueing, campaign text and follow-ups. CheapSequencer, Claude and the database are faked: no network, no cost."""

import asyncio
import csv
import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from dependencies import get_current_user
from services import outreach
from tests.conftest import ADMIN
from services.generation import FollowUpDraft
from services.sending import SendingError, SequencerClient

NOW = datetime(2026, 11, 10, 15, tzinfo=UTC)
WS = uuid4()
ICP = uuid4()
SETTINGS = {
    "sender_company": "PrintCoPilot", "sender_website": "printcopilot.co",
    "followup_fallback_2": "Hi {{first_name}}, bumping this.", "followup_fallback_3": "Hi {{first_name}}, a third.",
    "followup_fallback_4": "Hi {{first_name}}, a fourth.", "followup_fallback_5": "Hi {{first_name}}, closing the loop.",
    "followup_lead_hours": 24,
}  # fmt: skip


def run(coro):
    return asyncio.run(coro)


# ---------- Pure helpers ----------


def test_first_name_is_filled_in_or_falls_back() -> None:
    assert outreach.first_name_in("Hi {{ first_name }}, hello", "Ana") == "Hi Ana, hello"
    assert outreach.first_name_in("Hi {{first_name}}, hello", "  ") == "Hi there, hello"


def test_step_text_carries_the_signature() -> None:
    assert outreach.step_body(2, "PrintCoPilot", " printcopilot.co ") == "{{email_2_body}}\n\n{{sender_name}}\nPrintCoPilot\nprintcopilot.co"
    assert outreach.clean_website(" https://aheadtech360.com/ ") == "https://aheadtech360.com"
    assert outreach.looks_like_website("aheadtech360.com") and outreach.looks_like_website("https://www.printcopilot.co")
    assert not outreach.looks_like_website("Test address, not for real sending")
    assert outreach.signature("", "") == "{{sender_name}}"


def test_progress_reads_why_a_lead_stopped() -> None:
    p = outreach.progress({"status": "stopped", "sent_count": 1, "last_sent_at": "2026-11-03T14:00:00Z", "replied_at": "x"})
    assert (p.status, p.sent, p.stop_reason) == ("stopped", 1, "replied")
    assert p.last_sent_at == datetime(2026, 11, 3, 14, tzinfo=UTC)
    assert outreach.progress({"status": "active", "failure": {"code": "x"}}).stop_reason == "failed"
    assert outreach.progress({}).sent == 0


@pytest.mark.parametrize(
    ("sent", "hours_ago", "status", "stop", "expected"),
    [
        (1, 24 * 1, "active", None, 2),  # email 2 (day 3, two days after email 1) is due in a day: write it now
        (1, 12, "active", None, None),  # due in a day and a half: not yet
        (2, 24 * 2, "active", None, 3),  # email 3 (day 6, three days after email 2) is due in a day
        (3, 24 * 3, "active", None, 4),  # email 4 (day 10) is due in a day
        (4, 24 * 5, "active", None, 5),  # email 5 (day 16, six days after email 4) is due in a day
        (5, 24 * 9, "active", None, None),  # all five sent
        (0, 0, "active", None, None),  # email 1 hasn't gone out
        (1, 24 * 2, "active", "replied", None),  # replied: no follow-up, no cost
        (1, 24 * 2, "completed", None, None),
    ],
)
def test_due_follow_up(sent, hours_ago, status, stop, expected) -> None:
    last = NOW - timedelta(hours=hours_ago) if sent else None
    p = outreach.Progress(status, sent, last, stop)
    assert outreach.due_follow_up(p, outreach.DEFAULT_DELAYS, NOW, 24) == expected


def test_delays_come_from_the_campaign_steps() -> None:
    campaign = {"steps": [{"delay_days": 0}, {"delay_days": 4}, {"delay_days": "x"}]}
    assert outreach.delays_of(campaign) == {2: 4, 3: 3, 4: 4, 5: 6}


def test_idempotency_keys_are_stable_and_valid() -> None:
    a = outreach.key_for("import", "b", "a")
    assert a == outreach.key_for("import", "b", "a") != outreach.key_for("import", "a", "b")
    assert " " not in a and 8 <= len(a) <= 255


# ---------- Fakes ----------


def lead(email="ana@shop.com", first="Ana", icp=ICP, email_status="valid"):
    return SimpleNamespace(id=uuid4(), email=email, first_name=first, last_name="Ruiz", company="Shop", title="Owner",
                           website="shop.com", linkedin_url="", domain="shop.com", lead_intel_icp_id=icp,
                           email_status=email_status)  # fmt: skip


def email(status="approved", **extra):
    defaults = dict(id=uuid4(), status=status, subject="Quick idea", body="Body one", edited_body=None, hook="",
                    sequencer_campaign_id=None, sequencer_list_id=None, sequencer_lead_id=None, sent_at=None,
                    sequencer_inbox_id=None, sequencer_inbox_email=None, sequence_status=None, sequence_sent_count=0,
                    sequence_stop_reason=None, sequence_last_sent_at=None, sequence_synced_at=None, queued_at=None)  # fmt: skip
    return SimpleNamespace(**(defaults | extra))


class Sending:
    def __init__(self, pairs, queued_before=None):
        self.pairs, self.queued, self.added, self.existing = pairs, [], [], {}
        self.ledger: dict = dict(queued_before or {})  # address -> when it was queued

    async def queued_before(self, addresses):
        return {a: self.ledger[a] for a in addresses if a in self.ledger}

    async def claim(self, ws, campaign_id, emails):
        new = {a for _, a in emails if a not in self.ledger}
        for a in new:
            self.ledger[a] = datetime(2026, 11, 3, tzinfo=UTC)
        return new

    async def release(self, addresses):
        for a in addresses:
            self.ledger.pop(a, None)

    async def to_queue(self, ws, ids):
        return [(e, l) for e, l in self.pairs if e.id in ids and e.status == "approved"]

    async def mark_queued(self, emails, campaign_id, list_id):
        for e in emails:
            e.status, e.sequencer_campaign_id, e.sequencer_list_id = "queued", campaign_id, list_id
        self.queued += emails

    async def in_sequence(self, ws):
        return self.pairs

    async def follow_ups(self, lead_ids):
        return self.existing

    async def add_follow_up(self, first, step, body, version_id, confidence, cost):
        self.added.append((first.id, step, body))

    async def hooks(self, lead_id):
        return []

    async def commit(self):
        pass


class Sequencer:
    def __init__(self, campaigns, enrollments=None, fail=None):
        self.list_campaigns, self.enrolled, self.imports, self.patched = campaigns, [], [], []
        self.rows = enrollments or []
        self.fail = fail
        self.list_threads, self.detail = {}, {}
        self.copied, self.renamed = [], []
        self.activated = []

    async def campaigns(self):
        return self.list_campaigns

    async def campaign(self, campaign_id):
        found = next(c for c in self.list_campaigns if c["id"] == campaign_id)
        return found, "etag-1"

    async def enrollments(self, campaign_id):
        return self.rows

    async def create_list(self, name, key):
        self.lists = [*getattr(self, "lists", []), (name, key)]
        return "list-1"

    async def import_rows(self, list_id, rows, key):
        self.import_keys = [*getattr(self, "import_keys", []), key]
        if self.fail:
            raise SendingError(self.fail)
        self.imports.append((list_id, rows))

    async def enroll(self, campaign_id, list_id, key):
        self.enrolled.append((campaign_id, list_id))

    async def activate(self, campaign_id, key):
        self.activated.append((campaign_id, key))

    async def set_steps(self, campaign_id, steps, etag, key):
        self.patched.append((campaign_id, steps, etag))

    async def custom_fields(self):
        return [{"tag": t} for t in ("email_1_subject", "email_1_body", "email_2_body", "email_3_body", *getattr(self, "made", []))]

    async def create_custom_field(self, name, kind, key):
        self.made = [*getattr(self, "made", []), name.lower().replace(" ", "_")]

    async def duplicate(self, campaign_id, key):
        copy = {**next(c for c in self.list_campaigns if c["id"] == campaign_id), "id": f"copy-of-{campaign_id}", "name": "Template (copy)", "status": "draft", "steps": []}
        self.list_campaigns.append(copy)
        self.copied.append((campaign_id, key))
        return copy

    async def rename(self, campaign_id, name, etag, key):
        next(c for c in self.list_campaigns if c["id"] == campaign_id)["name"] = name
        self.renamed.append((campaign_id, name))

    async def configure_new_campaign(self, campaign_id, name, cap, sending_days, etag, key):
        row = next(c for c in self.list_campaigns if c["id"] == campaign_id)
        row.update(name=name, daily_send_cap=cap, sending_days=sending_days)
        self.renamed.append((campaign_id, name))

    async def set_daily_cap(self, campaign_id, cap, etag, key):
        next(c for c in self.list_campaigns if c["id"] == campaign_id)["daily_send_cap"] = cap

    async def inboxes(self):
        return [{"id": "in1", "email": "emily@meetaheadtech360.com"}]

    async def threads(self, campaign_id):
        return self.list_threads.get(campaign_id, [])

    async def thread(self, thread_id):
        return self.detail


class Repo:
    """Anything else Deps needs, answering with empty values."""

    def __init__(self, **answers):
        self.answers = answers

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            value = self.answers.get(name)
            return value(*args) if callable(value) else value

        return call


def deps(sending, sequencer, settings=SETTINGS, excluded=None, writer=None, prompts=None):
    async def matcher():
        hit = SimpleNamespace(reason="do_not_contact", key_value=excluded[1]) if excluded else None
        return lambda keys: hit if excluded and excluded in keys else None

    exclusions = SimpleNamespace(matcher=matcher)
    return outreach.Deps(
        WS, sending, Repo(sending=lambda ws: dict(settings)), Repo(get_many=lambda ws, ids: {}), prompts or Repo(),
        Repo(), Repo(for_icp=lambda *a: []), exclusions, Repo(record=None),
        Repo(get_icp=lambda icp_id: {"id": icp_id, "name": "DTF"}), sequencer, writer,
    )  # fmt: skip


CAMPAIGN = {"id": "c1", "name": "Coldbrew: DTF", "status": "draft", "steps": [
    {"id": "s1", "delay_days": 0, "thread_mode": "new_thread", "subject": "{{email_1_subject}}", "body_text": "{{email_1_body}}"},
    {"id": "s2", "delay_days": 3, "thread_mode": "reply_to_previous", "body_text": "{{email_2_body}}"},
    {"id": "s3", "delay_days": 5, "thread_mode": "reply_to_previous", "body_text": "{{email_3_body}}"},
]}  # fmt: skip


# ---------- Queue ----------


def test_queue_imports_and_enrolls_without_activating() -> None:
    ok, no_address, no_icp, blocked = (email(), lead()), (email(), lead(email="")), (email(), lead(icp=None)), (email(), lead(email="x@dnc.com"))
    draft = (email(status="draft"), lead())
    sending, seq = Sending([ok, no_address, no_icp, blocked, draft]), Sequencer([CAMPAIGN])
    out = run(outreach.queue(deps(sending, seq, excluded=("email", "x@dnc.com")), [p[0].id for p in (ok, no_address, no_icp, blocked, draft)]))

    by_id = {o.email_id: o for o in out}
    assert by_id[ok[0].id].status == "queued" and by_id[ok[0].id].campaign == "Coldbrew: DTF"
    assert [by_id[p[0].id].status for p in (no_address, no_icp, blocked, draft)] == ["skipped"] * 4
    assert by_id[blocked[0].id].reason == "On the do-not-contact list: asked not to be contacted (x@dnc.com)"
    assert seq.enrolled == [("c1", "list-1")]
    assert seq.activated == []  # starting to send stays a person's decision
    [(_, rows)] = seq.imports
    assert rows == [{
        "email": "ana@shop.com", "first_name": "Ana", "last_name": "Ruiz", "company_name": "Shop", "job_title": "Owner",
        "website": "shop.com", "linkedin_profile": "", "email_1_subject": "Quick idea", "email_1_body": "Body one",
        "email_2_body": "Hi Ana, bumping this.", "email_3_body": "Hi Ana, a third.", "email_4_body": "Hi Ana, a fourth.",
        "email_5_body": "Hi Ana, closing the loop.",
    }]  # fmt: skip
    assert ok[0].status == "queued" and ok[0].sequencer_campaign_id == "c1"


def test_a_risky_address_needs_its_own_confirmation_to_queue() -> None:
    """A catch-all domain accepts any address, so the email may bounce: it's queued only when a person confirmed it."""
    valid, risky, confirmed = (email(), lead()), (email(), lead(email="cory@iodev.com", email_status="risky")),         (email(), lead(email="jo@iodev.com", email_status="risky"))  # fmt: skip
    seq = Sequencer([CAMPAIGN])
    out = run(outreach.queue(deps(Sending([valid, risky, confirmed]), seq), [p[0].id for p in (valid, risky, confirmed)], {confirmed[0].id}))
    by_id = {o.email_id: o for o in out}
    assert by_id[valid[0].id].status == "queued" and by_id[confirmed[0].id].status == "queued"
    assert by_id[risky[0].id].status == "skipped" and by_id[risky[0].id].reason == outreach.RISKY
    assert risky[0].status == "approved"  # still approved, waiting for its own confirmation


def test_a_website_address_needs_its_own_confirmation_to_queue() -> None:
    """orders@ or info@ from a company's site reaches a shared inbox, not a person: reviewed and confirmed separately."""
    site, confirmed = (email(), lead(email="orders@quicktransfers.com", email_status="website")),         (email(), lead(email="info@dtfaffordable.com", email_status="website"))  # fmt: skip
    out = run(outreach.queue(deps(Sending([site, confirmed]), Sequencer([CAMPAIGN])), [site[0].id, confirmed[0].id], {confirmed[0].id}))
    by_id = {o.email_id: o for o in out}
    assert by_id[site[0].id].status == "skipped" and by_id[site[0].id].reason == outreach.WEBSITE
    assert by_id[confirmed[0].id].status == "queued"


def test_queue_needs_the_sender_website() -> None:
    with pytest.raises(SendingError, match="website"):
        run(outreach.queue(deps(Sending([]), Sequencer([]), settings=SETTINGS | {"sender_website": " "}), [uuid4()]))


def test_queue_without_a_campaign_leaves_the_email_approved() -> None:
    pair = (email(), lead())
    out = run(outreach.queue(deps(Sending([pair]), Sequencer([])), [pair[0].id]))
    assert out[0].status == "skipped" and "Coldbrew: DTF" in out[0].reason and pair[0].status == "approved"


def test_a_failed_import_leaves_the_email_approved() -> None:
    pair = (email(), lead())
    out = run(outreach.queue(deps(Sending([pair]), Sequencer([CAMPAIGN], fail="The import didn't finish")), [pair[0].id]))
    assert (out[0].status, out[0].reason, pair[0].status) == ("skipped", "The import didn't finish", "approved")


# ---------- Campaign text ----------


def test_templates_write_the_signature_into_draft_campaigns_only() -> None:
    sending = Sequencer([CAMPAIGN, CAMPAIGN | {"id": "c2", "name": "Coldbrew: Live", "status": "active"}, {"id": "c3", "name": "Other"}])
    out = run(outreach.sync_templates(deps(Sending([]), sending)))
    assert [(o.campaign, o.status) for o in out] == [("Coldbrew: DTF", "updated"), ("Coldbrew: Live", "skipped")]
    [(campaign_id, steps, etag)] = sending.patched
    assert (campaign_id, etag) == ("c1", "etag-1")
    # Five steps on the schedule: the three it had keep their ids, emails 4 and 5 are added like email 2.
    assert [s.get("id") for s in steps] == ["s1", "s2", "s3", None, None] and steps[0]["subject"] == "{{email_1_subject}}"
    assert [s["delay_days"] for s in steps] == [0, 2, 3, 4, 6]
    assert steps[2]["body_text"] == "{{email_3_body}}\n\n{{sender_name}}\nPrintCoPilot\nprintcopilot.co"
    assert steps[4]["body_text"].startswith("{{email_5_body}}") and steps[4].get("thread_mode") == steps[1].get("thread_mode", "reply_to_previous")
    assert "subject" not in steps[1] and "subject" not in steps[3]
    assert sending.made == ["email_4_body", "email_5_body"]  # the fields emails 4 and 5 ride in


def test_templates_already_in_place_are_left_alone() -> None:
    steps = [{"id": f"s{i}", "delay_days": d, "body_text": outreach.step_body(i, "PrintCoPilot", "printcopilot.co")}
             for i, d in outreach.SCHEDULE.items()]  # fmt: skip
    seq = Sequencer([CAMPAIGN | {"steps": steps}])
    assert [o.status for o in run(outreach.sync_templates(deps(Sending([]), seq)))] == ["unchanged"] and seq.patched == []


# ---------- Follow-ups ----------


def enrollment(address, **extra):
    return {"lead": {"id": "L-" + address, "email": address.upper()}, "status": "active", "sent_count": 1, "assigned_inbox_id": "in1",
            "last_sent_at": (NOW - timedelta(days=2)).isoformat()} | extra  # fmt: skip


def queued_pair(address):
    return (email(status="queued", sequencer_campaign_id="c1", sequencer_list_id="list-1"), lead(email=address))


@pytest.fixture
def claude(monkeypatch):
    calls = []

    async def write(writer, spec, user):
        calls.append(user)
        usage = SimpleNamespace(model="claude-haiku-5-5", input_tokens=1000, output_tokens=100)
        return FollowUpDraft(body="Hi Ana, one more thought.", confidence=80), [usage], None

    monkeypatch.setattr(outreach, "write_follow_up", write)
    return calls


def test_follow_ups_are_written_only_for_leads_who_have_not_replied(claude) -> None:
    due, replied, early = queued_pair("due@x.com"), queued_pair("replied@x.com"), queued_pair("early@x.com")
    rows = [
        enrollment("due@x.com"),
        enrollment("replied@x.com", status="stopped", replied_at="2026-11-09T10:00:00Z"),
        enrollment("early@x.com", last_sent_at=NOW.isoformat()),
    ]
    sending, seq = Sending([due, replied, early]), Sequencer([CAMPAIGN], rows)
    version = SimpleNamespace(id=uuid4(), system_prompt="s", user_prompt="Write email 2 for {{lead.firstName}}", model="claude-haiku-5-5", max_tokens=400)
    result = run(outreach.run_follow_ups(deps(sending, seq, writer=object(), prompts=Repo(for_icp=version)), now=NOW))

    assert (result.checked, result.written, result.ended) == (3, 1, 1)
    assert claude == ["Write email 2 for Ana"]  # one Claude call, for the lead who is due
    assert seq.imports == [("list-1", [{"email": "due@x.com", "email_2_body": "Hi Ana, one more thought."}])]
    assert sending.added == [(due[0].id, 2, "Hi Ana, one more thought.")]
    assert all(p[0].status == "sent" for p in (due, replied, early))  # email 1 went out to all three
    assert replied[0].sequence_stop_reason == "replied" and due[0].sequencer_lead_id == "L-due@x.com"
    assert (due[0].sequencer_inbox_id, due[0].sequencer_inbox_email) == ("in1", "emily@meetaheadtech360.com")
    assert result.cost_usd > 0


def test_follow_ups_already_written_are_not_written_again(claude) -> None:
    pair = queued_pair("due@x.com")
    sending = Sending([pair])
    sending.existing = {(pair[1].id, 2): email(status="queued")}
    result = run(outreach.run_follow_ups(deps(sending, Sequencer([CAMPAIGN], [enrollment("due@x.com")]), writer=object()), now=NOW))
    assert (result.written, claude) == (0, [])


def test_without_claude_the_generic_follow_up_stays(claude) -> None:
    pair = queued_pair("due@x.com")
    result = run(outreach.run_follow_ups(deps(Sending([pair]), Sequencer([CAMPAIGN], [enrollment("due@x.com")])), now=NOW))
    assert result.written == 0 and "ANTHROPIC_API_KEY" in result.notes[0] and claude == []


# ---------- The CheapSequencer client's writes ----------


def test_import_rows_uploads_a_csv_and_waits_for_it() -> None:
    seen: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append((request.method, path, dict(request.headers)))
        if path.endswith("/imports/upload-url"):
            body = json.loads(request.content)
            assert body["list_id"] == "list-1" and body["content_type"] == "text/csv"
            return httpx.Response(201, json={"data": {"import": {"id": "imp1"}, "upload": {"url": "https://up.example/x", "method": "PUT"}}})
        if request.url.host == "up.example":
            assert "authorization" not in request.headers  # the API key never goes to the storage host
            rows = list(csv.DictReader(io.StringIO(request.content.decode())))
            assert rows == [{"email": "a@x.com", "email_2_body": "Hi, line one\nline two"}]
            return httpx.Response(200)
        if path.endswith("/process"):
            return httpx.Response(202, json={"data": {"action": {"id": "act1", "status": "completed"}}})
        if path.endswith("/imports/imp1"):
            return httpx.Response(200, json={"data": {"counts": {"updated": 1}}})
        return httpx.Response(200, json={"data": {}})

    client = SequencerClient("k", transport=httpx.MockTransport(handler))
    out = run(client.import_rows("list-1", [{"email": "a@x.com", "email_2_body": "Hi, line one\nline two"}], "coldbrew-test-key"))
    assert out == {"counts": {"updated": 1}}
    writes = [(m, p, h) for m, p, h in seen if m == "POST"]
    assert [p.rsplit("/", 1)[-1] for _, p, _ in writes] == ["upload-url", "mapping-suggestion", "mapping", "process"]
    assert all(" " not in h["idempotency-key"] for _, _, h in writes)


# ---------- Outbox and replies ----------


@pytest.mark.parametrize(
    ("status", "sent", "stop", "expected"),
    [(None, 0, None, "queued"), ("active", 0, None, "queued"), ("active", 2, None, "sending"), ("stopped", 1, "replied", "replied"),
     ("completed", 3, None, "finished"), ("removed", 1, None, "stopped"), ("paused", 1, None, "paused")],
)  # fmt: skip
def test_lead_state(status, sent, stop, expected) -> None:
    assert outreach.lead_state(email(sequence_status=status, sequence_sent_count=sent, sequence_stop_reason=stop)) == expected


def test_outbox_steps_show_generic_text_until_written() -> None:
    first = email(status="sent", sequence_status="active", sequence_sent_count=1)
    written = email(status="queued", body="Hi Ana, a new angle.")
    fallbacks = {2: "Hi {{first_name}}, two.", 3: "Hi {{first_name}}, three.", 4: "Hi {{first_name}}, four.", 5: "Hi {{first_name}}, five."}
    steps = outreach.outbox_steps(first, lead(), {2: written}, fallbacks)
    assert [(s.step, s.status, s.personalized) for s in steps] == [(1, "sent", True), (2, "waiting", True), (3, "waiting", False),
                                                                   (4, "waiting", False), (5, "waiting", False)]  # fmt: skip
    assert steps[2].body == "Hi Ana, three." and steps[1].subject == ""

    replied = email(status="sent", sequence_status="stopped", sequence_sent_count=1, sequence_stop_reason="replied")
    assert [s.status for s in outreach.outbox_steps(replied, lead(), {}, {2: "", 3: "", 4: "", 5: ""})] == ["sent", *["skipped"] * 4]


THREAD = {
    "id": "p:in1:abc", "mailbox": {"id": "in1", "address": "emily@meetaheadtech360.com"}, "subject": "Quick idea",
    "preview": "Sounds good", "last_message_at": "2026-11-05T15:00:00Z", "message_count": 2, "inbound_count": 1,
    "unread_count": 1, "lead": {"email": "Ana@Shop.com", "name": None}, "sequence": {"id": "c1", "name": "Coldbrew: DTF"},
    "reply_classification": "interested",
}  # fmt: skip


@pytest.fixture
def api():
    from fastapi.testclient import TestClient

    from main import app
    from routers import sending as router

    ana = lead(email="ana@shop.com")
    first = email(status="sent", sequencer_campaign_id="c1", sequence_status="active", sequence_sent_count=1,
                  sequencer_inbox_email="emily@meetaheadtech360.com")  # fmt: skip
    store = Sending([(first, ana)])
    store.outbox = lambda ws: _value([(first, ana)])
    store.leads_by_email = lambda ws, emails: _value({"ana@shop.com": ana})
    seq = Sequencer([CAMPAIGN, {"id": "c9", "name": "Not ours"}])
    seq.list_threads = {"c1": [THREAD, THREAD | {"id": "p:in1:none", "inbound_count": 0}]}
    seq.detail = {"thread": THREAD, "messages": [
        {"id": "m2", "direction": "inbound", "from": {"name": "Ana", "address": "ana@shop.com"}, "to": [{"address": "emily@meetaheadtech360.com"}],
         "sent_at": "2026-11-05T15:00:00Z", "subject": "Re: Quick idea", "body_text": "Sounds good"},
        {"id": "m1", "direction": "outbound", "from": {"address": "emily@meetaheadtech360.com"}, "to": [{"address": "ana@shop.com"}],
         "sent_at": "2026-11-04T15:00:00Z", "subject": "Quick idea", "body_text": "Body one"},
    ]}  # fmt: skip
    app.dependency_overrides[router._deps] = lambda: deps(store, seq)
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    yield TestClient(app), ana
    app.dependency_overrides.clear()


async def _value(v):
    return v


def test_outbox_lists_each_lead_with_its_mailbox_and_emails(api) -> None:
    client, ana = api
    [row] = client.get("/api/v1/sending/outbox").json()
    assert (row["leadId"], row["campaign"], row["mailbox"], row["state"]) == (str(ana.id), "Coldbrew: DTF", "emily@meetaheadtech360.com", "sending")
    assert [s["status"] for s in row["steps"]] == ["sent", "waiting", "waiting", "waiting", "waiting"]
    assert row["steps"][1]["body"] == "Hi Ana, bumping this." and row["steps"][1]["personalized"] is False


def test_replies_list_only_conversations_where_the_lead_wrote_back(api) -> None:
    client, ana = api
    [t] = client.get("/api/v1/sending/replies").json()
    assert (t["id"], t["leadName"], t["leadId"], t["campaign"], t["classification"]) == ("p:in1:abc", "Ana Ruiz", str(ana.id), "Coldbrew: DTF", "interested")


def test_a_reply_thread_reads_oldest_first(api) -> None:
    client, _ = api
    d = client.get("/api/v1/sending/replies/p:in1:abc").json()
    assert [m["direction"] for m in d["messages"]] == ["outbound", "inbound"]
    assert d["messages"][1]["body"] == "Sounds good" and d["messages"][1]["to"] == ["emily@meetaheadtech360.com"]


def test_a_thread_outside_coldbrew_campaigns_is_not_shown(api) -> None:
    client, _ = api
    from routers import sending as router
    from main import app

    seq = Sequencer([CAMPAIGN])
    seq.detail = {"thread": THREAD | {"sequence": {"id": "c9"}}, "messages": []}
    app.dependency_overrides[router._deps] = lambda: deps(Sending([]), seq)
    assert client.get("/api/v1/sending/replies/p:in1:abc").status_code == 404


# ---------- Each ICP's campaign ----------


class Links:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    async def all(self):
        return dict(self.rows)

    async def link(self, icp_id, campaign_id):
        self.rows[icp_id] = campaign_id


def test_a_new_icp_gets_a_draft_copy_of_a_coldbrew_campaign() -> None:
    seq, links, icp = Sequencer([CAMPAIGN, {"id": "x", "name": "Someone else's", "status": "active"}]), Links(), uuid4()
    out = run(outreach.ensure_campaign(seq, links, icp, " Embroidery "))
    assert (out.status, out.campaign) == ("created", "Coldbrew: Embroidery")
    assert [c for c, _ in seq.copied] == ["c1"] and seq.renamed == [("copy-of-c1", "Coldbrew: Embroidery")]
    assert links.rows == {icp: "copy-of-c1"}
    # Its mailboxes' own daily limits hold sending back, not the campaign.
    assert next(c for c in seq.list_campaigns if c["id"] == "copy-of-c1")["daily_send_cap"] == outreach.CAMPAIGN_DAILY_CAP == 300
    # Saving it again changes nothing, and never makes a second copy.
    assert run(outreach.ensure_campaign(seq, links, icp, "Embroidery")).status == "unchanged" and len(seq.copied) == 1


def test_an_existing_campaign_with_the_name_is_linked_not_copied() -> None:
    seq, links, icp = Sequencer([CAMPAIGN]), Links(), uuid4()
    assert run(outreach.ensure_campaign(seq, links, icp, "DTF")).status == "linked"
    assert links.rows == {icp: "c1"} and seq.copied == []


def test_renaming_an_icp_renames_its_draft_campaign() -> None:
    icp = uuid4()
    seq = Sequencer([dict(CAMPAIGN)])
    out = run(outreach.ensure_campaign(seq, Links({icp: "c1"}), icp, "DTF printers"))
    assert (out.status, seq.renamed) == ("renamed", [("c1", "Coldbrew: DTF printers")])


def test_a_sending_campaign_keeps_its_name() -> None:
    icp = uuid4()
    seq = Sequencer([CAMPAIGN | {"status": "active"}])
    out = run(outreach.ensure_campaign(seq, Links({icp: "c1"}), icp, "DTF printers"))
    assert out.status == "skipped" and "Pause it" in out.note and seq.renamed == []


def test_without_a_campaign_to_copy_nothing_is_made() -> None:
    seq = Sequencer([{"id": "x", "name": "Someone else's", "status": "draft"}])
    out = run(outreach.ensure_campaign(seq, Links(), uuid4(), "DTF"))
    assert out.status == "skipped" and seq.copied == []


def test_queueing_uses_the_linked_campaign_after_a_rename() -> None:
    pair = (email(), lead())
    renamed = CAMPAIGN | {"name": "Coldbrew: DTF printers"}  # the ICP is still called "DTF" in this fake
    d = deps(Sending([pair]), Sequencer([renamed]))
    d.links = Links({ICP: "c1"})
    [out] = run(outreach.queue(d, [pair[0].id]))
    assert (out.status, out.campaign) == ("queued", "Coldbrew: DTF printers")


def test_duplicate_reads_the_copy_from_the_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/campaigns/c1/duplicate") and request.headers["idempotency-key"] == "coldbrew-dup-key"
        return httpx.Response(200, json={"data": {"sequence": {"id": "c2", "name": "Coldbrew: DTF (copy)", "status": "draft"}, "subsequences": []}})

    client = SequencerClient("k", transport=httpx.MockTransport(handler))
    assert run(client.duplicate("c1", "coldbrew-dup-key"))["id"] == "c2"


def test_a_failed_queue_can_be_retried() -> None:
    pair = (email(), lead())
    seq = Sequencer([CAMPAIGN], fail="The import didn't finish")
    run(outreach.queue(deps(Sending([pair]), seq), [pair[0].id]))
    seq.fail = None
    [out] = run(outreach.queue(deps(Sending([pair]), seq), [pair[0].id]))
    assert out.status == "queued"
    # Same list (same key and name, so CheapSequencer answers with the first one); a new key per import.
    assert seq.lists[0] == seq.lists[1] and seq.import_keys[0] != seq.import_keys[1]


# ---------- Nobody is queued twice ----------


def test_an_address_queued_before_is_never_queued_again() -> None:
    """Whichever lead, ICP or workspace it turns up under again, and even after its first lead was deleted."""
    again = (email(), lead(email="Ana@Shop.com"))  # a new lead and draft for someone queued in October
    sending = Sending([again], queued_before={"ana@shop.com": datetime(2026, 10, 20, tzinfo=UTC)})
    seq = Sequencer([CAMPAIGN])
    out = run(outreach.queue(deps(sending, seq), [again[0].id]))
    assert out[0].status == "skipped" and out[0].reason.startswith("Ana@Shop.com was already queued on Oct 20, 2026")
    assert seq.imports == [] and again[0].status == "approved"


def test_the_same_address_twice_in_one_batch_is_queued_once() -> None:
    first, twin = (email(), lead()), (email(), lead())  # two leads, one address
    sending, seq = Sending([first, twin]), Sequencer([CAMPAIGN])
    out = run(outreach.queue(deps(sending, seq), [first[0].id, twin[0].id]))
    assert sorted(o.status for o in out) == ["queued", "skipped"] and "in this batch twice" in next(o.reason for o in out if o.status == "skipped")
    assert sum(len(rows) for _, rows in seq.imports) == 1


def test_once_queued_a_second_run_skips_it_and_a_failed_import_gives_it_back() -> None:
    sending = Sending([(email(), lead())])
    out = run(outreach.queue(deps(sending, Sequencer([CAMPAIGN], fail="The import didn't finish")), [sending.pairs[0][0].id]))
    assert out[0].status == "skipped" and sending.ledger == {}  # not sent, so it can be queued later
    run(outreach.queue(deps(sending, Sequencer([CAMPAIGN])), [sending.pairs[0][0].id]))
    assert "ana@shop.com" in sending.ledger
    later = (email(), lead(email="ana@shop.com"))
    sending.pairs.append(later)
    out = run(outreach.queue(deps(sending, Sequencer([CAMPAIGN])), [later[0].id]))
    assert out[0].status == "skipped" and "already queued" in out[0].reason


@pytest.mark.parametrize("status", ["draft", "active", "paused"])
def test_queueing_never_activates_a_campaign(status):
    pair = email(), lead()
    seq = Sequencer([{**CAMPAIGN, "status": status}])
    outcomes = run(outreach.queue(deps(Sending([pair]), seq), [pair[0].id]))
    assert outcomes[0].status == "queued" and outcomes[0].reason is None and not seq.activated
    assert not hasattr(outreach.SequencerClient if hasattr(outreach, "SequencerClient") else object, "activate")


def test_nothing_enrolled_never_activates_campaign():
    pair = email(), lead()
    sending, seq = Sending([pair]), Sequencer([CAMPAIGN], fail="Import failed")
    out = run(outreach.queue(deps(sending, seq), [pair[0].id]))
    assert out[0].status == "skipped" and not seq.activated



def test_new_campaign_has_8_to_6_window_and_preserves_timezone_and_enabled_days():
    template = {**CAMPAIGN, "timezone": "America/Chicago", "sending_days": {
        day: {"enabled": day in ("tuesday", "saturday"), "start_hour": 10, "end_hour": 15}
        for day in outreach.CAMPAIGN_WEEKDAYS}}
    seq, links = Sequencer([template]), Links()
    result = run(outreach.ensure_campaign(seq, links, uuid4(), "New ICP"))
    copied = next(c for c in seq.list_campaigns if c["id"] == "copy-of-c1")
    assert result.status == "created" and copied["timezone"] == "America/Chicago"
    for day, window in copied["sending_days"].items():
        assert window == {"enabled": day in ("tuesday", "saturday"), "start_hour": 8, "end_hour": 18}
    assert template["sending_days"]["tuesday"]["start_hour"] == 10


def test_failed_new_campaign_configuration_is_finished_on_retry_without_copying_again():
    seq, links, icp = Sequencer([dict(CAMPAIGN)]), Links(), uuid4()
    configure = seq.configure_new_campaign
    async def fail(*args):
        raise SendingError("temporary failure")
    seq.configure_new_campaign = fail
    with pytest.raises(SendingError):
        run(outreach.ensure_campaign(seq, links, icp, "New ICP"))
    seq.configure_new_campaign = configure
    run(outreach.ensure_campaign(seq, links, icp, "New ICP"))
    assert len(seq.copied) == 1
    copied = next(c for c in seq.list_campaigns if c["id"] == "copy-of-c1")
    assert copied["name"] == "Coldbrew: New ICP"
    assert all(d["start_hour"] == 8 and d["end_hour"] == 18 for d in copied["sending_days"].values())
