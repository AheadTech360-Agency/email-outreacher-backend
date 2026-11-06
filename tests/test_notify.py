"""The reply email to the team (services/notify.py): who it goes to, what it says, and that it never breaks anything."""

import asyncio
from types import SimpleNamespace

import services.notify as notify
from config import Settings


def settings(**extra) -> Settings:
    base = {"smtp_host": "smtp.example.com", "smtp_port": 587, "smtp_username": "alerts@example.com", "smtp_password": "x",
            "email_from_name": "Coldbrew", "notify_emails": "a@example.com, b@example.com", "authorized_urls": "https://app.example.com"}  # fmt: skip
    return Settings(_env_file=None, **(base | extra))


LEAD = SimpleNamespace(id=1, email="Ana@Shop.com", first_name="Ana", last_name="Ruiz", company="Shop & Co")


def test_the_email_names_who_replied_and_links_to_their_conversation() -> None:
    msg = notify.reply_message(settings(), LEAD, "Coldbrew: DTF")
    assert msg["Subject"] == "New reply from Ana Ruiz at Shop & Co"
    assert msg["To"] == "a@example.com, b@example.com" and "alerts@example.com" in msg["From"]
    text = msg.get_body(("plain",)).get_content()
    assert "https://app.example.com/replies?open=ana%40shop.com" in text and "Campaign: Coldbrew: DTF" in text
    assert "Shop &amp; Co" in msg.get_body(("html",)).get_content()  # escaped in the HTML part


def test_app_url_wins_over_the_authorized_urls() -> None:
    assert notify.reply_link(settings(app_url="https://coldbrew.example.com/"), "a@b.com") == "https://coldbrew.example.com/replies?open=a%40b.com"


def test_nothing_is_sent_unless_smtp_and_recipients_are_set() -> None:
    assert notify.configured(settings())
    assert not notify.configured(settings(notify_emails=" "))
    assert not notify.configured(settings(smtp_password=None))


def test_a_mail_server_failure_is_logged_never_raised(monkeypatch) -> None:
    def down(*_):
        raise OSError("connection refused")

    monkeypatch.setattr(notify, "_send", down)
    assert asyncio.run(notify.reply_received(LEAD, settings=settings())) is False


def test_it_sends_through_smtp_when_set_up(monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(notify, "_send", lambda s, msg: sent.append(msg["Subject"]))
    assert asyncio.run(notify.reply_received(LEAD, settings=settings())) is True and sent == ["New reply from Ana Ruiz at Shop & Co"]
