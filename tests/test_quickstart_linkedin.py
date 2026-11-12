"""Quick Start's LinkedIn option: find on LinkedIn, search the web, verify, people (LinkedIn, then the web), leads, posts,
and a brief per company at the end. No email drafts. Plus the web search's matching rules."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from services import quickstart
from services.apify import employee_from_profile, company_keys, found_company
from services.searxng import Hit, SearchError, SearxClient, pick_facebook, pick_instagram, pick_profiles, pick_website
from tests.test_quickstart import World, new_run, run_all  # noqa: F401  (the world fixture's pieces)
from tests.test_quickstart import world  # noqa: F401  (pytest fixture)


class LinkedInWorld:
    """The Google Maps world's answers, plus the LinkedIn option's endpoints."""

    def __init__(self, w: World, web: bool = True, people_on_linkedin: bool = True) -> None:
        self.w, self.web, self.people_on_linkedin = w, web, people_on_linkedin
        self.briefed: list[str] = []

    async def job(self, session, run, path, body, label):
        w = self.w
        if path == "/pipeline/find-linkedin":
            w.calls.append(path)
            run.job_ids = [*run.job_ids, uuid4()]
            assert body["searchTerms"] == ["screen printing"] and body["maxCompanies"] == run.settings["companies"]
            return True, {"added": 3, "duplicates": 1, "companyIds": [str(i) for i in w.companies], "notes": []}, None
        if path == "/pipeline/web-search":
            w.calls.append(path)
            if not self.web:
                return True, {"searched": 0, "websites": 0, "instagram": 0, "facebook": 0, "notes": ["Web search isn't set up (SEARXNG_URL), so it was skipped."]}, None
            return True, {"searched": len(body["companyIds"]), "websites": 2, "instagram": 1, "facebook": 0, "notes": []}, None
        if path == "/pipeline/stakeholders" and not self.people_on_linkedin:
            w.calls.append(path)
            run.job_ids = [*run.job_ids, uuid4()]
            return True, {"results": [], "error": None}, None
        if path == "/pipeline/people-web":
            w.calls.append(path)
            for cid in body["companyIds"]:
                c = next(c for c in w.companies.values() if str(c.id) == cid)
                p = SimpleNamespace(id=uuid4(), company_id=c.id, lead_id=None, linkedin_url="https://linkedin.com/in/y", email=None, email_status=None)
                w.people[p.id] = p
            return True, {"companies": len(body["companyIds"]), "profiles": len(body["companyIds"]), "new": len(body["companyIds"]), "notes": []}, None
        if path == "/pipeline/briefs":
            w.calls.append(path)
            self.briefed += body["companyIds"]
            return True, {"written": len(body["companyIds"]), "failed": 0}, None
        return await w.job(session, run, path, body, label)


def linkedin_run(world, monkeypatch, **kw):
    lw = LinkedInWorld(world, **kw)
    monkeypatch.setattr(quickstart, "_job", lw.job)
    run = new_run(source="linkedin", only_with_email=False)
    return run, lw


def test_a_linkedin_run_ends_with_a_brief_for_every_qualified_company(world, monkeypatch) -> None:
    run, lw = linkedin_run(world, monkeypatch)
    run_all(run, world)
    assert world.calls == ["/pipeline/find-linkedin", "/pipeline/web-search", "/pipeline/research", "/pipeline/stakeholders",
                           "/stakeholders/add-as-leads", "/posts/fetch", "/pipeline/briefs"]  # fmt: skip
    assert "/emails/generate" not in world.calls  # analysis only: no drafts
    assert quickstart.steps_for(run.settings) == ["find", "google", "research", "people", "leads", "posts", "analysis"]
    assert all(run.progress[s]["state"] == "done" for s in quickstart.steps_for(run.settings))
    qualified = {str(c.id) for c in world.companies.values() if c.status == "qualified"}
    assert set(lw.briefed) == qualified and len(qualified) == 2
    assert run.progress["google"]["note"] == "2 websites, 1 Instagram and 0 Facebook pages found"
    assert run.detail.startswith("Done: 2 companies are analysed below")


def test_people_linkedin_misses_are_looked_for_on_the_web(world, monkeypatch) -> None:
    run, _ = linkedin_run(world, monkeypatch, people_on_linkedin=False)
    run_all(run, world)
    assert world.calls.index("/pipeline/people-web") > world.calls.index("/pipeline/stakeholders")
    assert world.calls[-1] == "/pipeline/briefs"


def test_without_web_search_the_run_skips_it_and_still_verifies(world, monkeypatch) -> None:
    run, _ = linkedin_run(world, monkeypatch, web=False)
    run_all(run, world)
    assert run.progress["google"]["state"] == "skipped" and "SEARXNG_URL" in run.progress["google"]["note"]
    assert world.calls.count("/pipeline/web-search") == 1 and "/pipeline/research" in world.calls


def test_other_runs_keep_their_steps() -> None:
    assert quickstart.steps_for({"source": "google_maps"}) == ["find", "research", "people", "leads", "posts", "write"]
    assert quickstart.steps_for({"source": "existing"}) == ["find", "research", "website", "people", "leads", "profiles", "posts", "write"]


# ---------- Web search matching ----------


def test_the_companys_own_site_not_a_directory() -> None:
    hits = [Hit("https://www.yelp.com/biz/ink-co-austin", "Ink Co - Yelp", ""), Hit("https://www.inkco.com/", "Ink Co | Screen printing in Austin", ""),
            Hit("https://other.com", "Unrelated", "")]  # fmt: skip
    assert pick_website("Ink Co", hits) == "inkco.com"
    assert pick_website("Ink Co", [Hit("https://random.com", "Something else", "nothing")]) is None


def test_socials_are_profiles_that_name_the_company() -> None:
    ig = [Hit("https://www.instagram.com/p/abc/", "Ink Co post", ""), Hit("https://www.instagram.com/inkco.atx/", "Ink Co (@inkco.atx)", "")]
    assert pick_instagram("Ink Co", ig) == "https://www.instagram.com/inkco.atx/"
    fb = [Hit("https://www.facebook.com/groups/x", "Ink Co group", ""), Hit("https://www.facebook.com/InkCoATX/", "Ink Co | Austin", "")]
    assert pick_facebook("Ink Co", fb) == "https://www.facebook.com/InkCoATX"


def test_linkedin_profiles_that_name_the_company() -> None:
    hits = [Hit("https://www.linkedin.com/in/jane-doe-123/", "Jane Doe - Owner - Ink Co | LinkedIn", ""),
            Hit("https://www.linkedin.com/in/bob/", "Bob - Owner - Other Shop | LinkedIn", ""),
            Hit("https://www.linkedin.com/company/ink-co/", "Ink Co | LinkedIn", "")]  # fmt: skip
    assert pick_profiles("Ink Co", hits, 5) == ["https://www.linkedin.com/in/jane-doe-123/"]


def test_searxng_needs_json_on() -> None:
    def refuse(request):
        return httpx.Response(403)

    client = SearxClient("http://searx.local", pause=0, transport=httpx.MockTransport(refuse))
    with pytest.raises(SearchError, match="json"):
        asyncio.run(client.search("x"))

    def answer(request):
        assert request.url.params["format"] == "json"
        return httpx.Response(200, json={"results": [{"url": "https://inkco.com", "title": "Ink Co", "content": ""}]})

    client = SearxClient("http://searx.local", pause=0, transport=httpx.MockTransport(answer))
    assert asyncio.run(client.search("ink co"))[0].url == "https://inkco.com"


# ---------- LinkedIn results ----------


def test_a_linkedin_search_result_becomes_a_company() -> None:
    c = found_company({"name": "Ink Co", "linkedinUrl": "https://www.linkedin.com/company/ink-co", "website": "https://inkco.com",
                       "employeeCount": 12, "industries": [{"name": "Printing Services"}],
                       "locations": [{"headquarter": True, "parsed": {"city": "Austin", "state": "Texas", "country": "US"}}]})  # fmt: skip
    assert (c.name, c.city, c.state, c.employees, c.industry) == ("Ink Co", "Austin", "Texas", 12, "Printing Services")
    assert found_company({"name": ""}) is None


def test_a_profile_from_the_web_counts_only_if_they_work_there_now() -> None:
    keys = company_keys("Ink Co", "inkco.com")
    now = {"publicIdentifier": "jane", "firstName": "Jane", "lastName": "Doe", "linkedinUrl": "https://linkedin.com/in/jane",
           "experience": [{"companyName": "Ink Co", "position": "Owner", "endDate": None}]}  # fmt: skip
    gone = {"publicIdentifier": "bob", "firstName": "Bob", "experience": [{"companyName": "Ink Co", "position": "Owner", "endDate": {"text": "2021"}}]}
    assert employee_from_profile(now, keys, None).title == "Owner"
    assert employee_from_profile(gone, keys, None) is None
