"""The emails list, its pages and its counts: narrowed in the database, so only what a page shows is read."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from crud import EmailFilters, EmailRepo
from models import Email, Lead
from routers import emails as er

WS = uuid4()


class Emails:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def list(self, ws, status=None, lead_id=None, *, statuses=None, icp_id=None, limit=None, offset=0):
        self.calls.append(("list", status, lead_id, statuses, icp_id, limit, offset))
        return []

    async def page(self, ws, f, limit, offset, now):
        self.calls.append(("page", f.statuses, f.search, limit, offset))
        return [], 120

    async def breakdown(self, ws, f, now):
        return {"draft": 3, "sent": 117}, {"unrated": 100, "good": 20}

    async def counts(self, ws, icp_id=None):
        return {"draft": 3, "sent": 117}

    async def insights(self, lead_ids):
        return {}

    async def posts(self, post_ids):
        return {}


class Feedback:
    async def for_emails(self, ids):
        return {}


class NoLeadIntel:
    async def list_icps(self):
        raise AssertionError("ICP names are read only when Feedback searches")


def filters(**kw) -> EmailFilters:
    base = dict(statuses=None, lead_id=None, icp_id=None, icp=None, search=None, terms=None, score_band=None, hook=None, flags=None,
                rating=None, rated=None, written=None, tag=None, sort="newest")  # fmt: skip
    return er.email_filters(**{**base, **kw, "statuses": ",".join(kw["statuses"]) if kw.get("statuses") else None})


def test_a_page_reads_one_page_and_counts_every_tab() -> None:
    repo = Emails()
    page = asyncio.run(er.page_emails(filters(statuses=["draft", "edited"], search="hey"), 50, 100, WS, repo, Feedback(), NoLeadIntel()))
    assert (page.total, page.limit, page.offset, page.items) == (120, 50, 100, [])
    assert (page.statuses, page.ratings) == ({"draft": 3, "sent": 117}, {"unrated": 100, "good": 20})
    assert repo.calls == [("page", ["draft", "edited"], "hey", 50, 100)]


def test_the_list_narrows_by_status_lead_and_icp() -> None:
    repo, lead = Emails(), uuid4()
    asyncio.run(er.list_emails(None, "approved", lead, None, WS, repo, Feedback()))
    assert repo.calls == [("list", None, lead, ["approved"], None, None, 0)]


def test_counts_are_one_number_per_status() -> None:
    assert asyncio.run(er.email_counts(None, WS, Emails())) == {"draft": 3, "sent": 117}


def sql(f: EmailFilters, **kw) -> str:
    query = EmailRepo(None)._filtered(WS, f, datetime(2026, 11, 12, tzinfo=UTC), **kw)
    return str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))


def test_review_and_feedback_filters_become_sql() -> None:
    review = sql(EmailFilters(statuses=["draft", "edited"], icp="none", search="hey", score_band="high", hook="yes", flags="no",
                              rating="unrated", written="7"))  # fmt: skip
    for part in ("eo_emails.status IN", "eo_leads.lead_intel_icp_id IS NULL", "ILIKE", "CASE WHEN", "cardinality", "eo_email_feedback.id IS NULL"):
        assert part in review, part
    # The tab counts drop the tab (statuses, or Feedback's rating tab) but keep Review's rating filter.
    counts = sql(EmailFilters(statuses=["draft"], rating="good", rated="bad"), statuses=False, rating=False)
    assert "eo_emails.status IN" not in counts and counts.count("eo_email_feedback.rating =") == 1


def test_feedback_search_matches_tag_labels_and_icp_names() -> None:
    icp = uuid4()
    q = sql(EmailFilters(terms="generic acme", icp_names={icp: "Acme Retail"}))
    assert "&&" in q  # "generic" is in the label of the too_generic tag
    assert "eo_leads.lead_intel_icp_id IN" in q  # "acme" is in an ICP's name


def test_feedback_sorts_keep_newest_first_on_ties() -> None:
    order = EmailRepo(None)._order(EmailFilters(sort="score-low"))
    assert [str(o.compile(dialect=postgresql.dialect())).replace("outreach.", "") for o in order][1:] == ["eo_emails.created_at DESC", "eo_emails.id"]


def test_sequence_raw_is_left_out_of_every_email_read() -> None:
    # Written by every follow-up check, never read: it isn't part of a plain select of the email.
    sql_text = str(select(Email).compile(dialect=postgresql.dialect()))
    assert "sequence_raw" not in sql_text and "sequence_synced_at" in sql_text


def test_the_filters_become_sql() -> None:
    where = EmailRepo._first_emails(WS, ["draft", "edited"], None, "none")
    text = str(select(Email, Lead).join(Lead, Lead.id == Email.lead_id).where(*where).compile(dialect=postgresql.dialect()))
    assert "eo_emails.step" in text and "eo_emails.status IN" in text and "eo_leads.lead_intel_icp_id IS NULL" in text
