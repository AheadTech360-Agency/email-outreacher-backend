"""The cost estimator's starting point, until a workspace saves its own: one line per operation that costs
money, plus the subscriptions.

Prices are read-only and set on the server every time (see `refresh`): COST_PRICES first, then Apify's own
price for the account's plan tier (services/sources.py), then the price here. Claude lines are priced from
tokens per use and the model (services/claude.py); Google Maps from Coldbrew's own table
(services/google_places.py). Quantities are only filled in where Coldbrew's own process fixes them (one
email and two follow-ups per lead, one reveal per lead, five posts per source by default); the rest start
at 0.

Verification lines count per company verified (`companies_per_month`); analysis lines per lead.
"""

from typing import Any

from services.claude import PRICES
from services.google_places import CENTER_COST_USD, SEARCH_COST_USD
from services.sources import SOURCES

CLAUDE_MODEL = "claude-haiku-5-5"
# Apify's published Free-tier rates, used when Apify can't be asked.
LINKEDIN_POST_USD = 0.002
INSTAGRAM_POST_USD = 0.0027


def claude_price(model: str, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = PRICES.get(model, PRICES[CLAUDE_MODEL])
    return round((tokens_in * price_in + tokens_out * price_out) / 1e6, 6)


def _claude(id_: str, name: str, unit: str, action: str | None, tokens_in: int, tokens_out: int, quantity: float, basis: str,
            note: str, category: str = "AI (Claude)") -> dict[str, Any]:  # fmt: skip
    return {"id": id_, "name": name, "category": category, "unit_label": unit, "model": CLAUDE_MODEL,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "price_usd": claude_price(CLAUDE_MODEL, tokens_in, tokens_out),
            "quantity": quantity, "basis": basis, "action": action, "note": note, "enabled": True, "planned": False, "free": False}  # fmt: skip


def _item(id_: str, name: str, category: str, unit: str, price: float, quantity: float, basis: str, note: str) -> dict[str, Any]:
    return {"id": id_, "name": name, "category": category, "unit_label": unit, "price_usd": price, "quantity": quantity,
            "basis": basis, "model": None, "tokens_in": None, "tokens_out": None, "action": None, "note": note, "enabled": True,
            "planned": False, "free": False}  # fmt: skip


# Estimate lines that are sources (services/sources.py): their price comes from Apify, and planned ones are marked.
LINE_SOURCE = {
    "instagram-profile": "verification:instagram",
    "facebook-page": "verification:facebook_page",
    "linkedin-company": "verification:linkedin_company",
    "google-reviews": "verification:google_reviews",
    "tiktok-profile": "verification:tiktok_profile",
    "youtube-channel": "verification:youtube_channel",
    "linkedin-posts": "analysis:linkedin",
    "instagram-posts": "analysis:instagram",
    "facebook-posts": "analysis:facebook",
    "linkedin-company-posts": "analysis:linkedin_company_posts",
    "x-posts": "analysis:x",
    "tiktok-videos": "analysis:tiktok",
    "youtube-videos": "analysis:youtube",
}
PLANNED = {f"{s.kind}:{s.id}" for s in SOURCES if s.status == "planned"}
# Lines that cost nothing as Coldbrew runs them, until COST_PRICES gives them a price (a Jina key, say).
FREE = {"website-pages"}
VERIFY = "Verification"
ANALYSIS = "Lead analysis"
APIFY = "Apify's price for your plan, read from Apify."

DEFAULT_ESTIMATE: dict[str, Any] = {
    "leads_per_month": 2000,
    "emails_per_lead": 3,
    "reply_rate_pct": 0,
    "companies_per_month": 0,
    "items": [
        # Writing (Claude)
        _claude("email-generation", "Email generation (hooks, email, score)", "per lead written", "Write emails", 10182, 2562, 1, "lead",
                "Tokens per lead measured on 30 Oct 2026 (2 leads). More posts or a bigger model cost more."),
        _claude("follow-up", "Follow-up writing", "per follow-up", "Write follow-ups", 2017, 933, 2, "lead",
                "Measured on 30 Oct 2026. Only written for leads who haven't replied, so fewer with a high reply rate."),
        _claude("prompt-improvement", "Prompt improvement", "per run", "Improve prompt", 0, 0, 0, "month",
                "No runs measured yet: enter tokens once you've used it, or leave at 0."),
        # Finding companies
        _item("maps-search", "Google Maps search", "Finding companies", "per search", SEARCH_COST_USD, 0, "month",
              "Coldbrew's price table. Each search term in each place is one search: enter searches a month."),
        _item("maps-location", "Google Maps location lookup", "Finding companies", "per place looked up", CENTER_COST_USD, 0, "month",
              "Coldbrew's price table. One lookup per place searched around."),
        _item("apollo-companies", "Apollo company search", "Finding companies", "per page", 0, 0, "month",
              "Enter your Apollo plan's cost per search page in COST_PRICES."),
        # Finding people
        _item("apollo-people", "Apollo people search", "Finding people", "per search", 0, 0, "month",
              "Enter your Apollo plan's cost per search in COST_PRICES."),
        _item("apify-linkedin-people", "LinkedIn people + work emails (Apify)", "Finding people", "per profile", 0.012, 0, "month",
              "Apify's Bronze price with the email search, on 2 Nov 2026 ($0.003 without emails), plus $0.02 each time a company is searched: enter profiles a month."),
        _item("apollo-reveal", "Email reveal (Apollo)", "Finding people", "per email revealed", 0, 1, "lead",
              "One Apollo credit per revealed email: enter your plan's price per credit in COST_PRICES."),
        # Verification: the sources used to decide whether a business is worth contacting (per company verified)
        _claude("verification", "Claude's fit score", "per company", "Research companies", 4992, 386, 1, "company",
                "Measured on 30 Oct 2026 (1 company), with the server's scoring model.", VERIFY),
        _item("website-pages", "Website pages (Jina)", VERIFY, "per page", 0, 1, "company",
              "Coldbrew's own reader is free; Jina only reads blocked sites, free up to its rate limit without a key."),
        _item("instagram-profile", "Instagram profile", VERIFY, "per profile", 0.0026, 1, "company", APIFY),
        _item("facebook-page", "Facebook page", VERIFY, "per page", 0.012, 0, "company", APIFY),
        _item("linkedin-company", "LinkedIn company page", VERIFY, "per company", 0.004, 0, "company", APIFY),
        _item("google-reviews", "Google reviews", VERIFY, "per review", 0.0006, 0, "company", APIFY),
        _item("tiktok-profile", "TikTok profile", VERIFY, "per result", 0.003, 0, "company", APIFY),
        _item("youtube-channel", "YouTube channel", VERIFY, "per video", 0.0013, 0, "company", APIFY),
        # Lead analysis: posts read to find something to open the email with (per lead)
        _item("linkedin-posts", "LinkedIn posts", ANALYSIS, "per post", LINKEDIN_POST_USD, 5, "lead",
              "5 posts per lead is the Settings default. " + APIFY),
        _item("instagram-posts", "Instagram posts", ANALYSIS, "per post", INSTAGRAM_POST_USD, 5, "lead",
              "5 posts per lead is the Settings default. " + APIFY),
        _item("facebook-posts", "Facebook posts", ANALYSIS, "per post", 0.005, 0, "lead", APIFY),
        _item("linkedin-company-posts", "Company LinkedIn posts", ANALYSIS, "per post", 0.002, 0, "lead", APIFY),
        _item("x-posts", "X posts", ANALYSIS, "per post", 0.0004, 0, "lead", APIFY),
        _item("tiktok-videos", "TikTok videos", ANALYSIS, "per video", 0.0037, 0, "lead", APIFY),
        _item("youtube-videos", "YouTube videos", ANALYSIS, "per video", 0.004, 0, "lead", APIFY),
        # Subscriptions
        _item("cheapsequencer", "CheapSequencer Starter", "Sending", "per month", 29, 1, "month",
              "10,000 campaign sends a month; warmup is free and unlimited. CheapSequencer's published price."),
        _item("cheapinboxes", "CheapInboxes (16 mailboxes)", "Sending", "per month", 56, 1, "month",
              "From your CheapInboxes billing on 30 Oct 2026."),
        _item("railway", "Railway (API, worker, beat, Redis)", "Hosting", "per month", 0, 1, "month", "Enter your Railway bill in COST_PRICES."),
        _item("vercel", "Vercel (frontend)", "Hosting", "per month", 0, 1, "month", "Enter your Vercel plan in COST_PRICES."),
        _item("supabase", "Supabase (database and sign-in)", "Hosting", "per month", 0, 1, "month", "Enter your Supabase plan in COST_PRICES."),
    ],
}  # fmt: skip
for _line in DEFAULT_ESTIMATE["items"]:
    _line["free"] = _line["id"] in FREE

_LEGACY_UNIT_LABEL = {"month": "per month", "lead": "per lead", "email": "per email sent"}
VERIFICATION = "verification"


def upgrade(saved: dict[str, Any]) -> dict[str, Any]:
    """An estimate saved in an older shape: items with one amount (before price x quantity), and
    verification counted a month (before companies per month and per-company counts)."""
    items = []
    companies = saved.get("companies_per_month")
    for item in saved.get("items", []):
        if "price_usd" not in item:
            unit = item.get("unit", "month")
            item = {**{k: v for k, v in item.items() if k not in ("unit", "amount_usd")}, "price_usd": item.get("amount_usd", 0),
                    "quantity": 1, "basis": unit, "unit_label": _LEGACY_UNIT_LABEL.get(unit, "per month"),
                    "model": None, "tokens_in": None, "tokens_out": None, "action": None}  # fmt: skip
        if companies is None and item["id"] in (VERIFICATION, "instagram-profile", "website-pages") and item.get("basis") == "month":
            # Verification used to be "N a month": N companies, one of each per company (pages: pages per company).
            if item["id"] == VERIFICATION:
                saved = {**saved, "companies_per_month": int(item.get("quantity") or 0)}
            per_company = saved.get("pages_per_company", 1) if item["id"] == "website-pages" else 1
            item = {**item, "basis": "company", "quantity": per_company}
        items.append(item)
    out = {k: v for k, v in saved.items() if k != "pages_per_company"}
    return {**out, "companies_per_month": out.get("companies_per_month") or 0, "items": items}


def _priced_line(item: dict[str, Any], scoring_model: str, overrides: dict[str, float],
                 apify: dict[str, tuple[float | None, float | None]] | None) -> dict[str, Any]:  # fmt: skip
    """One line with today's price, name and place."""
    item = {"planned": False, "free": False, **item}
    start = _KNOWN.get(item["id"])
    source = LINE_SOURCE.get(item["id"])
    if item["id"] == VERIFICATION:
        item["model"] = scoring_model
    if item.get("model") and item.get("tokens_in") is not None and item.get("tokens_out") is not None:
        item["price_usd"] = claude_price(item["model"], item["tokens_in"], item["tokens_out"])
    elif item["id"] in overrides:
        item["price_usd"] = overrides[item["id"]]
    elif source and apify and apify.get(source, (None, None))[0] is not None:
        item["price_usd"] = apify[source][0]
    elif start is not None:
        item["price_usd"] = start["price_usd"]
    if start is not None:
        # Lines Coldbrew knows keep their place: verification sources, lead analysis, and so on.
        item.update(category=start["category"], name=start["name"], unit_label=start["unit_label"], basis=start["basis"])
    item["planned"] = source in PLANNED
    item["free"] = item["id"] in FREE and not item["price_usd"]
    return item


_KNOWN = {item["id"]: item for item in DEFAULT_ESTIMATE["items"]}


def refresh(estimate: dict[str, Any], scoring_model: str, overrides: dict[str, float],
            apify: dict[str, tuple[float | None, float | None]] | None = None) -> dict[str, Any]:  # fmt: skip
    """Prices are read-only: every line Coldbrew knows gets today's price (COST_PRICES, then Apify's price for
    the plan tier, then the starting price), Claude lines are priced from their tokens and model, and company
    verification uses the server's scoring model. Lines Coldbrew has added since the estimate was saved are
    added (at their starting quantity), and planned sources are marked. Whatever price a saved estimate or
    the browser carried is ignored."""
    items = [_priced_line(item, scoring_model, overrides, apify) for item in estimate.get("items", [])]
    seen = {item["id"] for item in items}
    items += [_priced_line(start, scoring_model, overrides, apify) for id_, start in _KNOWN.items() if id_ not in seen]
    return {**estimate, "items": items}
