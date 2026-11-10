"""Coldbrew's own sending, phase 1: sender accounts and their checks (routers/senders.py, services/mail_check.py,
services/vault.py). Nothing here reaches a real mail server or Vault."""

import asyncio
import re
import smtplib
import socket
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from routers.senders import SenderIn, _imap_complete
from services import mail_check
from services.vault import PREFIX, secret_name

ROOT = Path(__file__).resolve().parents[1]

BASE = {"fromEmail": "Sarah@AheadTech360HQ.com", "smtpHost": "mail.privateemail.com", "smtpPort": 465, "smtpSecurity": "ssl",
        "smtpUsername": "sarah@aheadtech360hq.com", "smtpPassword": "app-pass"}  # fmt: skip


# ---------- What a sender account accepts ----------


def test_a_sender_needs_a_real_address_host_and_security() -> None:
    assert SenderIn.model_validate(BASE).from_email == "Sarah@AheadTech360HQ.com"
    for bad in ({"fromEmail": "not an email"}, {"smtpHost": "mail.example.com/evil"}, {"smtpSecurity": "none"}, {"smtpPort": 0},
                {"dailyLimit": 501}):  # fmt: skip
        with pytest.raises(ValidationError):
            SenderIn.model_validate(BASE | bad)


def test_imap_is_all_or_nothing() -> None:
    assert _imap_complete(SenderIn.model_validate(BASE)) is False
    full = BASE | {"imapHost": "mail.privateemail.com", "imapPort": 993, "imapSecurity": "ssl", "imapUsername": "sarah@x.com"}
    assert _imap_complete(SenderIn.model_validate(full)) is True
    with pytest.raises(HTTPException):
        _imap_complete(SenderIn.model_validate(BASE | {"imapHost": "mail.privateemail.com"}))


def test_a_blank_password_on_edit_means_keep_the_saved_one() -> None:
    assert SenderIn.model_validate(BASE | {"smtpPassword": "  "}).smtp_password is None


# ---------- The check only signs in, and only to public hosts ----------


def test_private_and_internal_hosts_are_refused(monkeypatch) -> None:
    for ip in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.2", "::1"):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, ip=ip: [(None, None, None, None, (ip, 0))])
        assert "private address" in mail_check.public_host("internal.example.com")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a: [(None, None, None, None, ("8.8.8.8", 0))])
    assert mail_check.public_host("mail.example.com") is None


def test_a_refused_password_says_to_use_an_app_password(monkeypatch) -> None:
    monkeypatch.setattr(mail_check, "public_host", lambda host: None)
    sent: list = []

    class Server:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, user, password): raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        def send_message(self, msg): sent.append(msg)  # never called: the check only signs in

    monkeypatch.setattr(smtplib, "SMTP_SSL", Server)
    error = mail_check.check_smtp(mail_check.Endpoint("smtp.gmail.com", 465, "ssl", "a@b.com", "x"))
    assert "app password" in error and sent == []


def test_a_good_sign_in_passes_and_sends_nothing(monkeypatch) -> None:
    monkeypatch.setattr(mail_check, "public_host", lambda host: None)
    seen: list = []

    class Server:
        def __init__(self, host, port, **k): seen.append((host, port))
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **k): seen.append("starttls")
        def login(self, user, password): seen.append("login")

    monkeypatch.setattr(smtplib, "SMTP", Server)
    result = asyncio.run(mail_check.check(mail_check.Endpoint("smtp.office365.com", 587, "starttls", "a@b.com", "x"), None))
    assert result.ok and not result.imap_checked and seen == [("smtp.office365.com", 587), "starttls", "login"]


# ---------- Vault: one file, sender secrets only ----------


def test_only_services_vault_touches_supabase_vault() -> None:
    files = [p for p in ROOT.glob("**/*.py") if not {".venv", "tests", "migrations"} & set(p.parts)]
    offenders = [str(p.relative_to(ROOT)) for p in files if p.name != "vault.py" and re.search(r"vault\.(secrets|create_secret|update_secret|decrypted_secrets)", p.read_text(encoding="utf-8"))]
    assert offenders == []
    source = (ROOT / "services" / "vault.py").read_text(encoding="utf-8")
    # A delete can only ever remove one of our own sender secrets, by id and name prefix.
    assert "delete from vault.secrets where id = :id and name like :prefix" in source


def test_secret_names_are_ours_and_unique_per_account_and_kind() -> None:
    from uuid import uuid4

    a = uuid4()
    assert secret_name(a, "smtp") == f"{PREFIX}/{a}/smtp" != secret_name(a, "imap")
