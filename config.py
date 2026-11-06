from functools import lru_cache
from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a local .env file).

    Provider keys are optional here so the app boots without them; each integration
    checks for its own key when it is called.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Coldbrew API"
    environment: str = "development"

    # The only callers allowed: comma-separated origins of the frontend (scheme, host and port, like
    # https://app.example.com). A request whose Origin or Referer isn't one of these is refused, and
    # CORS allows only these. Empty means none: every browser-originated request is refused.
    authorized_urls: str = Field(default="")
    # Shared secret the frontend's server sends in X-Coldbrew-Secret with every API call. Without it set,
    # the API refuses everything (except /health), so a missing setting never leaves it open.
    api_secret: str | None = None

    # Shared Lead Intel database (Supabase Postgres).
    database_url: str | None = None
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    # Where Supabase publishes the public keys its sign-in tokens are signed with. Defaults to the
    # project's standard address.
    supabase_jwks_url: str | None = None

    # Lead Intel user that new ICPs are recorded as created by (Lead Intel requires one). Until this
    # app has sign-in, every ICP added here is attributed to this account.
    lead_intel_icp_created_by: UUID | None = None

    # Claude, for scoring companies against an ICP (and later, writing emails).
    anthropic_api_key: str | None = None
    claude_scoring_model: str = "claude-haiku-5-5"
    # A researched company qualifies when it passes the ICP's rule and scores at least this.
    company_score_threshold: int = 60

    # Providers.
    apollo_api_key: str | None = None
    apify_token: str | None = None
    # Google Places API (New), for finding companies on Google Maps.
    google_places_api_key: str | None = None
    openai_api_key: str | None = None
    # Jina Reader, for websites that need a browser to render. Works without a key at a lower limit.
    jina_api_key: str | None = None
    # Requests per minute to allow Jina. Defaults sit just under its published limits
    # (20 without a key, 500 with one) so a burst never trips them.
    jina_rpm: int | None = None
    # Sending: CheapSequencer runs the campaigns; CheapInboxes runs the mailboxes and their domains.
    cheapsequencer_api_key: str | None = None
    cheapinboxes_api_key: str | None = None
    cheapsequencer_webhook_secret: str | None = None

    # Where each outside service is reached. The defaults are the providers' public APIs; point one at a
    # proxy or a test server by setting it.
    cheapsequencer_api_url: str = "https://api.cheapsequencer.com/v1"
    cheapinboxes_api_url: str = "https://api.cheapinboxes.com"
    apollo_api_url: str = "https://api.apollo.io/api/v1"
    apify_api_url: str = "https://api.apify.com/v2"
    google_places_url: str = "https://places.googleapis.com/v1/places:searchText"
    jina_reader_url: str = "https://r.jina.ai/"
    # Anthropic's API; empty means the SDK's own default.
    anthropic_base_url: str | None = None
    # The page named in the website reader's User-Agent, for site owners who want to know who's reading.
    bot_info_url: str = "https://aheadtech360.com"

    # An email to the team whenever a lead replies (services/notify.py). Port 465 is SSL; any other uses STARTTLS.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    email_from_name: str = "Coldbrew"
    # Who gets it: comma-separated addresses.
    notify_emails: str = ""
    # The app's address for links in those emails; empty means the first of AUTHORIZED_URLS.
    app_url: str | None = None

    # Prices the cost estimator uses where Coldbrew has no source of its own, as JSON by line id, e.g.
    # {"apollo-reveal": 0.02, "linkedin-posts": 0.002, "railway": 20}. The estimator shows prices read-only.
    cost_prices: str = ""

    # Background work: Celery workers and Celery beat, with Redis as the broker.
    redis_url: str = "redis://localhost:6379/0"
    # The broker Celery uses; empty means REDIS_URL.
    celery_broker_url: str | None = None
    # How often beat starts the follow-up check, in seconds.
    followup_check_seconds: int = Field(default=1800, ge=60)
    # The longest a background job (research, writing emails, fetching posts...) may run before it's stopped.
    job_time_limit_seconds: int = Field(default=3600, ge=60)
    # How long a read is answered from memory (response_cache.py). Any change made through Coldbrew clears it
    # at once; this bounds how stale changes made elsewhere (Lead Intel, the follow-up check) can look. 0: off.
    response_cache_seconds: int = Field(default=60, ge=0, le=3600)

    @property
    def auth_issuer(self) -> str:
        return f"{(self.supabase_url or '').rstrip('/')}/auth/v1"

    @property
    def jwks_url(self) -> str:
        return self.supabase_jwks_url or f"{self.auth_issuer}/.well-known/jwks.json"

    @property
    def cost_price_overrides(self) -> dict[str, float]:
        """COST_PRICES as {line id: price}; anything that isn't a non-negative number is ignored."""
        import json

        try:
            raw = json.loads(self.cost_prices or "{}")
        except ValueError:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): float(v) for k, v in raw.items() if isinstance(v, int | float) and not isinstance(v, bool) and v >= 0}

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def authorized_url_list(self) -> list[str]:
        return [o for o in (origin_of(u) for u in self.authorized_urls.split(",")) if o]


def origin_of(url: str | None) -> str | None:
    """scheme://host[:port] of a URL, lowercased, or None when it isn't one ("null", blank, a bare path)."""
    from urllib.parse import urlsplit

    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


@lru_cache
def get_settings() -> Settings:
    return Settings()
