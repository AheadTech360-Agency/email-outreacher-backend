"""Reading many companies' websites at once (services/enrich.py)."""

import asyncio
from types import SimpleNamespace

from services.enrich import enrich_companies
from services.website import SiteRead


class Reader:
    async def read(self, domain: str) -> SiteRead:
        await asyncio.sleep(0)
        if domain == "broken.com":
            raise RuntimeError("something inside the reader broke")
        return SiteRead(domain=domain, outcome="ok", via="own_crawl", text="hi",
                        pages=[{"url": f"https://{domain}/", "role": "homepage", "status": 200, "text": "hi"}],
                        socials={"instagram": f"https://instagram.com/{domain.split('.')[0]}"})  # fmt: skip


def company(domain: str) -> SimpleNamespace:
    return SimpleNamespace(domain=domain, website_url=None, instagram_url=None, facebook_url=None, linkedin_url=None,
                           twitter_url=None, phone=None, research=None)  # fmt: skip


def test_one_site_failing_does_not_stop_the_others() -> None:
    companies = [company("a.com"), company("broken.com"), company("b.com")]
    saved, busy = [], []

    async def save_pages(c, via, pages):
        busy.append(1)
        assert len(busy) == 1  # never two saves at once: one database session
        await asyncio.sleep(0)
        saved.append(c.domain)
        busy.pop()

    async def nothing(*args):
        return 0

    out = asyncio.run(enrich_companies(companies, website=True, instagram=False, linkedin=False, reader=Reader(), apify=None,
                                       save_pages=save_pages, save_posts=nothing, record=nothing))  # fmt: skip
    assert (out.sites_read, out.sites_failed) == (2, 1)
    assert sorted(saved) == ["a.com", "b.com"]
    assert companies[0].instagram_url == "https://instagram.com/a" and companies[0].research["website"]["outcome"] == "ok"
