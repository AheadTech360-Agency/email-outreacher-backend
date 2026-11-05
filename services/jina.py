"""Jina AI Reader (r.jina.ai): a page's readable text, rendered in a real browser.

Used for sites our own polite fetch can't read: ones that only render with JavaScript, or that
turn away plain HTTP clients. Never used for a site whose robots.txt forbids us.

Rate limits (published per IP, or per key when one is sent): 20 requests/minute without a key,
500 with one, plus a tokens-per-minute cap. Two things keep us inside them:
  - a limiter shared by every request in this process, which spaces calls to `rpm` a minute. It never allows more
    than the published limit (MAX_RPM_*), whatever JINA_RPM says: don't remove it (tests/test_jina_and_website.py);
  - a 429 (or a 503 while overloaded) is retried after `Retry-After`, or with growing waits.
Failed requests aren't charged.
"""

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

READER_URL = "https://r.jina.ai/"
DEFAULT_RPM_WITHOUT_KEY = 18  # published limit 20
DEFAULT_RPM_WITH_KEY = 450  # published limit 500
MAX_RPM_WITHOUT_KEY = 20  # Jina's published limits: the most a minute, ever
MAX_RPM_WITH_KEY = 500
MAX_RETRIES = 4
MAX_WAIT_SECONDS = 60.0


class JinaError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class JinaPage:
    url: str
    title: str
    content: str
    links: list[str] = field(default_factory=list)


class RateLimiter:
    """At most `rpm` starts in any 60-second window, shared by everything that holds this limiter.

    After a 429 everyone waits until `blocked_until`, not just the caller that was refused.
    """

    def __init__(self, rpm: int, clock=time.monotonic, sleep=asyncio.sleep) -> None:
        self.rpm = max(1, rpm)
        self._starts: deque[float] = deque()
        self._blocked_until = 0.0
        self._locks: dict[int, asyncio.Lock] = {}
        self._clock = clock
        self._sleep = sleep

    def _lock(self) -> asyncio.Lock:
        # One lock per event loop. The limiter outlives a loop (it's shared by the whole process), and each background
        # job runs on a fresh loop: a single lock, tied to the first loop that waited on it, crashed the next job with
        # "bound to a different event loop" (Quick Start's Read websites, 2026-11-05). The count of starts is shared.
        loop = id(asyncio.get_running_loop())
        if loop not in self._locks:
            self._locks = {loop: asyncio.Lock()}  # the old loops are gone; their locks with them
        return self._locks[loop]

    async def wait_turn(self) -> None:
        async with self._lock():
            while True:
                now = self._clock()
                if now < self._blocked_until:
                    await self._sleep(self._blocked_until - now)
                    continue
                while self._starts and now - self._starts[0] >= 60:
                    self._starts.popleft()
                if len(self._starts) < self.rpm:
                    self._starts.append(now)
                    return
                await self._sleep(60 - (now - self._starts[0]) + 0.05)

    def back_off(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)


# One limiter per key (or for "no key"), shared across requests so concurrent jobs can't add up
# past the limit. Jina tracks limits per key, or per IP without one.
_limiters: dict[str, RateLimiter] = {}


def limiter_for(api_key: str | None, rpm: int | None) -> RateLimiter:
    name = api_key or "anonymous"
    wanted = min(rpm or (DEFAULT_RPM_WITH_KEY if api_key else DEFAULT_RPM_WITHOUT_KEY),
                 MAX_RPM_WITH_KEY if api_key else MAX_RPM_WITHOUT_KEY)
    if name not in _limiters or _limiters[name].rpm != wanted:
        _limiters[name] = RateLimiter(wanted)
    return _limiters[name]


def _retry_after(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header), MAX_WAIT_SECONDS)
        except ValueError:
            pass
    # 2, 4, 8, 16 seconds, with jitter so parallel callers don't retry in step.
    return min(2 ** (attempt + 1) + random.uniform(0, 1), MAX_WAIT_SECONDS)


def _links(data: dict[str, Any]) -> list[str]:
    links = data.get("links")
    if isinstance(links, dict):  # {"anchor text": "url", ...}
        return [v for v in links.values() if isinstance(v, str)]
    if isinstance(links, list):
        out = []
        for item in links:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], str):
                out.append(item[1])
            elif isinstance(item, dict) and isinstance(item.get("url") or item.get("href"), str):
                out.append(item.get("url") or item.get("href"))
        return out
    return []


class JinaClient:
    def __init__(
        self,
        api_key: str | None,
        rpm: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        limiter: RateLimiter | None = None,
        sleep=asyncio.sleep,
        reader_url: str = READER_URL,
    ) -> None:
        self._reader_url = reader_url
        headers = {
            "Accept": "application/json",
            "X-With-Links-Summary": "all",
            "X-Retain-Images": "none",
            "X-Timeout": "25",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(headers=headers, timeout=45, transport=transport)
        self._limiter = limiter or limiter_for(api_key, rpm)
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._client.aclose()

    async def read(self, url: str) -> JinaPage:
        for attempt in range(MAX_RETRIES + 1):
            await self._limiter.wait_turn()
            try:
                response = await self._client.get(self._reader_url + url)
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise JinaError(f"Couldn't reach Jina: {exc}") from exc
                await self._sleep(_retry_after(httpx.Response(599), attempt))
                continue
            if response.status_code in (429, 503):
                if attempt == MAX_RETRIES:
                    raise JinaError("Jina is rate-limiting us; try again in a minute.", 429)
                # The limiter holds every caller (not just this one) until the wait is over.
                self._limiter.back_off(_retry_after(response, attempt))
                continue
            if response.status_code == 401:
                raise JinaError("Jina rejected the API key. Check JINA_API_KEY.", 401)
            if response.status_code == 402:
                raise JinaError("Jina says the key is out of tokens.", 402)
            if response.status_code >= 400:
                raise JinaError(f"Jina couldn't read {url} ({response.status_code}).", response.status_code)
            try:
                body = response.json()
            except ValueError:
                # Plain-text answer (no JSON support for this page): the body is the content.
                return JinaPage(url=url, title="", content=response.text)
            data = body.get("data") if isinstance(body, dict) else None
            data = data if isinstance(data, dict) else {}
            return JinaPage(
                url=data.get("url") or url,
                title=data.get("title") or "",
                content=data.get("content") or data.get("text") or "",
                links=_links(data),
            )
        raise JinaError("Jina is rate-limiting us; try again in a minute.", 429)
