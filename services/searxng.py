"""Web search through a self-hosted SearXNG instance (its JSON API), used by Quick Start's LinkedIn option to find a
company's website, Instagram and Facebook, and LinkedIn profiles of people there. Free: SearXNG asks the search
engines itself. The instance must have `json` in `search.formats`."""

import asyncio
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from services.website import normalize_domain

# Sites that list businesses but aren't the business's own website.
NOT_A_WEBSITE = (
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com", "youtube.com", "pinterest.",
    "yelp.", "yellowpages.", "bbb.org", "crunchbase.com", "zoominfo.com", "dnb.com", "bloomberg.com", "glassdoor.",
    "indeed.", "mapquest.com", "google.", "apple.com/maps", "amazon.", "ebay.", "etsy.com", "wikipedia.org", "manta.com",
    "opencorporates.com", "trustpilot.", "tripadvisor.", "angi.com", "thumbtack.com", "houzz.com", "chamberofcommerce.com",
)  # fmt: skip
INSTAGRAM_SKIP = {"p", "reel", "reels", "explore", "stories", "tv", "accounts", "about", "developer", "legal"}
FACEBOOK_SKIP = {"pages", "groups", "events", "watch", "marketplace", "share", "sharer", "login", "help", "people", "story.php", "photo.php", "permalink.php"}


class SearchError(Exception):
    """SearXNG refused or didn't answer. `message` is written for the person using the app."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Hit:
    url: str
    title: str
    content: str


class SearxClient:
    def __init__(self, base_url: str, timeout: float = 20.0, pause: float = 1.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport, headers={"Accept": "application/json"})
        self._pause = pause  # between searches, so the engines behind SearXNG don't start refusing it
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, limit: int = 10) -> list[Hit]:
        async with self._lock:
            try:
                r = await self._client.get("/search", params={"q": query, "format": "json", "categories": "general", "safesearch": 0})
            except httpx.HTTPError as exc:
                raise SearchError(f"Couldn't reach the search service (SearXNG): {exc.__class__.__name__}") from exc
            finally:
                await asyncio.sleep(self._pause)
        if r.status_code == 403:
            raise SearchError("The search service (SearXNG) refused JSON: turn on `json` under search.formats in its settings.")
        if r.status_code >= 400:
            raise SearchError(f"The search service (SearXNG) answered {r.status_code}.")
        try:
            items: list[dict[str, Any]] = r.json().get("results") or []
        except ValueError as exc:
            raise SearchError("The search service (SearXNG) didn't answer with JSON.") from exc
        return [Hit(str(i.get("url") or ""), str(i.get("title") or ""), str(i.get("content") or "")) for i in items if i.get("url")][:limit]


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


def pick_website(name: str, hits: list[Hit]) -> str | None:
    """The company's own site among the results: not a directory or a social network, and about this company."""
    for h in hits:
        host = _host(h.url)
        if not host or any(bad in host for bad in NOT_A_WEBSITE):
            continue
        if mentions(name, h.title, h.content, host):
            return normalize_domain(host)
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
