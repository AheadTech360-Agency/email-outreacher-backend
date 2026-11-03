"""Apify actors: LinkedIn company employees, LinkedIn profile posts, Instagram posts.

Each call starts a run, waits for it (up to RUN_TIMEOUT), then reads its dataset. Apify bills per
result; the run's actual cost (`usageTotalUsd`) is returned so it can be logged as spend.

Actors (all public, no cookies needed):
  harvestapi~linkedin-company-employees  $3 per 1,000 Basic profiles ("Short" mode) on Free and Starter
  harvestapi~linkedin-profile-posts      ~$2 per 1,000 posts
  apify~instagram-scraper                ~$1.50–2.70 per 1,000 results, by plan
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

BASE_URL = "https://api.apify.com/v2"
EMPLOYEES_ACTOR = "harvestapi~linkedin-company-employees"
LINKEDIN_POSTS_ACTOR = "harvestapi~linkedin-profile-posts"
INSTAGRAM_ACTOR = "apify~instagram-scraper"
INSTAGRAM_PROFILE_ACTOR = "apify~instagram-profile-scraper"
LINKEDIN_COMPANY_ACTOR = "harvestapi~linkedin-company"  # $0.004 a company page
LINKEDIN_PROFILE_ACTOR = "harvestapi~linkedin-profile-scraper"  # $0.004 a profile, no email search
PROFILE_DETAILS = "Profile details no email ($4 per 1k)"

RUN_TIMEOUT = 300  # seconds to wait for a run before giving up on it
TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}
COST_READS = 6  # reads of a finished run's cost while Apify settles it, COST_WAIT_SECONDS apart
COST_WAIT_SECONDS = 2


class ApifyError(Exception):
    """Apify refused or a run didn't finish. `message` is written for the person using the app."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class RunResult:
    items: list[dict[str, Any]]
    cost_usd: float | None
    run_id: str | None
    # People LinkedIn's name search returned who work at another company, dropped.
    skipped_elsewhere: int = 0


@dataclass
class Employee:
    external_id: str
    first_name: str
    last_name: str
    title: str
    linkedin_url: str | None
    company_name: str | None
    raw: dict[str, Any]
    # With the email search: their best work email and how sure Apify is (valid | risky | ...), else None.
    email: str | None = None
    email_status: str | None = None


# The LinkedIn people actor's modes (its input's profileScraperMode). Email search costs $0.012 a person (Bronze) and
# returns the full profile too; the short one $0.003, no email.
SHORT_PROFILES = "Short ($4 per 1k)"
PROFILES_WITH_EMAIL = "Full + email search ($12 per 1k)"


# A Google Maps listing name carries SEO text after a separator ("Quick Transfers - Same Day Custom DTF
# Transfers"); LinkedIn knows the company by the part before it.
_SEPARATORS = re.compile(r"\s+[-|–—:]\s+|\s*\|\s*")
_LEGAL = re.compile(r"\b(llc|inc|incorporated|co|corp|corporation|company|ltd|limited|pllc|lp)\b")


def search_name(name: str) -> str:
    """The company name to search LinkedIn for: the listing name without its SEO tail."""
    head = _SEPARATORS.split(name.strip(), maxsplit=1)[0].strip()
    return head or name.strip()


def _key(name: str | None) -> str:
    """Letters and digits only, legal suffixes dropped: "Quick Transfers, LLC" and "QuickTransfers" match."""
    return re.sub(r"[^a-z0-9]", "", _LEGAL.sub("", (name or "").lower()))


def company_keys(name: str, domain: str | None = None) -> set[str]:
    """What a person's current employer may be called and still be this company: the listing name, its
    searchable part, and the website's name (quicktransfers.com -> quicktransfers)."""
    keys = {_key(name), _key(search_name(name))}
    if domain:
        keys.add(_key(domain.lower().removeprefix("www.").split(".")[0]))
    return {k for k in keys if len(k) >= 3}


def _same_page(a: str | None, b: str | None) -> bool:
    def slug(u: str | None) -> str:
        return (urlsplit(u or "").path or "").rstrip("/").lower().removeprefix("/company/")

    return bool(a and b and slug(a) and slug(a) == slug(b))


def current_job_at(item: dict[str, Any], keys: set[str], linkedin_url: str | None) -> dict[str, Any] | None:
    """The person's current position at this company, or None when LinkedIn's name search matched someone
    who works somewhere else (searching "Quick Transfers" found a freelancer at "Quick Airport Transfers")."""
    for position in item.get("currentPositions") or item.get("currentPosition") or []:
        position = position or {}
        company = position.get("company") or {}
        url = position.get("companyLinkedinUrl") or company.get("linkedinUrl")
        if _same_page(url, linkedin_url) or _key(position.get("companyName") or company.get("name")) in keys:
            return position
    return None


def elsewhere(n: int) -> str | None:
    """The note for people a name search found at other companies."""
    if not n:
        return None
    return f"{n} {'person' if n == 1 else 'people'} at a company with a similar name left out"


def best_email(item: dict[str, Any]) -> tuple[str | None, str | None]:
    """The email to use from an email search: valid and deliverable first, then the highest quality score. Free
    addresses (gmail and the like) only when there's nothing else."""
    found = [e for e in item.get("emails") or [] if isinstance(e, dict) and "@" in str(e.get("email") or "")]
    if not found:
        return None, None
    rank = lambda e: (not e.get("free"), e.get("status") == "valid", bool(e.get("deliverable")), e.get("qualityScore") or 0)  # noqa: E731
    best = max(found, key=rank)
    return str(best["email"]).strip().lower(), str(best.get("status") or "unknown")


@dataclass
class FetchedPost:
    source: str  # linkedin | instagram
    url: str
    external_id: str | None
    posted_at: datetime | None
    text: str
    media_type: str  # text | image | video | carousel | article
    is_repost: bool
    # Which input this post belongs to: a LinkedIn profile slug, or an Instagram username.
    owner_key: str | None
    raw: dict[str, Any]


@dataclass
class InstagramProfile:
    handle: str
    full_name: str
    bio: str
    category: str | None
    followers: int | None
    private: bool
    verified: bool
    external_url: str | None
    raw: dict[str, Any]
    # The profile's latest posts, as the profile scraper returns them (usually up to 12).
    posts: list[FetchedPost] = field(default_factory=list)


INSTAGRAM_KINDS = {"Image": "image", "Video": "video", "Sidecar": "carousel"}


def _instagram_post(item: dict[str, Any], owner: str | None) -> FetchedPost | None:
    if not item.get("url") or item.get("error"):
        return None
    return FetchedPost(
        source="instagram",
        url=item["url"],
        external_id=str(item["id"]) if item.get("id") else None,
        posted_at=_date(item.get("timestamp")),
        text=item.get("caption") or "",
        media_type=INSTAGRAM_KINDS.get(item.get("type") or "", "image"),
        is_repost=False,
        owner_key=owner,
        raw=item,
    )


def linkedin_slug(url: str | None) -> str | None:
    """'https://www.linkedin.com/in/Ava-Morgan/' -> 'ava-morgan'."""
    if not url:
        return None
    match = re.search(r"linkedin\.com/(?:in|company)/([^/?#]+)", url, re.I)
    return match.group(1).lower() if match else None


# Instagram's own routes, which are nobody's profile.
INSTAGRAM_ROUTES = {"p", "reel", "reels", "tv", "explore", "stories", "s", "accounts", "direct", "about"}
# Accounts that belong to a platform a business links to, not to the business. From Lead Intel: a
# salon's site built on Booksy links Booksy's Instagram, and its 142,000 followers end up reported
# as the salon's.
PLATFORM_ACCOUNTS = {
    "booksybiz", "booksy", "vagaro", "glossgenius", "styleseat", "fresha", "schedulicity", "setmore",
    "acuityscheduling", "square", "squareup", "squarespace", "wix", "shopify", "godaddy", "weebly",
    "webflow", "bigcartel", "wordpress", "wordpressdotcom", "linktree", "instagram", "facebook", "meta",
    "whatsapp", "etsy", "yelp",
}  # fmt: skip
HANDLE = re.compile(r"^[a-z0-9._]{1,30}$")


def instagram_handle(url: str | None) -> str | None:
    """'https://instagram.com/samedaycustom/' or '@samedaycustom' -> 'samedaycustom'. Post and reel
    URLs, Instagram's own pages and platform accounts give None."""
    if not url or not url.strip():
        return None
    value = url.strip()
    if "instagram.com" not in value.lower():
        handle = value.lstrip("@").lower()
    else:
        path = [p for p in urlsplit(value if "://" in value else f"https://{value}").path.split("/") if p]
        if not path or path[0].lower() in INSTAGRAM_ROUTES:
            return None
        handle = path[0].lstrip("@").lower()
    if not HANDLE.match(handle) or handle in PLATFORM_ACCOUNTS:
        return None
    return handle


def _message(response: httpx.Response) -> str:
    try:
        error = response.json().get("error") or {}
        detail = error.get("message") or error.get("type")
    except ValueError:
        detail = response.text[:200]
    if response.status_code == 401:
        return "Apify rejected the token. Check APIFY_TOKEN."
    if response.status_code == 402:
        return f"Apify says the account is out of credit or over its plan limit. {detail or ''}".strip()
    if response.status_code == 429:
        return "Apify's rate limit was reached. Try again in a minute."
    return f"Apify answered {response.status_code}: {detail or 'no details'}"


def _date(value: Any) -> datetime | None:
    if isinstance(value, dict):
        value = value.get("date") or value.get("timestamp")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, UTC)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


class ApifyClient:
    def __init__(
        self, token: str, transport: httpx.AsyncBaseTransport | None = None, poll_seconds: float = 5, base_url: str = BASE_URL
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=90, transport=transport
        )
        self._poll = poll_seconds

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApifyError(f"Couldn't reach Apify: {exc}") from exc
        if response.status_code >= 400:
            raise ApifyError(_message(response), response.status_code)
        return response.json()

    async def get(self, path: str, **params: Any) -> Any:
        """A read from Apify's API: the account, an actor's details and prices. Free."""
        return await self._request("GET", path, params=params or None)

    async def run(self, actor: str, actor_input: dict[str, Any], max_items: int) -> RunResult:
        """Start a run, wait for it, and read up to `max_items` results."""
        run = (await self._request("POST", f"/acts/{actor}/runs", json=actor_input, params={"waitForFinish": 60}))["data"]
        waited = 60
        while run.get("status") not in TERMINAL and waited < RUN_TIMEOUT:
            await asyncio.sleep(self._poll)
            run = (await self._request("GET", f"/actor-runs/{run['id']}", params={"waitForFinish": 60}))["data"]
            waited += 60
        status = run.get("status")
        if status not in TERMINAL:
            raise ApifyError(f"The Apify run took over {RUN_TIMEOUT // 60} minutes. It's still running in Apify (run {run['id']}).")
        if status != "SUCCEEDED":
            raise ApifyError(f"The Apify run ended as {status}. {run.get('statusMessage') or ''}".strip())
        # Some actors "succeed" with no results when the account hits a plan limit, and only say so
        # in the status message (e.g. "free user run limit exceeded").
        note = (run.get("statusMessage") or "").lower()
        if "limit exceeded" in note or "upgrade" in note:
            raise ApifyError(
                f"Apify stopped the run: {run.get('statusMessage')}. The actor's free runs are used up; "
                "a paid Apify plan (or the actor's paid tier) is needed.",
                402,
            )
        items = await self._request(
            "GET", f"/datasets/{run['defaultDatasetId']}/items", params={"clean": "true", "format": "json", "limit": max_items}
        )
        # Apify settles a run's cost in the seconds after it finishes: it can read 0, then only the start fee,
        # then the full charge (a "no result" event lands last). Read until two reads agree.
        cost, previous = run.get("usageTotalUsd"), None
        for _ in range(COST_READS):
            if cost and cost == previous:
                break
            previous = cost
            await asyncio.sleep(min(self._poll, COST_WAIT_SECONDS))
            cost = (await self._request("GET", f"/actor-runs/{run['id']}"))["data"].get("usageTotalUsd")
        return RunResult(items=items if isinstance(items, list) else [], cost_usd=float(cost) if cost is not None else None, run_id=run.get("id"))

    # ---- Actors ----

    async def company_employees(
        self, company: str, job_titles: list[str], max_items: int, with_email: bool = False,
        linkedin_url: str | None = None, domain: str | None = None,
    ) -> tuple[list[Employee], RunResult]:
        """People at one company on LinkedIn: by its LinkedIn page when known, otherwise by its name. Only people
        whose current job is at this company are kept. With `with_email`, each person's work email is searched
        for too (and their full profile comes back)."""
        keys = company_keys(company, domain)
        actor_input: dict[str, Any] = {
            "companies": [linkedin_url or search_name(company)],
            "profileScraperMode": PROFILES_WITH_EMAIL if with_email else SHORT_PROFILES,
            "maxItems": max_items,
            "companyBatchMode": "all_at_once",
        }
        if job_titles:
            actor_input["jobTitles"] = job_titles
        result = await self.run(EMPLOYEES_ACTOR, actor_input, max_items)
        people = []
        for item in result.items:
            url = item.get("linkedinUrl")
            key = item.get("id") or item.get("publicIdentifier") or linkedin_slug(url)
            if not key:
                continue
            # The actor's current output has `currentPositions` ({title, companyName, company}); older runs had
            # `currentPosition` ({position, companyName}).
            # Someone whose current jobs are all at another company is dropped; with none listed, the actor's own
            # company filter is all there is to go on, so they're kept.
            listed = item.get("currentPositions") or item.get("currentPosition") or []
            position = current_job_at(item, keys, linkedin_url) if listed else {}
            if position is None:
                result.skipped_elsewhere += 1
                continue
            email, email_status = best_email(item)
            people.append(
                Employee(
                    external_id=str(key),
                    first_name=item.get("firstName") or "",
                    last_name=item.get("lastName") or "",
                    title=position.get("title") or position.get("position") or item.get("headline") or "",
                    linkedin_url=url,
                    company_name=position.get("companyName") or (position.get("company") or {}).get("name"),
                    raw=item,
                    email=email,
                    email_status=email_status,
                )
            )
        return people, result

    async def linkedin_posts(self, profile_urls: list[str], max_posts: int, since: datetime) -> tuple[list[FetchedPost], RunResult]:
        """Recent posts by these LinkedIn profiles."""
        actor_input = {
            "targetUrls": profile_urls,
            "maxPosts": max_posts,
            "postedLimitDate": since.strftime("%Y-%m-%d"),
            "includeQuotePosts": True,
            "includeReposts": True,
            "scrapeReactions": False,
            "scrapeComments": False,
        }
        result = await self.run(LINKEDIN_POSTS_ACTOR, actor_input, max_posts * len(profile_urls) + 10)
        posts = []
        for item in result.items:
            if item.get("type") not in (None, "post") or not item.get("linkedinUrl"):
                continue
            author = item.get("author") or {}
            media = "image" if item.get("postImages") else "article" if item.get("document") or item.get("article") else "text"
            # `query` says which input URL a post came from: more reliable than the author, who is
            # someone else on a repost.
            query = item.get("query")
            query_url = query if isinstance(query, str) else next(
                (v for v in (query or {}).values() if isinstance(v, str) and "linkedin.com" in v), None
            )
            header = item.get("header")
            header_text = (header.get("text") if isinstance(header, dict) else header) or ""
            posts.append(
                FetchedPost(
                    source="linkedin",
                    url=item["linkedinUrl"],
                    external_id=str(item["id"]) if item.get("id") else None,
                    posted_at=_date(item.get("postedAt")),
                    text=item.get("content") or "",
                    media_type=media,
                    # The actor has no explicit repost flag; a repost is a post by someone else.
                    is_repost=bool(item.get("repost") or item.get("isRepost") or item.get("resharedPost"))
                    or "repost" in str(header_text).lower(),
                    owner_key=(
                        linkedin_slug(query_url)
                        or author.get("publicIdentifier")
                        or linkedin_slug(author.get("linkedinUrl"))
                        or ""
                    ).lower()
                    or None,
                    raw=item,
                )
            )
        return posts, result

    async def instagram_posts(self, profile_urls: list[str], max_posts: int, since: datetime) -> tuple[list[FetchedPost], RunResult]:
        """Recent posts from these public Instagram profiles."""
        actor_input = {
            "directUrls": profile_urls,
            "resultsType": "posts",
            "resultsLimit": max_posts,
            "onlyPostsNewerThan": since.strftime("%Y-%m-%d"),
            "addParentData": False,
        }
        result = await self.run(INSTAGRAM_ACTOR, actor_input, max_posts * len(profile_urls) + 10)
        posts = []
        for item in result.items:
            owner = (item.get("ownerUsername") or instagram_handle(item.get("inputUrl")) or "").lower() or None
            if post := _instagram_post(item, owner):
                posts.append(post)
        return posts, result

    async def linkedin_companies(self, urls: list[str]) -> tuple[dict[str, dict[str, Any]], RunResult]:
        """LinkedIn company pages (description, size, industry, address, specialties...), keyed by the page's slug."""
        result = await self.run(LINKEDIN_COMPANY_ACTOR, {"companies": urls}, len(urls) + 5)
        out: dict[str, dict[str, Any]] = {}
        wanted = {linkedin_slug(u): u for u in urls}
        for item in result.items:
            slug = next((s for s in (linkedin_slug(item.get("linkedinUrl")), linkedin_slug(str(item.get("query") or "")),
                                     (item.get("universalName") or "").lower()) if s and s in wanted), None)  # fmt: skip
            if slug is None and len(urls) == 1:
                slug = linkedin_slug(urls[0])
            if slug:
                out[slug] = item
        return out, result

    async def linkedin_profiles(self, urls: list[str]) -> tuple[dict[str, dict[str, Any]], RunResult]:
        """Full LinkedIn profiles (about, experience, education, skills), keyed by the profile's slug."""
        result = await self.run(LINKEDIN_PROFILE_ACTOR, {"urls": urls, "profileScraperMode": PROFILE_DETAILS}, len(urls) + 5)
        out: dict[str, dict[str, Any]] = {}
        wanted = {linkedin_slug(u) for u in urls}
        for item in result.items:
            query = item.get("query")
            query_url = query if isinstance(query, str) else next((v for v in (query or {}).values() if isinstance(v, str)), None)
            slug = next((s for s in (linkedin_slug(query_url), (item.get("publicIdentifier") or "").lower(),
                                     linkedin_slug(item.get("linkedinUrl"))) if s and s in wanted), None)  # fmt: skip
            if slug is None and len(urls) == 1:
                slug = linkedin_slug(urls[0])
            if slug:
                out[slug] = item
        return out, result

    async def instagram_profiles(self, handles: list[str]) -> tuple[dict[str, InstagramProfile], RunResult]:
        """Profiles (bio, category, followers) for these usernames, keyed by lower-case handle.

        The same actor Lead Intel uses. Handles come from the company's own website, never a search.
        """
        wanted = list(dict.fromkeys(h.lower().lstrip("@") for h in handles if h))
        result = await self.run(INSTAGRAM_PROFILE_ACTOR, {"usernames": wanted}, len(wanted) + 5)
        profiles: dict[str, InstagramProfile] = {}
        for item in result.items:
            handle = (item.get("username") or "").lower()
            if not handle or item.get("error"):
                continue
            followers = item.get("followersCount")
            profiles[handle] = InstagramProfile(
                handle=handle,
                full_name=item.get("fullName") or "",
                bio=item.get("biography") or "",
                category=item.get("businessCategoryName"),
                followers=int(followers) if isinstance(followers, (int, float)) else None,
                private=bool(item.get("private")),
                verified=bool(item.get("verified")),
                external_url=item.get("externalUrl"),
                raw={k: v for k, v in item.items() if k not in ("latestPosts", "latestIgtvVideos", "relatedProfiles")},
                posts=[p for post in item.get("latestPosts") or [] if (p := _instagram_post(post, handle))],
            )
        return profiles, result
