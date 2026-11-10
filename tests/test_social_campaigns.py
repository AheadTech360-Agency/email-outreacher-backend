"""LinkedIn and Instagram campaigns (services/own_social.py, routers/campaigns.py): steps, profiles, prompts and the
Unipile calls that send. Unipile is faked over HTTP; nothing reaches LinkedIn or Instagram."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from routers.campaigns import StepIn, _check_steps
from services.prompts import DEFAULTS, PLACEHOLDER
from services.unipile import UnipileClient, profile_identifier


def test_profile_handles_come_from_their_addresses() -> None:
    assert profile_identifier("linkedin", "https://www.linkedin.com/in/maya-lane/") == "maya-lane"
    assert profile_identifier("linkedin", "linkedin.com/in/maya-lane?trk=x") == "maya-lane"
    assert profile_identifier("linkedin", "https://www.linkedin.com/company/acme") is None  # a company page isn't a person
    assert profile_identifier("instagram", "https://www.instagram.com/glowmedspa/") == "glowmedspa"
    assert profile_identifier("instagram", "@glow.med_spa") == "glow.med_spa"
    assert profile_identifier("instagram", "https://www.instagram.com/p/Cx123/") is None  # a post, not a profile
    assert profile_identifier("linkedin", None) is None and profile_identifier("instagram", "") is None


def steps(*specs) -> list[StepIn]:
    return [StepIn(action=a, body=b, delay_days=d) for a, b, d in specs]


def test_linkedin_steps_are_one_request_first_then_messages() -> None:
    _check_steps(steps(("invite", "{{aiMessage}}", 0), ("message", "{{aiMessage}}", 2)), "linkedin")
    for bad in (steps(("message", "x", 0), ("invite", "x", 2)),          # a request only first
                steps(("email", "x", 0)),                                   # not an email
                steps(("invite", "{{email1Body}}", 0)),                     # no email parts in a DM
                steps(("invite", "x" * 301, 0))):                           # longer than LinkedIn allows
        with pytest.raises(HTTPException) as caught:
            _check_steps(bad, "linkedin")
        assert caught.value.status_code == 422


def test_instagram_steps_are_messages_and_email_steps_are_emails() -> None:
    _check_steps(steps(("message", "Hi {{firstName|there}}, {{aiMessage}}", 0)), "instagram")
    with pytest.raises(HTTPException):
        _check_steps(steps(("invite", "x", 0)), "instagram")
    with pytest.raises(HTTPException):
        _check_steps([StepIn(action="message", subject="Hi", body="x")], "email")


def test_social_prompts_only_use_what_is_filled_in() -> None:
    from services.outreach import _variables

    class Sending:
        async def hooks(self, lead_id):
            return []

    lead = SimpleNamespace(id=uuid4(), lead_intel_icp_id=None, first_name="Maya", last_name="Lane", title="Owner", company="Glow", website="glow.com")
    filled = set(asyncio.run(_variables(SimpleNamespace(workspace_id=uuid4(), sending=Sending()), None, lead, 2, {1: "Hi Maya"})))
    for kind in ("linkedin_invite", "linkedin_message", "instagram_message"):
        d = DEFAULTS[kind]
        used = set(PLACEHOLDER.findall(d.system_prompt + d.user_prompt))
        assert used <= filled, (kind, used - filled)
        assert "—" not in d.system_prompt + d.user_prompt and "–" not in d.system_prompt + d.user_prompt


def test_earlier_messages_reach_the_prompt() -> None:
    from services.outreach import _variables

    class Sending:
        async def hooks(self, lead_id):
            return []

    lead = SimpleNamespace(id=uuid4(), lead_intel_icp_id=None, first_name="", last_name="", title="", company="", website="")
    deps = SimpleNamespace(workspace_id=uuid4(), sending=Sending())
    assert "first" in asyncio.run(_variables(deps, None, lead, 1, {}))["social.earlier"]
    later = asyncio.run(_variables(deps, None, lead, 3, {1: "Hi Maya, saw your post", 2: "Quick one"}))["social.earlier"]
    assert "Message 1:\nHi Maya, saw your post" in later and "Message 2:\nQuick one" in later


def test_unipile_sends_invites_and_messages_the_documented_way() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("content-type", ""), request.content.decode()))
        if request.url.path.startswith("/api/v1/users/") and request.method == "GET":
            assert request.url.params["account_id"] == "acc_1"
            return httpx.Response(200, json={"provider_id": "ACoAA123"})
        if request.url.path == "/api/v1/chats":
            return httpx.Response(201, json={"object": "ChatStarted", "chat_id": "chat_9", "message_id": "m_1"})
        if request.url.path.endswith("/messages") and request.method == "GET":
            return httpx.Response(200, json={"items": [{"is_sender": 1, "sender_id": "me"}, {"is_sender": 0, "sender_id": "ACoAA123"}]})
        return httpx.Response(201, json={"message_id": "m_2"})

    u = UnipileClient("https://api8.unipile.com:13851", "k", transport=httpx.MockTransport(handler))

    async def run():
        pid = await u.provider_id("acc_1", "maya-lane")
        await u.invite("acc_1", pid, "Hi Maya, would love to connect.")
        chat = await u.start_chat("acc_1", pid, "Thanks for connecting")
        await u.send_in_chat("chat_9", "One more thing")
        return pid, chat, await u.they_replied("chat_9", pid)

    pid, chat, replied = asyncio.run(run())
    assert pid == "ACoAA123" and chat == ("chat_9", "m_1") and replied
    invite = next(s for s in seen if s[1] == "/api/v1/users/invite")
    assert json.loads(invite[3]) == {"account_id": "acc_1", "provider_id": "ACoAA123", "message": "Hi Maya, would love to connect."}
    started = next(s for s in seen if s[1] == "/api/v1/chats")
    assert "attendees_ids=ACoAA123" in started[3] and "account_id=acc_1" in started[3]


def test_a_chat_with_only_our_messages_is_not_a_reply() -> None:
    u = UnipileClient("https://x", "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"items": [{"is_sender": 1, "sender_id": "me"}]})))
    assert asyncio.run(u.they_replied("chat_1", "ACoAA123")) is False
