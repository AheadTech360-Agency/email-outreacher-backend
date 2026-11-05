import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from schemas import PostGatherIn
from services.smart_posts import Target, gather


def test_instagram_window_filters_cached_and_fetched_posts_without_changing_linkedin():
    now = datetime(2026, 11, 5, tzinfo=UTC)
    linkedin_since = now - timedelta(days=60)
    instagram_since = now - timedelta(days=7)
    company = SimpleNamespace(id=uuid4(), linkedin_url="https://linkedin.com/company/demo", instagram_url="https://instagram.com/demo", research={})
    def post(network, age, dated=True):
        return SimpleNamespace(source=network, posted_at=now-timedelta(days=age) if dated else None, owner_key="demo")
    cached = {"linkedin": [post("linkedin", 30)], "instagram": [post("instagram", 30), post("instagram", 0, False)]}
    calls, saved = [], []
    async def owned(owner, network):
        return cached[network]
    async def save(owner, found):
        saved.extend(found)
        for p in found:
            cached[p.source].append(p)
        return len(found)
    async def fetch(urls, limit, since):
        calls.append(since)
        return [post("instagram", 2), post("instagram", 20), post("instagram", 0, False)], SimpleNamespace(cost_usd=0)
    async def quiet(*args):
        pass
    result = asyncio.run(gather(targets=[Target(None, company)], stages=["company_linkedin", "company_instagram"], max_posts=1,
        since=linkedin_since, instagram_since=instagram_since, apify=SimpleNamespace(instagram_posts=fetch), claude=None,
        owner_posts=owned, save=save, record_apify=quiet, record_claude=quiet))
    assert calls == [instagram_since]
    assert len(saved) == 1 and saved[0].posted_at == now-timedelta(days=2)
    assert result.leads[0].counts == {"company_linkedin": 1, "company_instagram": 1}


@pytest.mark.parametrize("days", [0, 366, 1.5])
def test_instagram_days_are_validated(days):
    with pytest.raises(ValidationError):
        PostGatherIn(sources=["company_instagram"], instagram_max_age_days=days)


def test_instagram_window_is_optional_for_existing_clients():
    assert PostGatherIn(sources=["company_instagram"]).instagram_max_age_days is None
