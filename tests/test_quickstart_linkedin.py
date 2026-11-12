"""Quick Start's LinkedIn option: find on LinkedIn, search the web, verify, people (LinkedIn, then the web), leads, posts,
and a brief per company at the end. No email drafts. Plus the web search's matching rules."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from services import quickstart
from services.apify import employee_from_profile, company_keys, found_company
from services.websearch import BraveSearch, GoogleSearch, Hit, domain_is_named, google_hits, pick_facebook, pick_instagram, pick_profiles, pick_website
from tests.test_quickstart import World, new_run, run_all  # noqa: F401  (the world fixture's pieces)
from tests.test_quickstart import world  # noqa: F401  (pytest fixture)


class LinkedInWorld:
    """The Google Maps world's answers, plus the LinkedIn option's endpoints."""

    def __init__(self, w: World, web: bool = True, people_on_linkedin: bool = True) -> None:
        self.w, self.web, self.people_on_linkedin = w, web, people_on_linkedin
        self.briefed: list[str] = []
        self.engines: list[str] = []

    async def job(self, session, run, path, body, label):
        w = self.w
        if path == "/pipeline/find-linkedin":
            w.calls.append(path)
            run.job_ids = [*run.job_ids, uuid4()]
            assert body["searchTerms"] == ["screen printing"] and body["maxCompanies"] == run.settings["companies"]
            return True, {"added": 3, "duplicates": 1, "companyIds": [str(i) for i in w.companies], "notes": []}, None
        if path == "/pipeline/web-search":
            w.calls.append(path)
            self.engines.append(body["engine"])
            if not self.web:
                return True, {"searched": 0, "websites": 0, "instagram": 0, "facebook": 0, "notes": ["Web search isn't set up (BRAVE_API_KEY), so it was skipped."]}, None
            return True, {"searched": len(body["companyIds"]), "websites": 2, "instagram": 1, "facebook": 0, "remaining": 960, "notes": []}, None
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


def linkedin_run(world, monkeypatch, settings=None, **kw):
    lw = LinkedInWorld(world, **kw)
    monkeypatch.setattr(quickstart, "_job", lw.job)
    run = new_run(source="linkedin", only_with_email=False, **(settings or {}))
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
    assert run.progress["google"]["note"] == "2 websites, 1 Instagram and 0 Facebook pages found. 960 free Brave searches left this month"
    assert lw.engines == ["brave"]  # the default
    assert run.detail.startswith("Done: 2 companies are analysed below")


def test_people_linkedin_misses_are_looked_for_on_the_web(world, monkeypatch) -> None:
    run, _ = linkedin_run(world, monkeypatch, people_on_linkedin=False)
    run_all(run, world)
    assert world.calls.index("/pipeline/people-web") > world.calls.index("/pipeline/stakeholders")
    assert world.calls[-1] == "/pipeline/briefs"


def test_without_web_search_the_run_skips_it_and_still_verifies(world, monkeypatch) -> None:
    run, _ = linkedin_run(world, monkeypatch, web=False)
    run_all(run, world)
    assert run.progress["google"]["state"] == "skipped" and "BRAVE_API_KEY" in run.progress["google"]["note"]
    assert world.calls.count("/pipeline/web-search") == 1 and "/pipeline/research" in world.calls


def test_a_run_can_search_google_through_apify(world, monkeypatch) -> None:
    run, lw = linkedin_run(world, monkeypatch, settings={"web_search": "google"})
    run_all(run, world)
    assert lw.engines == ["google"]


def test_other_runs_keep_their_steps() -> None:
    assert quickstart.steps_for({"source": "google_maps"}) == ["find", "research", "people", "leads", "posts", "write"]
    assert quickstart.steps_for({"source": "existing"}) == ["find", "research", "website", "people", "leads", "profiles", "posts", "write"]


# ---------- Web search matching ----------


def test_the_companys_own_site_not_a_directory() -> None:
    hits = [Hit("https://www.yelp.com/biz/ink-co-austin", "Ink Co - Yelp", ""), Hit("https://www.inkco.com/", "Ink Co | Screen printing in Austin", ""),
            Hit("https://other.com", "Unrelated", "")]  # fmt: skip
    assert pick_website("Ink Co", hits) == "inkco.com"
    assert pick_website("Ink Co", [Hit("https://random.com", "Something else", "nothing")]) is None


def test_knock_off_and_reseller_domains_are_not_the_website() -> None:
    # What a search for "Allbirds official website" answered: knock-off stores first.
    fakes = [Hit("https://www.allbirdsstores.com/", "Allbirds Official Store", ""), Hit("https://www.allbirds-official.com/", "Allbirds", ""),
             Hit("https://allbirdsofficial.us/", "Allbirds Shoes Sale", "")]  # fmt: skip
    assert pick_website("Allbirds", fakes) is None
    assert pick_website("Allbirds", [*fakes, Hit("https://www.allbirds.com/", "Allbirds: Sustainable Shoes", "")]) == "allbirds.com"
    # What Brave answered: another country's store pages aren't a US company's website; a subdomain gives the main site.
    assert pick_website("Allbirds", [Hit("https://stores.allbirds.ca/locations/sf", "Allbirds San Francisco", ""),
                                     Hit("https://www.allbirds.co.uk/pages/sf", "Allbirds", "")]) is None  # fmt: skip
    assert pick_website("Allbirds", [Hit("https://stores.allbirds.com/locations/sf", "Allbirds San Francisco", "")]) == "allbirds.com"
    assert domain_is_named("Ink Co", "shop.inkco.co.uk") and domain_is_named("Ink Co LLC", "inkcollc.com") and domain_is_named("Ink Co", "ink-co.com")
    assert domain_is_named("Black & White Prints", "blackandwhiteprints.com")
    assert not domain_is_named("Ink Co", "inkcostore.com") and not domain_is_named("Ink Co", "theinkco.com")


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


def brave(handler):
    return BraveSearch("key", pause=0, transport=httpx.MockTransport(handler))


def test_brave_search_results() -> None:
    def answer(request):
        assert request.headers["X-Subscription-Token"] == "key" and request.url.params["q"] == "ink co"
        return httpx.Response(200, json={"web": {"results": [{"url": "https://inkco.com", "title": "<strong>Ink Co</strong>", "description": "Prints &amp; more"}]}})

    out = asyncio.run(brave(answer).search_many(["ink co", "ink co"]))
    assert out.hits["ink co"] == [Hit("https://inkco.com", "Ink Co", "Prints & more")] and not out.notes


def test_brave_stops_at_the_months_free_searches(monkeypatch) -> None:
    from routers import pipeline

    class Calls:
        logged = []

        async def used_this_month(self, provider):
            assert provider == "brave"
            return 998

        async def record(self, *args, **kw):
            self.logged.append(kw["credits"])

    asked = []

    def answer(request):
        asked.append(request.url.params["q"])
        return httpx.Response(200, json={"web": {"results": []}})

    calls = Calls()
    out = asyncio.run(pipeline.run_searches(brave(answer), ["a", "b", "c", "d"], 10, calls, uuid4()))
    assert asked == ["a", "b"] and calls.logged == [2] and out.remaining == 0 and "1,000 free Brave searches are used up" in out.notes[0]


def test_brave_running_out_stops_the_searches_and_says_so() -> None:
    seen = []

    def answer(request):
        seen.append(request.url.params["q"])
        if len(seen) == 1:
            return httpx.Response(200, json={"web": {"results": []}})
        return httpx.Response(429, json={"type": "ErrorResponse"})

    out = asyncio.run(brave(answer).search_many(["a", "b", "c"]))
    assert seen == ["a", "b"] and list(out.hits) == ["a"] and "refused" in out.notes[0] and "Google" in out.notes[0]


def test_google_through_apify_runs_every_query_once() -> None:
    queries = ["ink co", 'site:instagram.com "Ink Co"']

    class FakeApify:
        async def run(self, actor, actor_input, max_items):
            assert actor == "apify~google-search-scraper" and actor_input["queries"] == "\n".join(queries)
            assert actor_input["maxPagesPerQuery"] == 1
            items = [{"searchQuery": {"term": queries[0]}, "organicResults": [{"url": "https://inkco.com", "title": "Ink Co", "description": ""}]},
                     {"searchQuery": {"term": queries[1]}, "organicResults": []}]  # fmt: skip
            return SimpleNamespace(items=items, cost_usd=0.009, run_id="r1")

    out = asyncio.run(GoogleSearch(FakeApify()).search_many([*queries, "ink co"]))
    assert out.hits["ink co"][0].url == "https://inkco.com" and out.hits[queries[1]] == [] and out.cost_usd == 0.009
    assert google_hits([{"searchQuery": {"term": "x"}}]) == {"x": []}


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


# ---------- LinkedIn's search answering nothing ----------


def test_an_empty_linkedin_search_is_asked_once_more_and_said_as_it_is() -> None:
    from routers import pipeline as pr
    from schemas import LinkedInFindIn

    asked = []

    class Apify:
        async def search_linkedin_companies(self, term, places, max_items):
            asked.append(max_items)
            return [], SimpleNamespace(items=[], cost_usd=0.001, run_id=None)

    class Companies:
        async def workspace_exists(self, workspace_id):
            return True

        async def add_linkedin(self, workspace_id, icp_id, found):
            return [], 0

    class Calls:
        async def record(self, *a, **kw):
            pass

    class LeadIntel:
        async def get_icp(self, icp_id):
            return {"id": icp_id, "name": "X"}

    body = LinkedInFindIn(icp_id=uuid4(), search_terms=["screen printing"], places=[], max_companies=3)
    out = asyncio.run(pr.find_on_linkedin(body, uuid4(), LeadIntel(), Companies(), Calls(), Apify()))
    assert asked == [6, 6]  # 3 wanted plus a few in case some are here already (not 10 more), asked twice
    assert out.added == 0 and "answered nothing" in out.notes[0] and "try again in a few minutes" in out.notes[0]


def test_a_facebook_page_listed_as_the_website_is_their_facebook_not_their_domain() -> None:
    fb = found_company({"name": "B&S Embroidery", "website": "https://www.facebook.com/BSEmbroidery"})
    assert (fb.website_url, fb.facebook_url) == (None, "https://www.facebook.com/BSEmbroidery")
    ig = found_company({"name": "Ink Co", "website": "instagram.com/inkco"})
    assert (ig.website_url, ig.instagram_url) == (None, "instagram.com/inkco")
    assert found_company({"name": "Ink Co", "website": "https://www.yelp.com/biz/ink-co"}).website_url is None
    assert found_company({"name": "Ink Co", "website": "https://inkco.com"}).website_url == "https://inkco.com"
