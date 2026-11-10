"""The email warmer, the parts with no database or mailbox (services/warmup.py)."""

import random
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from types import SimpleNamespace

from services import warmup
from services.own_mail import classify


def account(**extra):
    return SimpleNamespace(**({"from_name": "Sarah Lane", "from_email": "sarah@acme.com", "warmup_daily_target": 20,
                               "warmup_started_at": None, "warmup_reply_percent": 30} | extra))  # fmt: skip


NOW = datetime(2026, 11, 10, 16, 0, tzinfo=UTC)  # 11 am in New York


def test_a_new_mailbox_ramps_up_to_its_target() -> None:
    assert warmup.target_today(account(warmup_started_at=NOW), NOW) == 3
    assert warmup.target_today(account(warmup_started_at=NOW - timedelta(days=4)), NOW) == 11
    assert warmup.target_today(account(warmup_started_at=NOW - timedelta(days=60)), NOW) == 20  # never past the target


def test_sends_stay_in_the_window_and_stop_at_the_days_target() -> None:
    rng = random.Random(3)
    assert not warmup.send_now(0, 10, NOW.replace(hour=4), 5, rng)  # night in the US
    assert not warmup.send_now(10, 10, NOW, 5, rng)  # done for the day


def test_the_days_sends_are_spread_across_the_window() -> None:
    rng = random.Random(7)
    sent, times = 0, []
    t = NOW.replace(hour=13, minute=0)
    while t.hour < 23:
        if warmup.send_now(sent, 10, t, 5, rng):
            sent += 1
            times.append(t.hour)
        t += timedelta(minutes=5)
    assert 6 <= sent <= 10 and len(set(times)) >= 4  # about the target, over several hours, not in one burst


def test_a_warm_up_email_reads_naturally_and_is_never_taken_for_a_campaign_reply() -> None:
    rng = random.Random(1)
    sender, to = account(), account(from_name="Tom Reyes", from_email="tom@acme-mail.com")
    subject, body = warmup.compose(sender, to, rng)
    msg, message_id = warmup.build(sender, to, subject, body, in_reply_to="<first@acme.com>")
    assert body.split(",")[0].endswith("Tom") and body.rstrip().endswith("Sarah")
    assert msg[warmup.HEADER] and msg["In-Reply-To"] == "<first@acme.com>" and message_id.endswith("@acme.com>")
    received = message_from_bytes(msg.as_bytes(), policy=policy.default)
    assert classify(received) is None  # the campaign reply reader drops it by its warm-up header


def test_the_workspace_settings_set_the_ramp_and_the_window() -> None:
    from services.own_settings import Settings

    s = Settings(warmup_start_per_day=5, warmup_ramp_per_day=5, warmup_timezone="Europe/London", warmup_start="08:00", warmup_end="12:00", warmup_days=[1, 2, 3, 4, 5])
    assert warmup.target_today(account(warmup_started_at=NOW - timedelta(days=2)), NOW, s) == 15
    rng = random.Random(2)
    tuesday_9am_london = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    assert any(warmup.send_now(0, 20, tuesday_9am_london, 5, rng, s.warmup_window) for _ in range(20))
    assert not warmup.send_now(0, 20, tuesday_9am_london.replace(hour=13), 5, rng, s.warmup_window)  # after 12:00 London
    assert not warmup.send_now(0, 20, datetime(2026, 11, 14, 9, 0, tzinfo=UTC), 5, rng, s.warmup_window)  # Saturday is off
