"""Research and scoring, with fake website reader, Apify and Claude. No network, no cost."""

import asyncio
import functools
from types import SimpleNamespace
from uuid import uuid4

import anthropic
import httpx

from models import Company
from services.apify import FetchedPost, InstagramProfile, RunResult
from services.research import research_companies
from services.prompts import DEFAULTS, render, verify_variables
from services.scoring import CompanyScore, CompanyScorer, PromptSpec
from services.website import SiteRead

ICP = {
    "name": "Screen printing", "industry": "Printing", "sources": [], "any_of": ["screen print"],
    "none_of": ["equipment"], "website_any_of": [], "instagram_any_of": [], "require_all": False,
    "min_followers": None, "max_followers": None, "requires_website": False, "min_reviews": None,
    "places": ["Chicago"], "search_terms": ["screen printing"],
}  # fmt: skip


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeReader:
    def __init__(self, sites: dict[str, SiteRead]) -> None:
        self.sites = sites
        self.read_calls: list[str] = []

    async def read(self, domain, max_pages=4):
        self.read_calls.append(domain)
        return self.sites.get(domain, SiteRead(domain=domain, outcome="unreachable", detail="down"))

    # These sites have no other pages, so Claude is never offered more to read.
    def can_read_more(self, site, max_pages=6, max_seconds=120):
        return False

    async def read_more(self, site, url, reason="", max_pages=6, max_seconds=120):
        return None


class FakeApify:
    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    async def instagram_profiles(self, handles):
        self.asked.append(handles)
        def post(h):
            return FetchedPost("instagram", f"https://www.instagram.com/p/{h}1/", "1", None, "New colours", "image", False, h, {})

        profiles = {
            h: InstagramProfile(h, h.title(), "Custom tees", "Printing service", 1200, False, False, None, {"id": h}, [post(h)])
            for h in handles
        }
        return profiles, RunResult([], 0.0026 * len(handles), "run-ig")


USAGE = SimpleNamespace(input_tokens=850, output_tokens=95)


class FakeScorer:
    def __init__(self, score: int = 82, error: str | None = None) -> None:
        self.calls = 0
        self.value, self.error = score, error

    async def score(self, icp, profile, evidence, prompt=None, more=None):
        from services.scoring import ScoreResult

        self.calls += 1
        self.last_evidence = evidence
        return ScoreResult(None if self.error else self.value, "Prints shirts for local teams", ["Website offers screen printing"],
                           [], "claude-haiku-5-5", self.error, input_tokens=900, output_tokens=120)  # fmt: skip


def company(name, domain):
    return Company(id=uuid4(), workspace_id=uuid4(), icp_id=uuid4(), source="apollo", name=name, domain=domain,
                   status="found", overridden=False, raw={"industry": "printing"})  # fmt: skip


def site(domain, text, socials=None):
    return SiteRead(domain=domain, outcome="ok", via="own_crawl", resolved_url=f"https://{domain}/", text=text,
                    title=domain, socials=socials or {},
                    pages=[{"url": f"https://{domain}/", "role": "homepage", "status": 200, "text": text}])  # fmt: skip


@sync
async def test_research_reads_checks_scores_and_decides() -> None:
    good, wrong, sells_machines, down = (
        company("Ink Co", "inkco.com"), company("Bake Co", "bakeco.com"),
        company("Press Supply", "presssupply.com"), company("Down Co", "down.com"),
    )  # fmt: skip
    reader = FakeReader({
        "inkco.com": site("inkco.com", "Same day screen printing", {"instagram": "https://instagram.com/inkco"}),
        "bakeco.com": site("bakeco.com", "Fresh bread daily"),
        "presssupply.com": site("presssupply.com", "Screen printing equipment for sale"),
    })  # fmt: skip
    apify, scorer = FakeApify(), FakeScorer(82)
    summary = await research_companies([good, wrong, sells_machines, down], ICP, None, reader, apify, scorer, 60)

    # Only a person rejects: the rest wait for approval.
    assert (summary.qualified, summary.not_fit, summary.review, summary.errors) == (1, 0, 3, 0)
    assert good.status == "qualified" and good.score == 82 and good.status_reason.startswith("Scored 82")
    assert good.instagram_url == "https://instagram.com/inkco"
    assert good.research["instagram"]["followers"] == 1200 and good.research["website"]["via"] == "own_crawl"
    assert wrong.status == "review" and "None of the ICP's words" in wrong.status_reason
    assert sells_machines.status == "review" and '"equipment"' in sells_machines.status_reason
    assert down.status == "review" and "couldn't be read" in down.status_reason
    # Places were applied by Apollo's search, so they're skipped, not failed.
    assert good.rule_detail["skipped"]
    # Claude reads every site that could be reached (it picks the pages the rule then checks), so the
    # three reachable companies were each scored once; the unreachable one wasn't. The rule still
    # decides: the two that failed it are not a fit despite Claude's 82, which is kept for reference.
    assert scorer.calls == 3 and apify.asked == [["inkco"]]
    assert wrong.score == 82 and wrong.score_detail["summary"] and down.score is None
    assert summary.apify_cost_usd == 0.0026
    assert (summary.claude_calls, summary.input_tokens, summary.output_tokens) == (3, 2700, 360)
    # What was read comes back to be stored: page texts (not kept in the company's research JSON) and posts.
    via, pages = summary.pages[good.id]
    assert via == "own_crawl" and pages[0]["text"] == "Same day screen printing" and "text" not in good.research["website"]["pages"][0]
    assert [p.text for p in summary.posts[good.id]] == ["New colours"] and good.research["instagram"]["raw"] == {"id": "inkco"}
    assert (good.score_detail["input_tokens"], good.score_detail["prompt_version_id"]) == (900, None)
    assert summary.claude_model == "claude-haiku-5-5"


@sync
async def test_a_low_score_waits_for_approval_and_overrides_stick() -> None:
    low, kept = company("Ink Co", "inkco.com"), company("Kept Co", "keptco.com")
    kept.overridden, kept.status = True, "qualified"
    reader = FakeReader({"inkco.com": site("inkco.com", "screen printing"), "keptco.com": site("keptco.com", "bakery")})
    await research_companies([low, kept], ICP, None, reader, None, FakeScorer(40), 60)
    assert low.status == "review" and low.status_reason.startswith("Scored 40, under 60")
    assert kept.status == "qualified" and kept.rule_fit is False  # research recorded, decision kept


@sync
async def test_without_claude_the_rule_decides_and_scoring_errors_are_errors() -> None:
    a = company("Ink Co", "inkco.com")
    await research_companies([a], ICP, None, FakeReader({"inkco.com": site("inkco.com", "screen printing")}), None, None, 60)
    assert a.status == "qualified" and "Not scored" in a.status_reason
    b = company("Ink Co", "inkco.com")
    await research_companies([b], ICP, None, FakeReader({"inkco.com": site("inkco.com", "screen printing")}), None,
                             FakeScorer(error="Claude is rate-limiting us; try again shortly."), 60)  # fmt: skip
    assert b.status == "error" and "rate-limiting" in b.status_reason


def test_default_prompt_carries_icp_and_evidence_verbatim() -> None:
    variables = verify_variables(ICP, {"description": "Local print shops", "pains": "Slow quotes", "offer": "Faster quoting"},
                                 {"name": "Ink Co", "website_text": "Same day screen printing", "instagram": None})  # fmt: skip
    prompt = render(DEFAULTS["verify"].user_prompt, variables)
    assert "Faster quoting" in prompt and "Same day screen printing" in prompt and "Ink Co" in prompt
    assert "{{" not in prompt and "Instagram profile:\n(not found)" in prompt
    # Unknown placeholders render empty rather than leaking into the prompt.
    assert render("a {{nope}} b {{ icp.name }}", variables) == f"a  b {ICP['name']}"


@sync
async def test_scorer_uses_the_given_prompt_and_its_model() -> None:
    seen = {}

    async def parse(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(stop_reason="end_turn", usage=USAGE, parsed_output=CompanyScore(score=70, summary="ok", reasons=[], concerns=[]))

    fake = SimpleNamespace(messages=SimpleNamespace(parse=parse), close=lambda: asyncio.sleep(0))
    spec = PromptSpec("Be strict.", "Score {{company.name}} for {{icp.name}}.", "claude-sonnet-5-5", 900)
    result = await CompanyScorer("key", "claude-haiku-5-5", client=fake).score(ICP, None, {"name": "Ink Co"}, spec)
    assert (seen["model"], seen["system"], seen["max_tokens"]) == ("claude-sonnet-5-5", "Be strict.", 900)
    assert seen["messages"][0]["content"] == f"Score Ink Co for {ICP['name']}."
    assert result.model == "claude-sonnet-5-5"


@sync
async def test_scorer_uses_haiku_low_effort_and_structured_output() -> None:
    seen = {}

    async def parse(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(stop_reason="end_turn", usage=USAGE, parsed_output=CompanyScore(score=130, summary=" Fits ", reasons=["a", " "], concerns=[]))

    fake = SimpleNamespace(messages=SimpleNamespace(parse=parse), close=lambda: asyncio.sleep(0))
    scorer = CompanyScorer("key", "claude-haiku-5-5", client=fake)
    result = await scorer.score(ICP, None, {"name": "Ink Co"})
    assert seen["model"] == "claude-haiku-5-5" and seen["output_config"] == {"effort": "low"}
    assert seen["output_format"] is CompanyScore
    assert (result.score, result.summary, result.reasons) == (100, "Fits", ["a"])  # clamped and tidied
    assert (result.input_tokens, result.output_tokens) == (850, 95)


@sync
async def test_scorer_reports_refusals_and_api_errors() -> None:
    async def refuse(**kwargs):
        return SimpleNamespace(stop_reason="refusal", usage=USAGE, parsed_output=None)

    fake = SimpleNamespace(messages=SimpleNamespace(parse=refuse), close=lambda: asyncio.sleep(0))
    assert "declined" in (await CompanyScorer("k", "m", client=fake).score(ICP, None, {})).error

    async def limited(**kwargs):
        raise anthropic.RateLimitError("slow down", response=httpx.Response(429, request=httpx.Request("POST", "https://x")), body=None)

    fake = SimpleNamespace(messages=SimpleNamespace(parse=limited), close=lambda: asyncio.sleep(0))
    assert "rate-limiting" in (await CompanyScorer("k", "m", client=fake).score(ICP, None, {})).error


@sync
async def test_claude_reads_one_page_at_a_time_and_decides_when_to_stop() -> None:
    from services.scoring import ScoreResult

    class Reader(FakeReader):
        def can_read_more(self, site, max_pages=6, max_seconds=120):
            return bool(site.candidates) and len(site.pages) < max_pages

        async def read_more(self, site, url, reason="", max_pages=6, max_seconds=120):
            match = next((c for c in site.candidates if c["url"] == url), None)
            if match is None:
                return None
            site.candidates.remove(match)
            page = {"url": url, "role": "about", "status": 200, "text": "We do screen printing", "reason": reason}
            site.pages.append(page)
            site.text += " We do screen printing"
            return page

    class Scorer:
        def __init__(self):
            self.offers = []

        async def score(self, icp, profile, evidence, prompt=None, more=None):
            self.offers.append(None if more is None else [c["url"] for c in more.candidates])
            asks = len(self.offers) == 1  # first call: wants the About page; second: has enough
            return ScoreResult(85, "Prints shirts", [], [], "claude-haiku-5-5", input_tokens=100, output_tokens=20,
                               next_page="https://inkco.com/about" if asks else None,
                               next_reason="See what they make" if asks else "")  # fmt: skip

    home = site("inkco.com", "Ink Co custom tees")
    home.candidates = [{"url": "https://inkco.com/about", "text": "About us"}, {"url": "https://inkco.com/blog", "text": "Blog"}]
    good = company("Ink Co", "inkco.com")
    scorer = Scorer()
    summary = await research_companies([good], ICP, None, Reader({"inkco.com": home}), None, scorer, 60)

    assert scorer.offers == [["https://inkco.com/about", "https://inkco.com/blog"], ["https://inkco.com/blog"]]
    assert good.status == "qualified" and good.score == 85 and summary.claude_calls == 2
    assert good.score_detail["reading"] == [{"url": "https://inkco.com/about", "reason": "See what they make", "read": True}]
    assert [p["url"] for p in good.research["website"]["pages"]] == ["https://inkco.com/", "https://inkco.com/about"]
    assert len(summary.pages[good.id][1]) == 2  # both pages kept with their text, for eo_company_pages


@sync
async def test_an_icp_with_its_website_switched_off_never_reads_it() -> None:
    ink = company("Ink Co", "inkco.com")
    reader = FakeReader({"inkco.com": site("inkco.com", "Same day screen printing")})
    await research_companies([ink], ICP, None, reader, None, None, 60, read_website=False)
    assert reader.read_calls == []
    # The rule needs website words it never got, and says why.
    assert ink.status == "review" and "website not read: off for this ICP" in ink.status_reason


@sync
async def test_fit_score_switched_off_says_so() -> None:
    ink = company("Ink Co", "inkco.com")
    reader = FakeReader({"inkco.com": site("inkco.com", "Same day screen printing")})
    await research_companies([ink], ICP, None, reader, None, None, 60, unscored="Claude's fit score is off for this ICP")
    assert ink.status == "qualified" and ink.status_reason.endswith("Not scored: Claude's fit score is off for this ICP")


def test_jina_can_be_switched_off_for_a_while() -> None:
    from services.website import WebsiteReader

    reader = WebsiteReader(jina=object())
    with reader.jina_off():
        assert reader._jina is None
    assert reader._jina is not None
    with reader.jina_off(False):
        assert reader._jina is not None


@sync
async def test_with_auto_approve_off_every_company_waits_for_approval() -> None:
    """Like auto-send for emails: off, even a high score waits for a person."""
    high = company("Ink Co", "inkco.com")
    reader = FakeReader({"inkco.com": site("inkco.com", "screen printing")})
    summary = await research_companies([high], ICP, None, reader, None, FakeScorer(95), 60, auto_approve=False)
    assert high.status == "review" and high.status_reason.startswith("Scored 95:") and summary.review == 1


@sync
async def test_reading_the_site_again_keeps_socials_and_contacts_but_not_the_verdict() -> None:
    """No AI: what the site shows is kept, the score and status aren't touched."""
    from services.research import reread_site

    ink = company("Ink Co", "inkco.com")
    ink.status, ink.score, ink.linkedin_url = "qualified", 82, "https://www.linkedin.com/company/old"
    found = site("inkco.com", "Custom printing. Email hello@inkco.com or call (512) 555-0100",
                 socials={"instagram": "https://instagram.com/inkco", "tiktok": "https://tiktok.com/@inkco"})  # fmt: skip
    out = await reread_site(ink, FakeReader({"inkco.com": found}))
    assert out.reached and (ink.status, ink.score) == ("qualified", 82)
    assert ink.instagram_url == "https://instagram.com/inkco" and ink.linkedin_url == "https://www.linkedin.com/company/old"
    assert out.socials["tiktok"] == "https://tiktok.com/@inkco" and "hello@inkco.com" in out.emails
    assert ink.research["website"]["text"].startswith("Custom printing") and "reread_at" in ink.research
    down = await reread_site(company("Gone Co", "gone.com"), FakeReader({}))
    assert not down.reached and down.detail == "down"


def test_background_paths_with_an_id() -> None:
    from background import long_running

    assert long_running("/pipeline/companies/7f3c/reread") == "Reading the website again"
    assert long_running("/pipeline/research") == "Researching companies"
    assert long_running("/pipeline/companies/7f3c/content") is None
