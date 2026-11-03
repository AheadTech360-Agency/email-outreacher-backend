"""Reading a company's website: its visible text and the social accounts it links to.

Follows Lead Intel's crawler rules (packages/providers/src/crawl), so a site is treated the same
whichever project reads it:
  - robots.txt is read first and obeyed, for our bot's name;
  - one request at a time, at least 2 seconds apart (longer if robots.txt asks);
  - HTTPS first, HTTP only as a fallback;
  - homepage, then the contact / social / about pages it links to, at most 4 pages, 30 seconds;
  - the text kept is the visible words, collapsed and cut at 2,000 characters.

When our own fetch can't read the site (it turns plain clients away, or renders only with
JavaScript), Jina Reader reads it in a real browser instead. Never when robots.txt forbids us.

Read step by step (research with Claude): `read(domain, max_pages=1)` reads only the homepage and
lists the site's other pages (`candidates`); `read_more(site, url)` then reads one chosen page at a
time, under the same rules, up to a number of pages and seconds the workspace sets.
"""

import asyncio
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from services.contacts import contact_links, founder_names
from services.jina import JinaClient, JinaError

BOT_INFO_URL = "https://aheadtech360.com"


def user_agent(info_url: str = BOT_INFO_URL) -> str:
    """Who's reading, with a page site owners can open to find out more."""
    return f"AheadTech360LeadBot/1.0 (+{info_url})"


USER_AGENT = user_agent()
ROBOTS_TOKEN = "AheadTech360LeadBot"
REQUEST_TIMEOUT = 15.0
BUDGET_SECONDS = 30.0
MIN_DELAY_SECONDS = 2.0
MAX_PAGES = 4
MAX_BYTES = 5 * 1024 * 1024
TEXT_LIMIT = 2000
# Each page's own text is kept too (stored in eo_company_pages), up to this much.
PAGE_TEXT_LIMIT = 50_000
# Below this many characters of text, a page probably renders with JavaScript.
THIN_TEXT = 200
# Reading step by step, with Claude choosing each next page. Defaults; each workspace sets its own.
AI_MAX_PAGES = 6
AI_BUDGET_SECONDS = 120.0
MAX_CANDIDATES = 40
NOT_PAGES = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|ico|zip|mp4|mov|mp3|css|js|xml|json|docx?|xlsx?)$", re.I)

PRIORITY_PAGES: list[tuple[str, re.Pattern[str]]] = [
    ("contact", re.compile(r"contact|get-?in-?touch|reach-?us|find-?us|book|appointment|quote|estimate|schedule", re.I)),
    ("social", re.compile(r"social|follow-?us|connect|our-?socials|find-?us-?on|community", re.I)),
    ("about", re.compile(r"about|who-?we-?are|our-?story|services|what-?we-?do|team", re.I)),
]

SOCIAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/(?!p/|reel/|explore/|stories/)([A-Za-z0-9_.]+)", re.I),
    "facebook": re.compile(r"https?://(?:www\.|m\.)?facebook\.com/(?!sharer|share\.php|dialog/|plugins/|tr[/?]|tr$)([A-Za-z0-9_.\-]+)", re.I),
    "linkedin": re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|in)/([A-Za-z0-9_\-%]+)", re.I),
    "tiktok": re.compile(r"https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]+)", re.I),
    "youtube": re.compile(r"https?://(?:www\.)?youtube\.com/(?:@|channel/|c/|user/)([A-Za-z0-9_\-]+)", re.I),
    "x": re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/(?!intent/|share)([A-Za-z0-9_]+)", re.I),
    "pinterest": re.compile(r"https?://(?:[a-z]{2}\.|www\.)?pinterest\.[a-z.]+/(?!pin/|pin-builder|search/)([A-Za-z0-9_]+)", re.I),
    "threads": re.compile(r"https?://(?:www\.)?threads\.(?:net|com)/@([A-Za-z0-9_.]+)", re.I),
}


@dataclass
class SiteRead:
    domain: str
    # ok | unreachable | blocked_by_robots | no_text
    outcome: str
    via: str | None = None  # own_crawl | jina
    resolved_url: str | None = None
    text: str = ""
    title: str = ""
    pages: list[dict] = field(default_factory=list)  # [{url, role, status, text}]
    socials: dict[str, str] = field(default_factory=dict)  # {"instagram": "https://instagram.com/x", ...}
    # For contact details (services/contacts.py): mailto and tel links, and founders named in structured data.
    mailtos: list[str] = field(default_factory=list)
    tels: list[str] = field(default_factory=list)
    owner_names: list[str] = field(default_factory=list)
    detail: str | None = None
    # Pages on the same site not read yet: [{url, text}] (text is the link's words). See read_more.
    candidates: list[dict] = field(default_factory=list)
    # What read_more needs to keep reading politely: robots rules, delay, clock.
    session: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def reached(self) -> bool:
        return self.outcome == "ok"


class _PageParser(HTMLParser):
    """Visible text, the <title>, and every link on a page. Script, style and similar are skipped."""

    SKIP = {"script", "style", "noscript", "svg", "template", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[str] = []
        self.anchors: list[tuple[str, str]] = []  # (href, the link's words)
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._anchor: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
                self._anchor = (href, [])

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._anchor:
            href, words = self._anchor
            self.anchors.append((href, " ".join(words)[:80]))
            self._anchor = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.text.append(data.strip())
            if self._anchor:
                self._anchor[1].append(data.strip())


def _parse(html: str) -> _PageParser:
    parser = _PageParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001  (malformed HTML: keep what was parsed)
        pass
    return parser


def parse_html(html: str) -> tuple[str, str, list[str]]:
    parser = _parse(html)
    return " ".join(parser.text), parser.title.strip(), parser.links


def same_site_pages(base_url: str, anchors: list[tuple[str, str]], skip: set[str]) -> list[dict]:
    """Links to other pages on the same site, with their words: [{url, text}]. No files, no repeats."""
    host = urlsplit(base_url).netloc
    out: dict[str, str] = {}
    for href, words in anchors:
        url = urljoin(base_url, href).split("#")[0]
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.netloc != host or NOT_PAGES.search(parts.path):
            continue
        key = url.rstrip("/")
        if key in skip or key in out:
            continue
        out[key] = words
    return [{"url": url, "text": words} for url, words in out.items()][:MAX_CANDIDATES]


def find_socials(sources: list[str]) -> dict[str, str]:
    """The first profile URL for each social network found in these links or page sources.

    Page sources are searched too, because sites often name their accounts only in structured
    data ("sameAs") or footer scripts, not in a plain link.
    """
    found: dict[str, str] = {}
    for source in sources:
        for network, pattern in SOCIAL_PATTERNS.items():
            if network in found:
                continue
            match = pattern.search(source)
            if match:
                found[network] = match.group(0).rstrip("/").replace("http://", "https://")
    return found


def digest(texts: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(texts)).strip()[:TEXT_LIMIT]


def page_text(text: str) -> str:
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()[:PAGE_TEXT_LIMIT]


def normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    host = urlsplit(v if "://" in v else f"https://{v}").netloc or v
    host = host.split("@")[-1].split(":")[0].removeprefix("www.")
    return host or None


class WebsiteReader:
    def __init__(
        self,
        jina: JinaClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep=asyncio.sleep,
        clock=time.monotonic,
        bot_info_url: str = BOT_INFO_URL,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent(bot_info_url), "Accept": "text/html,application/xhtml+xml"},
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            max_redirects=5,
            transport=transport,
        )
        self._jina = jina
        self._sleep = sleep
        self._clock = clock

    @contextmanager
    def jina_off(self, off: bool = True) -> Iterator[None]:
        """Read without Jina for a while (an ICP that has it switched off): only our own reader."""
        saved = self._jina
        if off:
            self._jina = None
        try:
            yield
        finally:
            self._jina = saved

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str) -> httpx.Response | None:
        try:
            response = await self._client.get(url)
        except httpx.HTTPError:
            return None
        if len(response.content) > MAX_BYTES:
            return None
        return response

    async def _robots(self, domain: str) -> tuple[RobotFileParser, float]:
        robots = RobotFileParser()
        response = await self._get(f"https://{domain}/robots.txt")
        if response is not None and response.status_code == 200:
            robots.parse(response.text.splitlines())
        else:
            robots.parse([])  # missing or unreadable: everything allowed, as crawlers treat it
        delay = robots.crawl_delay(ROBOTS_TOKEN) or 0
        return robots, max(MIN_DELAY_SECONDS, float(delay))

    async def read(self, domain_or_url: str, max_pages: int = MAX_PAGES) -> SiteRead:
        """Read a site. With max_pages=1 only the homepage is read; read_more continues from there."""
        domain = normalize_domain(domain_or_url)
        if not domain:
            return SiteRead(domain="", outcome="unreachable", detail="No website")
        started = self._clock()
        remaining = lambda: BUDGET_SECONDS - (self._clock() - started)  # noqa: E731
        robots, delay = await self._robots(domain)
        requests = 0

        async def polite_get(url: str) -> httpx.Response | None:
            nonlocal requests
            if requests:
                if remaining() <= delay:
                    return None
                await self._sleep(delay)
            requests += 1
            return await self._get(url)

        # Homepage: HTTPS first, HTTP as a fallback.
        homepage = None
        home_url = None
        for candidate in (f"https://{domain}/", f"http://{domain}/"):
            if not robots.can_fetch(ROBOTS_TOKEN, candidate):
                return SiteRead(domain=domain, outcome="blocked_by_robots", detail="robots.txt asks us not to read this site")
            homepage = await polite_get(candidate)
            if homepage is not None and homepage.status_code < 400 and "html" in homepage.headers.get("content-type", "html"):
                home_url = str(homepage.url)
                break
            homepage = None

        if homepage is None:
            return await self._with_jina(domain, robots, SiteRead(domain=domain, outcome="unreachable", detail="The site didn't answer"))

        parsed = _parse(homepage.text)
        text, title, links = " ".join(parsed.text), parsed.title.strip(), parsed.links
        anchors = list(parsed.anchors)
        texts = [text]
        all_links = list(links)
        sources = [homepage.text]
        pages = [{"url": home_url, "role": "homepage", "status": homepage.status_code, "text": page_text(text)}]

        # Contact, social and about pages on the same site, in that order of value.
        site_host = urlsplit(home_url).netloc
        chosen: list[tuple[str, str]] = []
        for role, pattern in PRIORITY_PAGES:
            for href in links:
                url = urljoin(home_url, href).split("#")[0]
                parts = urlsplit(url)
                if parts.netloc != site_host or url.rstrip("/") == home_url.rstrip("/"):
                    continue
                if pattern.search(parts.path) and url not in {u for u, _ in chosen}:
                    chosen.append((url, role))
                    break
        for url, role in chosen[: max(0, max_pages - 1)]:
            if remaining() <= 0 or not robots.can_fetch(ROBOTS_TOKEN, url):
                continue
            page = await polite_get(url)
            if page is None or page.status_code >= 400:
                continue
            page_parsed = _parse(page.text)
            body, page_links = " ".join(page_parsed.text), page_parsed.links
            anchors += page_parsed.anchors
            texts.append(body)
            all_links.extend(page_links)
            sources.append(page.text)
            pages.append({"url": str(page.url), "role": role, "status": page.status_code, "text": page_text(body)})

        result = SiteRead(
            domain=domain,
            outcome="ok",
            via="own_crawl",
            resolved_url=home_url,
            text=digest(texts),
            title=title,
            pages=pages,
            socials=find_socials([urljoin(home_url, link) for link in all_links] + sources),
            mailtos=contact_links(all_links)[0],
            tels=contact_links(all_links)[1],
            owner_names=[n for source in sources for n in founder_names(source)],
            candidates=same_site_pages(home_url, anchors, {p["url"].rstrip("/") for p in pages}),
            session={"robots": robots, "delay": delay, "started": self._clock(), "last": self._clock(), "texts": texts},
        )
        if len(result.text) < THIN_TEXT:
            result.outcome = "no_text"
            result.detail = "Very little text without a browser; the site probably needs JavaScript"
            return await self._with_jina(domain, robots, result)
        return result

    async def _with_jina(self, domain: str, robots: RobotFileParser, fallback: SiteRead) -> SiteRead:
        """Read the homepage through Jina's browser, if we have Jina and robots.txt allows it."""
        url = fallback.resolved_url or f"https://{domain}/"
        if self._jina is None or not robots.can_fetch(ROBOTS_TOKEN, url):
            return fallback
        try:
            page = await self._jina.read(url)
        except JinaError as exc:
            fallback.detail = f"{fallback.detail or 'Not readable'}; Jina: {exc.message}"
            return fallback
        text = digest([page.content])
        if not text:
            return fallback
        socials = find_socials(page.links + re.findall(r"https?://[^\s)\]>\"']+", page.content))
        home = page.url or url
        return SiteRead(
            domain=domain,
            outcome="ok",
            via="jina",
            resolved_url=home,
            text=text,
            title=page.title,
            pages=[{"url": home, "role": "homepage", "status": 200, "text": page_text(page.content)}],
            socials={**fallback.socials, **socials},
            mailtos=contact_links(page.links)[0],
            tels=contact_links(page.links)[1],
            candidates=same_site_pages(home, [(link, "") for link in page.links], {home.rstrip("/")}),
            session={"robots": robots, "delay": MIN_DELAY_SECONDS, "started": self._clock(), "last": self._clock(),
                     "texts": [page.content]},  # fmt: skip
        )

    def can_read_more(self, site: SiteRead, max_pages: int = AI_MAX_PAGES, max_seconds: float = AI_BUDGET_SECONDS) -> bool:
        s = site.session
        return (
            site.reached and bool(s) and bool(site.candidates) and len(site.pages) < max_pages
            and self._clock() - s["started"] < max_seconds
        )  # fmt: skip

    async def read_more(
        self,
        site: SiteRead,
        url: str,
        reason: str = "",
        max_pages: int = AI_MAX_PAGES,
        max_seconds: float = AI_BUDGET_SECONDS,
    ) -> dict | None:
        """Read one more page of a site already read, politely; None if it can't be read.

        Only a page from site.candidates. The page is added to site.pages (with `reason`), its text to
        site.text, its links to site.candidates and site.socials.
        """
        wanted = url.rstrip("/")
        match = next((c for c in site.candidates if c["url"].rstrip("/") == wanted), None)
        if match is None or not self.can_read_more(site, max_pages, max_seconds):
            return None
        site.candidates = [c for c in site.candidates if c is not match]
        s = site.session
        if not s["robots"].can_fetch(ROBOTS_TOKEN, match["url"]):
            return None
        wait = s["delay"] - (self._clock() - s["last"])
        if wait > 0:
            await self._sleep(wait)
        s["last"] = self._clock()
        if site.via == "jina":
            if self._jina is None:
                return None
            try:
                got = await self._jina.read(match["url"])
            except JinaError:
                return None
            body, final_url, status, anchors, source = got.content, got.url or match["url"], 200, [(l, "") for l in got.links], got.content
        else:
            response = await self._get(match["url"])
            if response is None or response.status_code >= 400 or "html" not in response.headers.get("content-type", "html"):
                return None
            parsed = _parse(response.text)
            body, final_url, status, anchors, source = " ".join(parsed.text), str(response.url), response.status_code, parsed.anchors, response.text
        page = {"url": final_url, "role": match["text"] or "page", "status": status, "text": page_text(body), "reason": reason}
        site.pages.append(page)
        s["texts"].append(body)
        site.text = digest(s["texts"])
        read = {p["url"].rstrip("/") for p in site.pages}
        site.candidates += [c for c in same_site_pages(final_url, anchors, read) if c["url"].rstrip("/") not in
                            {x["url"].rstrip("/") for x in site.candidates}]  # fmt: skip
        site.candidates = site.candidates[:MAX_CANDIDATES]
        site.socials = {**find_socials([urljoin(final_url, h) for h, _ in anchors] + [source]), **site.socials}
        mailtos, tels = contact_links([h for h, _ in anchors])
        site.mailtos = list(dict.fromkeys(site.mailtos + mailtos))
        site.tels = list(dict.fromkeys(site.tels + tels))
        site.owner_names = list(dict.fromkeys(site.owner_names + founder_names(source)))
        return page
