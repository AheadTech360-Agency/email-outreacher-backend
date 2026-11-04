"""Filling in verified companies and people (services/enrich.py), with a fake Apify and a fake website reader."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from services import enrich
from services.apify import FetchedPost, InstagramProfile, RunResult
from services.website import SiteRead


def company(name, domain, **kw):
    defaults = dict(id=uuid4(), name=name, domain=domain, website_url=None, linkedin_url=None, instagram_url=None, facebook_url=None,
                    twitter_url=None, phone=None, research=None, raw={}, status="qualified", score=None)  # fmt: skip
    return SimpleNamespace(**(defaults | kw))


class Reader:
    async def read(self, domain, max_pages=4):
        return SiteRead(domain=domain, outcome="ok", via="own_crawl", resolved_url=f"https://{domain}/", text="We print shirts",
                        title=domain, pages=[{"url": f"https://{domain}/", "role": "homepage", "status": 200, "text": "We print shirts"}],
                        socials={"instagram": "https://instagram.com/inkco", "linkedin": "https://www.linkedin.com/company/inkco"})  # fmt: skip


class Apify:
    def __init__(self) -> None:
        self.calls = []

    async def instagram_profiles(self, handles):
        self.calls.append(("instagram", tuple(handles)))
        post = FetchedPost("instagram", "https://ig/1", None, None, "New drop", "image", False, "inkco", {})
        return {"inkco": InstagramProfile("inkco", "Ink Co", "Custom shirts", "Printing", 1200, False, False, None, {}, [post])}, RunResult([], 0.0023, "r1")

    async def linkedin_companies(self, urls):
        self.calls.append(("linkedin", tuple(urls)))
        page = {"name": "Ink Co", "description": "Screen printing in Austin", "employeeCount": 12, "foundedOn": {"year": 2014}}
        return {"inkco": page}, RunResult([], 0.004, "r2")

    async def linkedin_profiles(self, urls):
        self.calls.append(("profiles", tuple(urls)))
        return {"ava": {"about": "I run Ink Co", "experience": [{"position": "Owner", "companyName": "Ink Co"}]}}, RunResult([], 0.004, "r3")


def run(coro):
    return asyncio.run(coro)


async def nothing(*args, **kwargs):
    return 0


def test_a_verified_company_is_filled_in_without_being_judged() -> None:
    ink, apify, pages = company("Ink Co", "inkco.com"), Apify(), []

    async def save_pages(c, via, found):
        pages.append(c.id)

    out = run(enrich.enrich_companies([ink], website=True, instagram=True, linkedin=True, reader=Reader(), apify=apify,
                                      save_pages=save_pages, save_posts=lambda c, p: asyncio.sleep(0, 1), record=nothing))  # fmt: skip
    assert (out.sites_read, out.instagram, out.linkedin_companies, out.posts_saved) == (1, 1, 1, 1) and pages == [ink.id]
    assert ink.status == "qualified" and ink.score is None  # nothing judged
    assert ink.research["instagram"]["followers"] == 1200 and ink.research["linkedin"]["description"] == "Screen printing in Austin"
    assert (ink.raw["short_description"], ink.raw["founded_year"], ink.raw["estimated_num_employees"]) == ("Screen printing in Austin", 2014, 12)
    assert out.cost_usd == 0.0063
    # Run again: nothing is bought twice.
    again = run(enrich.enrich_companies([ink], website=False, instagram=True, linkedin=True, reader=Reader(), apify=apify,
                                        save_pages=save_pages, save_posts=lambda c, p: asyncio.sleep(0, 0), record=nothing))  # fmt: skip
    assert again.cost_usd == 0 and len(apify.calls) == 2


def test_sites_read_side_by_side_are_saved_one_at_a_time() -> None:
    """They share one database session, which refuses two statements at once: a Quick Start run crashed on it
    (2026-11-04). Saving waits on a slow write here, so overlapping saves would show."""
    companies = [company(f"Shop {i}", f"shop{i}.com") for i in range(8)]
    busy, overlapped, saved = False, False, []

    async def save_pages(c, via, found):
        nonlocal busy, overlapped
        overlapped = overlapped or busy
        busy = True
        await asyncio.sleep(0.01)
        saved.append(c.id)
        busy = False

    out = run(enrich.enrich_companies(companies, website=True, instagram=False, linkedin=False, reader=Reader(), apify=None,
                                      save_pages=save_pages, save_posts=nothing, record=nothing, progress=nothing))  # fmt: skip
    assert out.sites_read == 8 and len(saved) == 8 and not overlapped


def test_only_what_was_ticked_runs() -> None:
    ink, apify = company("Ink Co", "inkco.com", instagram_url="https://instagram.com/inkco"), Apify()
    out = run(enrich.enrich_companies([ink], website=False, instagram=True, linkedin=False, reader=Reader(), apify=apify,
                                      save_pages=nothing, save_posts=lambda c, p: asyncio.sleep(0, 0), record=nothing))  # fmt: skip
    assert [c[0] for c in apify.calls] == ["instagram"] and out.sites_read == 0


def test_a_persons_linkedin_profile_goes_into_their_record_once() -> None:
    ava = SimpleNamespace(linkedin_url="https://www.linkedin.com/in/ava", raw={"city": "Austin"})
    apify = Apify()
    out = run(enrich.enrich_people([ava], apify=apify, record=nothing))
    assert out.linkedin_people == 1 and ava.raw["about"] == "I run Ink Co" and ava.raw["city"] == "Austin"
    assert ava.raw["experience"][0]["position"] == "Owner" and "linkedin_profile_at" in ava.raw
    run(enrich.enrich_people([ava], apify=apify, record=nothing))
    assert len(apify.calls) == 1
