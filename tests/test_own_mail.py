"""Coldbrew's own sending, the parts with no database (services/own_mail.py)."""

import random
from datetime import UTC, datetime
from email import message_from_bytes, policy
from zoneinfo import ZoneInfo

import pytest

from services.own_mail import (
    MissingValue, Outgoing, build_message, classify, clean_schedule, in_window, next_send_time, own_words, render, reply_subject,
    unknown_variables,
)  # fmt: skip

NY = ZoneInfo("America/New_York")
SCHEDULE = clean_schedule({"timezone": "America/New_York", "start": "09:00", "end": "17:00", "days": [1, 2, 3, 4, 5, 6]})


# ---------- Text ----------


def test_variables_fill_in_and_fallbacks_cover_empty_ones() -> None:
    assert render("Hi {{firstName|there}}, {{ company }}", {"firstName": "", "company": "Ink Co"}) == "Hi there, Ink Co"
    with pytest.raises(MissingValue) as missing:
        render("Hi {{firstName}}", {"firstName": "  "})
    assert missing.value.name == "firstName"


def test_a_typo_in_a_variable_is_caught_when_saving() -> None:
    assert unknown_variables("{{firstName}} {{frstName}} {{email1Body}}") == ["frstName"]


# ---------- The email ----------


def test_a_follow_up_threads_under_the_first_email_and_can_be_unsubscribed_from() -> None:
    msg, message_id = build_message(Outgoing("Sarah Lane", "sarah@acme.com", "ana@shop.com", "Re: idea", "Body", "Sarah\nAcme", "<first@acme.com>"))
    assert message_id.endswith("@acme.com>") and msg["Message-ID"] == message_id
    assert msg["In-Reply-To"] == msg["References"] == "<first@acme.com>"
    assert msg["From"] == "Sarah Lane <sarah@acme.com>" and msg["List-Unsubscribe"] == "<mailto:sarah@acme.com?subject=unsubscribe>"
    assert msg.get_content().strip() == "Body\n\nSarah\nAcme"
    assert reply_subject("Quick idea") == "Re: Quick idea" and reply_subject("RE: x") == "RE: x"


# ---------- When ----------


def test_the_window_holds_its_hours_days_and_timezone() -> None:
    assert in_window(SCHEDULE, datetime(2026, 11, 10, 10, 0, tzinfo=NY))  # Tuesday 10:00 in New York
    assert not in_window(SCHEDULE, datetime(2026, 11, 10, 18, 0, tzinfo=NY))  # after hours
    assert not in_window(SCHEDULE, datetime(2026, 11, 15, 10, 0, tzinfo=NY))  # Sunday


def test_the_next_send_is_inside_the_next_window() -> None:
    rng = random.Random(1)
    saturday_night = datetime(2026, 11, 14, 20, 0, tzinfo=NY)
    for _ in range(50):
        at = next_send_time(SCHEDULE, saturday_night, rng).astimezone(NY)
        assert at.isoweekday() == 1 and 9 <= at.hour < 17  # Sunday is off: Monday, in hours
    noon = datetime(2026, 11, 10, 12, 0, tzinfo=NY)
    assert noon <= next_send_time(SCHEDULE, noon, rng).astimezone(NY) < datetime(2026, 11, 10, 17, 0, tzinfo=NY)


def test_a_bad_window_is_refused() -> None:
    with pytest.raises(ValueError):
        clean_schedule({"start": "17:00", "end": "09:00"})
    with pytest.raises(ValueError):
        clean_schedule({"days": []})
    with pytest.raises(Exception):
        clean_schedule({"timezone": "Mars/Base"})


# ---------- What came in ----------


def parse(raw: str):
    return message_from_bytes(raw.replace("\n", "\r\n").encode(), policy=policy.default)


def test_a_reply_points_at_our_email_and_keeps_only_their_words() -> None:
    m = classify(parse("""Message-ID: <r1@shop.com>
From: Ana <ana@shop.com>
Subject: Re: Quick idea
In-Reply-To: <first@acme.com>
Date: Tue, 10 Nov 2026 15:00:00 +0000

Sounds good, call me Thursday.

On Mon, Sarah wrote:
> Quick idea for Ink Co
"""))
    assert (m.type, m.from_email, m.refers_to) == ("reply", "ana@shop.com", ["<first@acme.com>"])
    assert m.snippet == "Sounds good, call me Thursday." and m.received_at == datetime(2026, 11, 10, 15, tzinfo=UTC)


def test_bounces_auto_replies_and_unsubscribes_are_told_apart() -> None:
    bounce = classify(parse("""Message-ID: <b1@mx.google.com>
From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
Subject: Delivery Status Notification (Failure)
X-Failed-Recipients: gone@shop.com

Address not found.
"""))
    assert bounce.type == "bounce" and bounce.failed_recipient == "gone@shop.com"
    ooo = classify(parse("Message-ID: <o1@shop.com>\nFrom: ana@shop.com\nSubject: Out of Office: back Monday\nAuto-Submitted: auto-replied\n\nAway.\n"))
    assert ooo.type == "auto_reply"
    stop = classify(parse("Message-ID: <u1@shop.com>\nFrom: ana@shop.com\nSubject: Re: idea\n\nPlease remove me from your list.\n"))
    assert stop.type == "unsubscribe"
    assert classify(parse("From: ana@shop.com\nSubject: hi\n\nno id\n")) is None


def test_own_words_drops_the_quoted_email() -> None:
    assert own_words("Yes please\n\n> earlier\n> text") == "Yes please"


# ---------- Warm-up mail and {{followUp}} ----------


def test_warm_up_mail_is_never_a_reply() -> None:
    by_header = parse("Message-ID: <w1@x.com>\nFrom: bot@warm.io\nSubject: Re: quick question\nX-Warmup-Id: 81723\nIn-Reply-To: <first@acme.com>\n\nThanks!\n")
    assert classify(by_header) is None
    by_marker = parse("Message-ID: <w2@x.com>\nFrom: bot@warm.io\nSubject: Re: catching up W4RM-7Q\n\nSounds good.\n")
    assert classify(by_marker, warmup_markers=["w4rm-7q"]) is None
    assert classify(by_marker).type == "reply"  # without the marker set, it reads as a reply
    real = parse("Message-ID: <r9@shop.com>\nFrom: ana@shop.com\nSubject: Re: idea\n\nWe warm up our inboxes too, but yes, let's talk.\n")
    assert classify(real, warmup_markers=["W4RM-7Q"]).type == "reply"


def test_followup_goes_only_in_emails_2_to_5() -> None:
    from fastapi import HTTPException

    from routers.campaigns import StepIn, _check_steps

    ok = [StepIn(subject="Hi", body="{{email1Body}}")] + [StepIn(body="{{followUp}}") for _ in range(4)]
    _check_steps(ok)
    with pytest.raises(HTTPException):
        _check_steps([StepIn(subject="Hi", body="{{followUp}}")])  # email 1
    with pytest.raises(HTTPException):
        _check_steps(ok + [StepIn(body="{{followUp}}")])  # email 6
    with pytest.raises(HTTPException):
        _check_steps([StepIn(subject="{{followUp}}", body="x")])  # in a subject
