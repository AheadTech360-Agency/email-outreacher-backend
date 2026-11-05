"""Which of the five emails a reply came after (services/smart_analysis.py)."""

from services.smart_analysis import replies_by_email, reply_after


def row(replied, emails_sent):
    return {"replied": replied, "emails_sent": emails_sent}


def test_a_reply_counts_against_the_last_email_sent_before_it() -> None:
    assert reply_after(row(True, 3)) == 3
    assert reply_after(row(True, 0)) == 1  # replied before the count was synced: email 1
    assert reply_after(row(True, 9)) == 5
    assert reply_after(row(False, 4)) is None


def test_replies_are_counted_for_each_of_the_five_emails() -> None:
    rows = [row(True, 1), row(True, 1), row(True, 4), row(False, 5)]
    assert replies_by_email(rows) == [
        {"label": "Email 1", "replies": 2}, {"label": "Email 2", "replies": 0}, {"label": "Email 3", "replies": 0},
        {"label": "Email 4", "replies": 1}, {"label": "Email 5", "replies": 0},
    ]
