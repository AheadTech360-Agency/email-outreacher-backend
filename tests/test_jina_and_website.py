"""Jina's rate limiting and retries, and the website reader, against fakes. No network."""

import asyncio
import functools

import httpx
import pytest

from services.jina import JinaClient, JinaError, RateLimiter
from services.website import WebsiteReader, find_socials, normalize_domain


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeClock:
    """Time that only moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


# ---------- Jina ----------


@sync
async def test_limiter_spaces_requests_to_the_per_minute_limit() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        await limiter.wait_turn()
    assert clock.slept == []
    await limiter.wait_turn()  # the 4th has to wait for the first to leave the 60s window
    assert clock.slept and clock.now >= 1060


@sync
async def test_429_is_retried_after_retry_after_and_holds_other_callers() -> None:
    clock = FakeClock()
    limiter = RateLimiter(100, clock=clock, sleep=clock.sleep)
    answers = iter([
        httpx.Response(429, headers={"retry-after": "7"}),
        httpx.Response(200, json={"code": 200, "data": {"title": "T", "url": "https://x.com/", "content": "Hello",
                                                        "links": {"Instagram": "https://instagram.com/x"}}}),
    ])  # fmt: skip
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["accept"] = request.headers.get("accept")
        return next(answers)

    jina = JinaClient("key123", transport=httpx.MockTransport(handler), limiter=limiter, sleep=clock.sleep)
    page = await jina.read("https://x.com/")
    await jina.aclose()
    assert page.title == "T" and page.content == "Hello" and page.links == ["https://instagram.com/x"]
    assert seen["url"] == "https://r.jina.ai/https://x.com/"
    assert seen["auth"] == "Bearer key123" and seen["accept"] == "application/json"
    assert 7 in clock.slept  # waited Retry-After before trying again


@sync
async def test_persistent_429_gives_up_with_a_clear_error() -> None:
    clock = FakeClock()
    limiter = RateLimiter(100, clock=clock, sleep=clock.sleep)
    jina = JinaClient(None, transport=httpx.MockTransport(lambda r: httpx.Response(429)), limiter=limiter, sleep=clock.sleep)
    with pytest.raises(JinaError) as exc:
        await jina.read("https://x.com/")
    await jina.aclose()
    assert exc.value.status == 429 and "rate-limiting" in exc.value.message


@pytest.mark.parametrize(("status", "text"), [(401, "rejected the API key"), (402, "out of tokens")])
@sync
async def test_jina_errors_are_readable(status: int, text: str) -> None:
    jina = JinaClient(None, transport=httpx.MockTransport(lambda r: httpx.Response(status)), limiter=RateLimiter(100))
    with pytest.raises(JinaError) as exc:
        await jina.read("https://x.com/")
    await jina.aclose()
    assert text in exc.value.message


# ---------- Website reader ----------

HOME = """<html><head><title>Ink Co</title><script>var x='screen printing in script';</script>
<script type="application/ld+json">{"sameAs":["https://www.instagram.com/inkco/"]}</script></head>
<body><h1>Ink Co</h1><p>Same day screen printing and embroidery for local teams and brands across the city.</p>
<a href="/contact">Contact us</a><a href="/about-us">About</a><a href="/blog">Blog</a>
<a href="https://facebook.com/tr?id=1">pixel</a><a href="https://www.facebook.com/inkco">Facebook</a></body></html>"""


def site(routes: dict[str, httpx.Response], hits: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        assert request.headers["user-agent"].startswith("AheadTech360LeadBot")
        return routes.get(str(request.url), httpx.Response(404))

    return httpx.MockTransport(handler)


def html(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html"})


@sync
async def test_reads_home_contact_about_politely_and_finds_socials() -> None:
    long = "We print shirts. " * 20
    hits: list[str] = []
    routes = {
        "https://inkco.com/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /blog\n"),
        "https://inkco.com/": html(HOME),
        "https://inkco.com/contact": html(f"<p>Call us. {long}</p>"),
        "https://inkco.com/about-us": html(f"<p>Family run since 2009. {long}</p>"),
    }
    clock = FakeClock()
    reader = WebsiteReader(transport=site(routes, hits), sleep=clock.sleep, clock=clock)
    result = await reader.read("www.InkCo.com")
    await reader.aclose()
    assert result.outcome == "ok" and result.via == "own_crawl" and result.title == "Ink Co"
    assert [p["role"] for p in result.pages] == ["homepage", "contact", "about"]
    assert "screen printing" in result.text and "in script" not in result.text  # scripts aren't text
    assert len(result.text) <= 2000
    assert "Family run since 2009" in result.pages[2]["text"] and len(result.pages[2]["text"]) > 300  # full page, not the digest
    assert result.socials == {"instagram": "https://www.instagram.com/inkco", "facebook": "https://www.facebook.com/inkco"}
    assert all(s >= 2 for s in clock.slept) and len(clock.slept) == 2  # 2s between page requests
    assert "https://inkco.com/blog" not in hits


@sync
async def test_robots_disallow_stops_everything_including_jina() -> None:
    hits: list[str] = []
    routes = {"https://shy.com/robots.txt": httpx.Response(200, text="User-agent: AheadTech360LeadBot\nDisallow: /\n")}
    jina_calls: list[str] = []
    jina = JinaClient(None, transport=httpx.MockTransport(lambda r: jina_calls.append(1) or httpx.Response(200)), limiter=RateLimiter(100))
    reader = WebsiteReader(jina=jina, transport=site(routes, hits))
    result = await reader.read("shy.com")
    await reader.aclose()
    await jina.aclose()
    assert result.outcome == "blocked_by_robots" and jina_calls == [] and hits == ["https://shy.com/robots.txt"]


@sync
async def test_javascript_only_site_falls_back_to_jina() -> None:
    hits: list[str] = []
    routes = {"https://spa.com/": html("<html><body><div id='root'></div></body></html>")}
    body = {"data": {"title": "SPA Co", "url": "https://spa.com/", "content": "We do screen printing. " * 20,
                     "links": [["IG", "https://instagram.com/spaco"]]}}  # fmt: skip
    jina = JinaClient(None, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)), limiter=RateLimiter(100))
    reader = WebsiteReader(jina=jina, transport=site(routes, hits))
    result = await reader.read("spa.com")
    await reader.aclose()
    await jina.aclose()
    assert result.outcome == "ok" and result.via == "jina" and "screen printing" in result.text
    assert result.socials["instagram"] == "https://instagram.com/spaco"


@sync
async def test_unreachable_site_without_jina_is_reported() -> None:
    reader = WebsiteReader(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    result = await reader.read("down.com")
    await reader.aclose()
    assert result.outcome == "unreachable" and not result.reached


def test_helpers() -> None:
    assert normalize_domain("https://WWW.Example.com/path") == "example.com"
    assert normalize_domain("example.com:8080") == "example.com"
    assert normalize_domain("") is None
    assert find_socials(['"sameAs":["https://instagram.com/p/ABC","https://instagram.com/real"]']) == {
        "instagram": "https://instagram.com/real"
    }


# ---------- The rate limit stays (2026-11-05) ----------


def test_jina_is_never_allowed_more_than_its_published_limit() -> None:
    """Without a key Jina allows 20 a minute. Whatever JINA_RPM says, the limiter never lets more through."""
    from services.jina import MAX_RPM_WITHOUT_KEY, MAX_RPM_WITH_KEY, limiter_for

    assert MAX_RPM_WITHOUT_KEY == 20 and MAX_RPM_WITH_KEY == 500
    assert limiter_for(None, None).rpm <= 20
    assert limiter_for(None, 1000).rpm == 20
    assert limiter_for("key", 100_000).rpm == 500


def test_the_shared_limiter_works_on_every_job_s_event_loop() -> None:
    """Each background job runs on a fresh event loop and the limiter is shared by the process: the second job crashed
    with "bound to a different event loop" once callers had waited on it in the first."""
    import asyncio

    limiter = RateLimiter(2)

    async def job() -> None:
        limiter._starts.clear()
        waits = [asyncio.wait_for(limiter.wait_turn(), 0.2) for _ in range(4)]
        await asyncio.gather(*waits, return_exceptions=True)  # two get through, two wait on the lock

    asyncio.run(job())
    asyncio.run(job())  # raised RuntimeError before
