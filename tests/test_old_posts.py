"""Posts older than the age limit are never stored, whichever path read them (crud.PostRepo._recent)."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from crud import PostRepo


class Session:
    """Answers the two limits: Settings' and the longest ICP one."""

    def __init__(self, setting, icp) -> None:
        self.answers = [setting, icp]

    async def scalar(self, _query):
        return self.answers.pop(0)


def post(days_old):
    when = None if days_old is None else datetime.now(UTC) - timedelta(days=days_old)
    return SimpleNamespace(posted_at=when, url=f"https://instagram.com/p/{days_old}")


def kept(setting, icp, ages):
    out = asyncio.run(PostRepo(Session(setting, icp))._recent(uuid4(), [post(a) for a in ages]))
    return sorted(p.url.rsplit("/", 1)[1] for p in out)


def test_posts_older_than_the_limit_or_undated_are_never_stored() -> None:
    assert kept(60, None, [1, 59, 61, 400, None]) == ["1", "59"]


def test_an_icp_with_a_longer_limit_keeps_its_posts() -> None:
    assert kept(60, 90, [59, 80, 120]) == ["59", "80"]


def test_without_a_setting_the_default_60_days_holds() -> None:
    assert kept(None, None, [30, 70]) == ["30"]
