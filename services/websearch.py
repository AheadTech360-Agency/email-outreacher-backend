"""Web searches for Quick Start's LinkedIn option: each company's website, Instagram and Facebook, and LinkedIn
profiles of people there. Two engines, chosen per run:
  - Brave Search API (BRAVE_API_KEY): free up to the plan's monthly credit (about 1,000 searches). Brave doesn't say
    how many are left, so Coldbrew counts them (eo_provider_calls) and stops at BRAVE_MONTHLY_SEARCHES;
  - Google, through Apify's Google Search actor: paid per results page (about $0.0025 to $0.0045), logged as spend.
Both are official ways in; nothing here pretends to be a browser."""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from services.website import normalize_domain

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
GOOGLE_SEARCH_ACTOR = "apify~google-search-scraper"
Engine = Literal["brave", "google"]

# Sites that list businesses but aren't the business's own website.
NOT_A_WEBSITE = (
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com", "youtube.com", "pinterest.",
    "yelp.", "yellowpages.", "bbb.org", "crunchbase.com", "zoominfo.com", "dnb.com", "bloomberg.com", "glassdoor.",
    "indeed.", "mapquest.com", "google.", "apple.com/maps", "amazon.", "ebay.", "etsy.com", "wikipedia.org", "manta.com",
    "opencorporates.com", "trustpilot.", "tripadvisor.", "angi.com", "thumbtack.com", "houzz.com", "chamberofcommerce.com",
)  # fmt: skip
INSTAGRAM_SKIP = {"p", "reel", "reels", "explore", "stories", "tv", "accounts", "about", "developer", "legal"}
FACEBOOK_SKIP = {"pages", "groups", "events", "watch", "marketplace", "share", "sharer", "login", "help", "people", "story.php", "photo.php", "permalink.php"}
# Two-part endings, so the name in "inkco.co.uk" is "inkco".
SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}
# Country endings other than these aren't a US company's own site (stores.allbirds.ca, allbirds.co.uk).
OK_COUNTRY_ENDINGS = {"us", "co", "io", "ai", "me", "tv", "ly", "so"}
# Words a company's own domain may add to its name ("inkcoinc.com", "inkcohq.com"). Not "official", "store",
# "shop" or "outlet": knock-off stores use those ("allbirds-official.com", "allbirdsstores.com").
NAME_EXTRAS = ("inc", "co", "llc", "corp", "hq", "group", "company", "usa", "us")


class SearchError(Exception):
    """The search engine refused or didn't answer. `message` is written for the person using the app."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Hit:
    url: str
    title: str
    content: str


@dataclass
class Searched:
    """Each query's results, what the searches cost (Google through Apify), and the Apify run to log."""

    hits: dict[str, list[Hit]]
    cost_usd: float | None = None
    run: Any = None
    notes: list[str] = field(default_factory=list)
    remaining: int | None = None  # Brave: free searches left this month, as Coldbrew counts them


class BraveSearch:
    """Brave's Search API, one search at a time, a little apart (its limit is per second)."""

    name = "Brave Search"

    def __init__(self, api_key: str, pause: float = 0.3, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(timeout=20.0, transport=transport,
                                         headers={"Accept": "application/json", "X-Subscription-Token": api_key})  # fmt: skip
        self._pause = pause
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, limit: int = 10) -> list[Hit]:
        async with self._lock:
            try:
                r = await self._client.get(BRAVE_URL, params={"q": query, "count": min(20, max(1, limit)), "country": "us",
                                                              "search_lang": "en"})  # fmt: skip
            except httpx.HTTPError as exc:
                raise SearchError(f"Couldn't reach Brave Search: {exc.__class__.__name__}") from exc
            finally:
                await asyncio.sleep(self._pause)
        if r.status_code == 429:
            raise SearchError("Brave Search refused more searches (its limit or credit is used up). "
                              "Choose Google (Apify) for web search on the next run.")  # fmt: skip
        if r.status_code in (401, 403):
            raise SearchError("Brave Search refused the key (BRAVE_API_KEY). Check it in the Brave dashboard.")
        if r.status_code >= 400:
            raise SearchError(f"Brave Search answered {r.status_code}.")
        try:
            items: list[dict[str, Any]] = (r.json().get("web") or {}).get("results") or []
        except ValueError as exc:
            raise SearchError("Brave Search didn't answer with JSON.") from exc
        return [Hit(str(i.get("url") or ""), _plain(i.get("title")), _plain(i.get("description"))) for i in items if i.get("url")][:limit]

    async def search_many(self, queries: list[str], limit: int = 10) -> Searched:
        """Every query, one after another; stops at the first refusal (what was found until then is kept)."""
        out = Searched({})
        for q in dict.fromkeys(queries):
            try:
                out.hits[q] = await self.search(q, limit)
            except SearchError as exc:
                out.notes.append(exc.message)
                break
        return out


class GoogleSearch:
    """Google results through Apify's Google Search actor: all the queries in one run, one results page each."""

    name = "Google (Apify)"

    def __init__(self, apify: Any) -> None:
        self._apify = apify

    async def aclose(self) -> None:
        pass  # the Apify client is closed by its own dependency

    async def search_many(self, queries: list[str], limit: int = 10) -> Searched:
        from services.apify import ApifyError

        wanted = list(dict.fromkeys(queries))
        if not wanted:
            return Searched({})
        actor_input = {"queries": "\n".join(wanted), "maxPagesPerQuery": 1, "countryCode": "us", "languageCode": "en",
                       "mobileResults": False, "saveHtml": False, "saveHtmlToKeyValueStore": False}  # fmt: skip
        try:
            run = await self._apify.run(GOOGLE_SEARCH_ACTOR, actor_input, len(wanted) + 5)
        except ApifyError as exc:
            return Searched({}, notes=[f"Google search (Apify): {exc.message}"])
        return Searched(google_hits(run.items, limit), cost_usd=run.cost_usd, run=run)


def google_hits(items: list[dict[str, Any]], limit: int = 10) -> dict[str, list[Hit]]:
    """The Google Search actor's pages, as each query's results."""
    out: dict[str, list[Hit]] = {}
    for page in items:
        term = str((page.get("searchQuery") or {}).get("term") or "")
        hits = [Hit(str(o.get("url") or ""), _plain(o.get("title")), _plain(o.get("description")))
                for o in page.get("organicResults") or [] if o.get("url")]  # fmt: skip
        out[term] = (out.get(term, []) + hits)[:limit]
    return out


def _plain(text: Any) -> str:
    """Brave marks the matched words with <strong>; plain text is wanted."""
    t = re.sub(r"<[^>]+>", "", str(text or ""))
    return t.replace("&amp;", "&").replace("&#x27;", "'").replace("&#39;", "'").replace("&quot;", '"')


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _words(name: str) -> list[str]:
    """The distinctive words of a company name (no legal suffixes), for checking a result is about it."""
    stop = {"inc", "llc", "ltd", "co", "corp", "company", "the", "and", "of", "group", "limited", "gmbh", "plc", "&"}
    return [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in stop and len(w) > 1]


def mentions(name: str, *texts: str) -> bool:
    """Whether the result names this company: most of its distinctive words as whole words, or the name written as one
    word (inkco.com, @inkco.atx, /InkCoATX). A word inside another word doesn't count ("ink" in "linkedin")."""
    words = _words(name)
    if not words:
        return False
    tokens = set(re.findall(r"[a-z0-9]+", " ".join(texts).lower()))
    if sum(1 for w in words if w in tokens) >= max(1, (len(words) + 1) // 2):
        return True
    joined = "".join(words)
    return len(joined) >= 4 and any(joined in tok for tok in tokens)


def _registered(host: str) -> list[str]:
    """The registered domain's parts: shop.inkco.co.uk -> [inkco, co, uk]; www.inkco.com -> [inkco, com]."""
    parts = host.split(".")
    n = 3 if len(parts) >= 3 and parts[-2] in SECOND_LEVEL and len(parts[-1]) == 2 else 2
    return parts[-n:]


def _name_label(host: str) -> str:
    """The part of a host that carries the name, without hyphens: shop.inkco.co.uk -> inkco."""
    return _registered(host)[0].replace("-", "")


def domain_is_named(name: str, host: str) -> bool:
    """Whether the domain is the company's name: all of it, or its distinctive words, maybe with one of NAME_EXTRAS."""
    label = _name_label(host)
    every = "".join(re.findall(r"[a-z0-9]+", name.lower().replace("&", "and")))
    distinct = "".join(_words(name))
    names = {n for n in (every, distinct) if len(n) >= 3}
    return any(label == n or (label.startswith(n) and label[len(n):] in NAME_EXTRAS) for n in names)


def pick_website(name: str, hits: list[Hit]) -> str | None:
    """The company's own site among the results: not a directory or a social network, the result names the company,
    the domain is the company's name, and it isn't another country's site. The registered domain is kept (a store
    locator's subdomain gives the main site). A near miss is left empty: better no website than a knock-off's."""
    for h in hits:
        host = _host(h.url)
        if not host or "." not in host or any(bad in host for bad in NOT_A_WEBSITE):
            continue
        ending = host.rsplit(".", 1)[1]
        if len(ending) == 2 and ending not in OK_COUNTRY_ENDINGS:
            continue
        if domain_is_named(name, host) and mentions(name, h.title, h.content, host):
            return normalize_domain(".".join(_registered(host)))
    return None


def pick_instagram(name: str, hits: list[Hit]) -> str | None:
    for h in hits:
        if _host(h.url) != "instagram.com":
            continue
        path = [p for p in urlparse(h.url).path.split("/") if p]
        if len(path) >= 1 and path[0].lower() not in INSTAGRAM_SKIP and mentions(name, h.title, h.content, path[0]):
            return f"https://www.instagram.com/{path[0]}/"
    return None


def pick_facebook(name: str, hits: list[Hit]) -> str | None:
    for h in hits:
        if _host(h.url) not in ("facebook.com", "m.facebook.com"):
            continue
        path = [p for p in urlparse(h.url).path.split("/") if p]
        if len(path) >= 1 and path[0].lower() not in FACEBOOK_SKIP and mentions(name, h.title, h.content, path[0]):
            return f"https://www.facebook.com/{path[0]}"
    return None


def pick_profiles(company: str, hits: list[Hit], limit: int) -> list[str]:
    """LinkedIn profile links of people who say they work at this company (the result names it)."""
    out: list[str] = []
    for h in hits:
        if _host(h.url).split(".")[-2:] != ["linkedin", "com"]:
            continue
        path = [p for p in urlparse(h.url).path.split("/") if p]
        if len(path) >= 2 and path[0] == "in" and mentions(company, h.title, h.content):
            url = f"https://www.linkedin.com/in/{path[1]}/"
            if url not in out:
                out.append(url)
        if len(out) >= limit:
            break
    return out


def quoted(text: str) -> str:
    return '"' + text.replace('"', "").strip() + '"'
