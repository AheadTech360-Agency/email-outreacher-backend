"""The emails list, its pages and its counts: narrowed in the database, so only what a page shows is read."""

import asyncio
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from crud import EmailRepo
from models import Email, Lead
from routers import emails as er

WS = uuid4()


class Emails:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def list(self, ws, status=None, lead_id=None, *, statuses=None, icp_id=None, limit=None, offset=0):
        self.calls.append(("list", status, lead_id, statuses, icp_id, limit, offset))
        return []

    async def count(self, ws, statuses, lead_id, icp_id):
        self.calls.append(("count", statuses, lead_id, icp_id))
        return 120

    async def counts(self, ws, icp_id=None):
        return {"draft": 3, "sent": 117}

    async def insights(self, lead_ids):
        return {}

    async def posts(self, post_ids):
        return {}


class Feedback:
    async def for_emails(self, ids):
        return {}


def test_a_page_reads_one_page_of_the_asked_statuses_and_counts_them_all() -> None:
    repo = Emails()
    page = asyncio.run(er.page_emails("draft,edited", None, "none", 50, 100, WS, repo, Feedback()))
    assert (page.total, page.limit, page.offset, page.items) == (120, 50, 100, [])
    assert repo.calls == [("list", None, None, ["draft", "edited"], "none", 50, 100), ("count", ["draft", "edited"], None, "none")]


def test_the_list_narrows_by_status_lead_and_icp() -> None:
    repo, lead = Emails(), uuid4()
    asyncio.run(er.list_emails(None, "approved", lead, None, WS, repo, Feedback()))
    assert repo.calls == [("list", None, lead, ["approved"], None, None, 0)]


def test_counts_are_one_number_per_status() -> None:
    assert asyncio.run(er.email_counts(None, WS, Emails())) == {"draft": 3, "sent": 117}


def test_the_filters_become_sql() -> None:
    where = EmailRepo._first_emails(WS, ["draft", "edited"], None, "none")
    sql = str(select(Email, Lead).join(Lead, Lead.id == Email.lead_id).where(*where).compile(dialect=postgresql.dialect()))
    assert "eo_emails.step" in sql and "eo_emails.status IN" in sql and "eo_leads.lead_intel_icp_id IS NULL" in sql
    icp = uuid4()
    sql = str(select(Email).join(Lead, Lead.id == Email.lead_id).where(*EmailRepo._first_emails(WS, None, None, str(icp))).compile(dialect=postgresql.dialect()))
    assert "eo_leads.lead_intel_icp_id =" in sql and "status IN" not in sql


def test_sequence_raw_is_left_out_of_every_email_read() -> None:
    # Written by every follow-up check, never read: it isn't part of a plain select of the email.
    sql = str(select(Email).compile(dialect=postgresql.dialect()))
    assert "sequence_raw" not in sql and "sequence_synced_at" in sql
