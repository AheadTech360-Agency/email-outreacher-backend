"""Request and response models. camelCase on the wire to match the frontend."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """camelCase on the wire to match the frontend's TypeScript types; snake_case in Python."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


class Page[T](ApiModel):
    items: list[T]
    total: int
    limit: int
    offset: int


# ---------- ICPs ----------

class IcpProfileIn(ApiModel):
    """This workspace's outreach settings for a Lead Intel ICP. The ICP itself stays in Lead Intel."""

    description: str = ""
    titles: list[str] = []
    pains: str = ""
    offer: str = ""
    proof: str = ""
    cta: str = ""
    tone: str = ""
    words_to_avoid: str = ""
    hook_guidance: str = ""
    # Lead analysis: whose posts are read for the opening line.
    enabled_sources: list[str] = []
    # Verification: what research reads to decide whether a business is worth contacting.
    verification_sources: list[str] = ["website", "jina", "instagram", "score"]
    # Lead analysis limits for this ICP; null means the workspace setting.
    posts_per_source: int | None = Field(default=None, ge=1, le=20)
    max_post_age_days: int | None = Field(default=None, ge=1, le=365)

    @field_validator("verification_sources")
    @classmethod
    def _known_verification(cls, value: list[str]) -> list[str]:
        from services.sources import VERIFICATION_IDS

        unknown = sorted(set(value) - set(VERIFICATION_IDS))
        if unknown:
            raise ValueError(f"Unknown verification sources: {', '.join(unknown)}")
        return list(dict.fromkeys(value))

    @field_validator("enabled_sources")
    @classmethod
    def _known_analysis(cls, value: list[str]) -> list[str]:
        from services.sources import ANALYSIS_IDS

        # Older profiles saved "website" here; it never fetched posts, so it's dropped.
        return [v for v in dict.fromkeys(value) if v in ANALYSIS_IDS]
    # What we sell to this ICP (eo_products), or none.
    product_id: UUID | None = None
    # Null uses the workspace's default prompt of that kind.
    verify_prompt_id: UUID | None = None
    hook_prompt_id: UUID | None = None
    email_prompt_id: UUID | None = None


class IcpProfileOut(IcpProfileIn):
    updated_at: datetime


ICP_SOURCES = (
    "business_name", "website_text", "website_domain", "instagram_bio", "instagram_category", "instagram_handle",
    "meta_ads_text",
)  # fmt: skip


class IcpCreateIn(ApiModel):
    """A new ICP, with exactly Lead Intel's fields. Added to Lead Intel's icps table."""

    name: str = Field(min_length=1, max_length=100)
    industry: str = Field(min_length=1, max_length=100)
    priority: int = Field(default=100, ge=0, le=10000)
    enabled: bool = True
    sources: list[str] = Field(default=[], max_length=len(ICP_SOURCES))
    any_of: list[str] = Field(default=[], max_length=100)
    none_of: list[str] = Field(default=[], max_length=100)
    website_any_of: list[str] = Field(default=[], max_length=100)
    instagram_any_of: list[str] = Field(default=[], max_length=100)
    require_all: bool = False
    min_followers: int | None = Field(default=None, ge=0)
    max_followers: int | None = Field(default=None, ge=0)
    search_terms: list[str] = Field(default=[], max_length=50)
    requires_website: bool = False
    min_reviews: int | None = Field(default=None, ge=0)
    places: list[str] = Field(default=[], max_length=100)


class Icp(ApiModel):
    """A Lead Intel ICP, every field as Lead Intel stores it (read-only), plus this workspace's outreach
    profile for it if one exists."""

    id: UUID
    name: str
    industry: str
    priority: int
    enabled: bool
    # Which text the rule reads; empty means all of it.
    sources: list[str]
    any_of: list[str]
    none_of: list[str]
    website_any_of: list[str]
    instagram_any_of: list[str]
    require_all: bool
    min_followers: int | None
    max_followers: int | None
    # What Lead Intel searches Google Maps with.
    search_terms: list[str]
    requires_website: bool
    min_reviews: int | None
    places: list[str]
    company_count: int
    created_at: datetime
    updated_at: datetime
    profile: IcpProfileOut | None = None
    # After a create or rename: what happened to its CheapSequencer campaign.
    campaign_note: str | None = None


# ---------- Companies ----------

class Instagram(ApiModel):
    handle: str | None = None
    url: str | None = None
    followers: int | None = None
    category: str | None = None
    bio: str | None = None


class Company(ApiModel):
    """A Lead Intel business, read-only."""

    id: UUID
    name: str
    city: str | None
    state: str | None
    country: str | None
    icp_id: UUID | None
    icp_name: str | None
    tier: int | None
    problem_score: float | None
    website_status: str | None
    website_url: str | None
    domain: str | None
    instagram: Instagram
    created_at: datetime
    # When this company was pulled into the workspace in X-Workspace-Id; null if it isn't in it.
    imported_at: datetime | None = None


class CompanyDetail(Company):
    # Every stored field for the business (website text, Instagram profile, listing data...),
    # exactly as Lead Intel saved it.
    fields: dict[str, str | None]


class CompanyImportIn(ApiModel):
    """Either specific Lead Intel companies, or every contactable company in one Lead Intel ICP."""

    company_ids: list[UUID] | None = Field(default=None, max_length=5000)
    icp_id: UUID | None = None


class CompanyImportResult(ApiModel):
    imported: int
    already_in_workspace: int
    # Ids Lead Intel doesn't have, or that are excluded or merged and so can't be contacted.
    not_found: list[UUID]


class CompanyRemoveIn(ApiModel):
    company_ids: list[UUID] = Field(min_length=1, max_length=5000)


class CompanyRemoveResult(ApiModel):
    removed: int


# ---------- Leads ----------

class LeadIn(ApiModel):
    """One lead added by hand. Needs an email or a LinkedIn URL."""

    icp_id: UUID | None = None
    first_name: str = Field(default="", max_length=200)
    last_name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    company: str = Field(default="", max_length=300)
    title: str = Field(default="", max_length=300)
    website: str = Field(default="", max_length=500)
    linkedin_url: str = Field(default="", max_length=500)
    instagram_url: str = Field(default="", max_length=500)
    facebook_url: str = Field(default="", max_length=500)
    extra: dict[str, str] = {}


class LeadOut(ApiModel):
    id: UUID
    icp_id: UUID | None = Field(validation_alias="lead_intel_icp_id")
    first_name: str
    last_name: str
    email: str | None
    email_status: str | None
    title: str
    company: str
    website: str
    domain: str | None
    linkedin_url: str | None
    instagram_url: str | None
    facebook_url: str | None
    extra: dict[str, str]
    origin: str
    status: str
    # The Email Scoring prompt's score for the lead's current draft, and its reasons.
    score: int | None
    score_reasons: list[str] | None = None
    # How many of their own posts are stored (from Fetch posts or Enrich).
    post_count: int = 0
    created_at: datetime
    # When their first email was queued for sending: only then do they count as a lead (the Leads page).
    queued_at: datetime | None = None


class ColumnMapping(ApiModel):
    column: str
    # The lead field this column fills, or null when it's kept as an extra field.
    field: str | None


class RowOutcome(ApiModel):
    row: int  # spreadsheet row number: the header is row 1
    outcome: Literal["imported", "duplicate", "invalid"]
    reason: str | None = None
    lead_id: UUID | None = None


class ImportResult(ApiModel):
    dry_run: bool
    total_rows: int
    imported: int
    duplicates_in_file: int
    duplicates_existing: int
    invalid: int
    websites_from_email: int
    columns: list[ColumnMapping]
    rows: list[RowOutcome]


# ---------- Workspaces ----------


class WorkspaceIn(ApiModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class WorkspaceUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class WorkspaceOut(ApiModel):
    id: UUID
    name: str
    description: str
    created_at: datetime


class WorkspaceMembersIn(ApiModel):
    user_ids: list[UUID] = Field(default=[], max_length=500)


# ---------- Do-not-contact list ----------

ExclusionReason = Literal["current_client", "past_client", "do_not_contact", "competitor", "other"]


class ExclusionIn(ApiModel):
    key_type: Literal["domain", "email", "phone", "place_id", "name_location"]
    value: str = Field(min_length=1, max_length=300)
    reason: ExclusionReason = "do_not_contact"
    business_name: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=1000)


class ExcludeCompanyIn(ApiModel):
    reason: ExclusionReason = "do_not_contact"
    note: str = Field(default="", max_length=1000)


class ExclusionOut(ApiModel):
    id: UUID
    key_type: str
    key_value: str
    reason: str
    business_name: str
    note: str
    created_at: datetime


# ---------- Sending: mailboxes (CheapSequencer) and their domains (CheapInboxes) ----------


class MailboxOut(ApiModel):
    id: str
    email: str
    name: str
    domain: str
    provider: str
    status: str
    auth_status: str
    needs_attention: bool
    last_error: str | None
    tags: list[str]
    campaign_daily: int
    warmup_daily: int
    total_daily: int
    warmup_enabled: bool
    warmup_state: str | None
    warmup_started_at: str | None
    last_sent_at: str | None
    today_sends: int
    today_replies: int
    today_failed: int
    today_inbox: int
    today_spam: int
    today_saved: int
    today_unknown: int


class DomainHealth(ApiModel):
    domain: str
    status: str
    health: str
    # Each failing DNS check, in a sentence.
    problems: list[str]


class CampaignBrief(ApiModel):
    name: str
    status: str
    daily_send_cap: int | None


class SendingSummary(ApiModel):
    connected: int
    connected_limit: int | None
    sends_used: int
    sends_limit: int | None
    period_ends_at: str | None
    # Campaign emails all mailboxes may send per day, together.
    campaign_daily: int
    warmup_today_sends: int
    warmup_today_inbox: int
    warmup_today_spam: int
    warmup_today_saved: int
    warmup_week_sent: int
    warmup_week_inbox: int
    warmup_week_spam: int
    warmup_week_saved: int
    needing_review: int


class MailboxesOut(ApiModel):
    summary: SendingSummary
    mailboxes: list[MailboxOut]
    domains: list[DomainHealth]
    campaigns: list[CampaignBrief]
    # Why part of the page is missing (a key not set, a service down).
    errors: list[str]


# ---------- Sending through CheapSequencer ----------


class SendingSettingsIn(ApiModel):
    # Who the emails are from: the company and its website, under the sender's name.
    sender_company: str = Field(default="", max_length=200)
    sender_website: str = Field(default="", max_length=300)
    # What email 2 and 3 say until Coldbrew writes the personal one ({{first_name}} is filled in).
    followup_fallback_2: str = Field(min_length=1, max_length=2000)
    followup_fallback_3: str = Field(min_length=1, max_length=2000)
    # How many hours before a follow-up is due Claude writes it.
    followup_lead_hours: int = Field(ge=2, le=72)


class TemplateOutcomeOut(ApiModel):
    campaign: str
    status: str  # updated | unchanged | skipped
    reason: str | None = None


class SendingSettingsOut(SendingSettingsIn):
    # What saving did to the campaigns' text.
    templates: list[TemplateOutcomeOut]


class SendingCampaignOut(ApiModel):
    id: str
    name: str
    status: str
    daily_send_cap: int | None
    queued: int


class QueueIn(ApiModel):
    email_ids: list[UUID] = Field(min_length=1, max_length=500)
    # Emails to risky addresses (catch-all domains) a person confirmed. Any other risky one is left out.
    risky_ok: list[UUID] = Field(default=[], max_length=500)


class QueueOutcomeOut(ApiModel):
    email_id: UUID
    status: str  # queued | skipped
    reason: str | None = None
    campaign: str | None = None


class QueueOut(ApiModel):
    queued: int
    results: list[QueueOutcomeOut]


class OutboxStepOut(ApiModel):
    step: int
    subject: str  # empty on 2 and 3: they reply in the same thread
    body: str
    # waiting: not sent yet; sent; skipped: the sequence ended before it.
    status: str
    # False while email 2 or 3 is still the generic text from Settings.
    personalized: bool
    sent_at: datetime | None = None


class OutboxRowOut(ApiModel):
    email_id: UUID
    lead_id: UUID
    name: str
    email: str
    title: str
    company: str
    campaign_id: str
    campaign: str
    # The mailbox all three emails go out from, once CheapSequencer has assigned one.
    mailbox: str | None = None
    # queued, sending, replied, bounced, unsubscribed, failed, paused, stopped or finished.
    state: str
    sent: int
    queued_at: datetime | None = None
    last_sent_at: datetime | None = None
    synced_at: datetime | None = None
    steps: list[OutboxStepOut]


class ReplyThreadOut(ApiModel):
    id: str
    subject: str
    preview: str
    mailbox: str
    lead_email: str
    lead_name: str
    lead_id: UUID | None = None
    campaign: str
    last_message_at: datetime | None = None
    message_count: int
    inbound_count: int
    unread_count: int
    classification: str | None = None


class ReplyMessageOut(ApiModel):
    id: str
    direction: str  # inbound | outbound
    from_name: str
    from_address: str
    to: list[str]
    sent_at: datetime | None = None
    subject: str
    body: str


class ReplyThreadDetailOut(ReplyThreadOut):
    messages: list[ReplyMessageOut]


class CampaignSyncOut(ApiModel):
    icp: str
    status: str  # created | renamed | linked | unchanged | skipped
    note: str


# ---------- Cost estimator ----------

# How an item's quantity counts: per lead contacted, per month, or per email sent.
CostBasis = Literal["lead", "month", "email", "company"]


class CostItem(ApiModel):
    """One thing that costs money: an operation (an email written, a post fetched, a company verified...)
    or a subscription. A month costs price x quantity, times leads or emails sent for those bases."""

    id: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=80)
    category: str = Field(default="Other", max_length=40)
    # What one use is, like "per post" or "per email written".
    unit_label: str = Field(default="per month", max_length=40)
    price_usd: float = Field(ge=0, le=1_000_000)
    quantity: float = Field(default=1, ge=0, le=10_000_000)
    basis: CostBasis = "month"
    # Claude operations: the price is worked out from tokens per use and the model's price per token.
    model: str | None = Field(default=None, max_length=60)
    tokens_in: int | None = Field(default=None, ge=0, le=10_000_000)
    tokens_out: int | None = Field(default=None, ge=0, le=10_000_000)
    # The Usage log action it's measured against, like "Write emails".
    action: str | None = Field(default=None, max_length=60)
    note: str = Field(default="", max_length=300)
    enabled: bool = True
    # A source Coldbrew can't run yet (services/sources.py): shown for planning, never run.
    planned: bool = False
    # Costs nothing as Coldbrew runs it (set on the server), unlike a price of 0 that nobody has set yet.
    free: bool = False


class CostEstimate(ApiModel):
    leads_per_month: int = Field(ge=0, le=1_000_000)
    # Emails actually sent per lead, on average: 3 when nobody replies, fewer as replies stop sequences.
    emails_per_lead: float = Field(ge=0, le=10)
    # Replies per 100 leads, for the cost per reply. 0 until you set it.
    reply_rate_pct: float = Field(ge=0, le=100)
    # Companies verified a month: lines counted per company use this.
    companies_per_month: int = Field(default=0, ge=0, le=1_000_000)
    items: list[CostItem] = Field(max_length=80)


class ClaudePrice(ApiModel):
    model: str
    input_per_mtok: float  # USD per million input tokens
    output_per_mtok: float


class MeasuredUse(ApiModel):
    """A Claude operation's average from the Usage log: per lead for emails, per call otherwise."""

    action: str
    uses: int
    tokens_in: int
    tokens_out: int
    cost_usd: float


class SourceOut(ApiModel):
    """A source Coldbrew can use (services/sources.py) and its price for the Apify plan tier."""

    id: str
    kind: str  # verification | analysis
    level: str  # company | lead
    label: str
    hint: str
    provider: str  # coldbrew | jina | claude | apify
    unit: str
    actor: str | None
    status: str  # live | planned
    unit_price: float | None  # per unit, from Apify for the plan tier; None when free or unknown
    run_price: float | None  # per run, if the actor charges one


class SourcesOut(ApiModel):
    tier: str | None  # the Apify plan tier the prices are for; None without APIFY_TOKEN
    sources: list[SourceOut]
    note: str | None = None


class CostReference(ApiModel):
    claude_prices: list[ClaudePrice]
    # The model company verification uses (CLAUDE_SCORING_MODEL on the server).
    scoring_model: str
    measured: list[MeasuredUse]
    since: datetime


class FollowUpRunOut(ApiModel):
    checked: int
    written: int
    ended: int
    cost_usd: float
    notes: list[str]


# ---------- Users ----------

UserRole = Literal["sales", "management", "admin"]


class UserIn(ApiModel):
    name: str = Field(default="", max_length=100)
    email: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$", max_length=254)
    role: UserRole = "sales"
    workspace_ids: list[UUID] = Field(default=[], max_length=100)
    # Their first password, which the admin passes on. Unused when the email already has a sign-in
    # (shared with Lead Intel): that one keeps its own password.
    password: str = Field(min_length=12, max_length=72)


class UserUpdate(ApiModel):
    name: str | None = Field(default=None, max_length=100)
    role: UserRole | None = None
    status: Literal["active", "deactivated"] | None = None
    workspace_ids: list[UUID] = Field(default=[], max_length=100)


class UserOut(ApiModel):
    id: UUID
    name: str
    email: str
    role: str
    # invited: added here, hasn't signed in yet.
    status: str
    workspace_ids: list[UUID]
    last_active_at: datetime | None
    created_at: datetime


class UserAdded(UserOut):
    # created: a new sign-in with the password given. existing: the email already had one (shared with
    # Lead Intel), which they keep using, password and all.
    sign_in: Literal["created", "existing"]


# ---------- Stakeholders (Apollo) ----------


class StakeholderOut(ApiModel):
    id: UUID
    company_id: UUID | None = Field(validation_alias="lead_intel_business_id")
    # The pipeline company (eo_companies) they were found at.
    pipeline_company_id: UUID | None = Field(default=None, validation_alias="company_id")
    icp_id: UUID | None = Field(validation_alias="lead_intel_icp_id")
    organization_name: str | None
    source: str
    first_name: str
    # Masked ("Hu***n") until the person is revealed.
    last_name: str
    title: str
    seniority: str | None
    linkedin_url: str | None
    has_email: bool | None
    email: str | None
    email_status: str | None
    revealed_at: datetime | None
    lead_id: UUID | None
    found_at: datetime
    # When their first email was queued: a person counts as a lead only from then.
    queued_at: datetime | None = None


class StakeholderDetailOut(StakeholderOut):
    """A person for their page: everything stored about them, grouped ({group: {label: value}})."""

    data: dict[str, dict[str, str]] = {}


class StakeholderSearchIn(ApiModel):
    company_ids: list[UUID] = Field(min_length=1, max_length=25)
    titles: list[str] = Field(default=[], max_length=30)
    seniorities: list[str] = Field(default=[], max_length=11)


class CompanySearchResult(ApiModel):
    company_id: UUID
    name: str | None
    status: Literal["searched", "skipped", "failed"]
    found: int = 0
    new: int = 0
    # How many people Apollo has in total for this search; we save the first page.
    total_at_apollo: int | None = None
    reason: str | None = None


class StakeholderSearchResult(ApiModel):
    results: list[CompanySearchResult]
    # Set when the provider refused (bad key, plan, rate limit, credit); the rest weren't searched.
    error: str | None = None
    # What the provider charged, when it says (Apify does; Apollo searches are free).
    cost_usd: float | None = None


class LinkedInSearchIn(ApiModel):
    """Find people at companies on LinkedIn (Apify). Paid per profile: $0.003, or $0.012 with their work email."""

    company_ids: list[UUID] = Field(min_length=1, max_length=10)
    titles: list[str] = Field(default=[], max_length=30)
    max_per_company: int = Field(default=10, ge=1, le=50)
    # Search for each person's work email too (and get their full profile).
    find_emails: bool = True


class AddAsLeadsIn(ApiModel):
    stakeholder_ids: list[UUID] = Field(min_length=1, max_length=100)


class AddAsLeadsResult(ApiModel):
    added: int
    # Already a lead (same email or LinkedIn), now linked to it.
    linked_existing: int
    skipped: int


class IcpSearchIn(ApiModel):
    """Search Apollo for people who fit a Lead Intel ICP, anywhere, not just at companies already imported."""

    icp_id: UUID
    titles: list[str] = Field(default=[], max_length=30)
    seniorities: list[str] = Field(default=[], max_length=11)
    # Company headquarters: cities, states, countries or postcodes.
    locations: list[str] = Field(default=[], max_length=30)
    keywords: str = Field(default="", max_length=200)
    # "min,max" employee counts, e.g. "1,10".
    employee_ranges: list[str] = Field(default=[], max_length=10)
    verified_email_only: bool = True
    per_page: int = Field(default=25, ge=1, le=100)
    page: int = Field(default=1, ge=1, le=500)


class IcpSearchResult(ApiModel):
    found: int
    new: int
    total_at_apollo: int | None
    page: int
    error: str | None = None


class StakeholderRevealIn(ApiModel):
    stakeholder_ids: list[UUID] = Field(min_length=1, max_length=25)


class RevealOutcome(ApiModel):
    stakeholder_id: UUID
    status: Literal["revealed", "no_match", "already_revealed", "not_found", "failed"]
    email: str | None = None
    lead_id: UUID | None = None
    lead_created: bool = False
    reason: str | None = None


class StakeholderRevealResult(ApiModel):
    outcomes: list[RevealOutcome]
    revealed: int
    error: str | None = None


# ---------- Posts ----------


class PostFetchIn(ApiModel):
    lead_ids: list[UUID] = Field(min_length=1, max_length=25)
    # Posts fetched per source (company LinkedIn, their LinkedIn, company Instagram) when a source is read.
    max_posts: int = Field(default=3, ge=1, le=20)
    max_age_days: int = Field(default=60, ge=1, le=365)
    # Which networks to fetch; both unless an ICP turned one off.
    sources: list[Literal["linkedin", "instagram"]] = Field(default=["linkedin", "instagram"], min_length=1)


class LeadPostsResult(ApiModel):
    lead_id: UUID
    # Posts found per source; null when there's no URL for it or it wasn't needed.
    company_linkedin: int | None = None
    linkedin: int | None
    instagram: int | None  # the company's Instagram
    new: int
    # Whether Claude found enough to open an email with, at which source it stopped, and why.
    enough: bool = False
    stopped_at: str | None = None
    reason: str = ""


class PostGatherIn(ApiModel):
    """Read posts for people and/or companies: any of the three sources, up to `max_posts` from each, stopping for
    each one as soon as Claude finds enough to write with."""

    stakeholder_ids: list[UUID] = Field(default=[], max_length=25)
    company_ids: list[UUID] = Field(default=[], max_length=25)
    sources: list[Literal["company_linkedin", "person_linkedin", "company_instagram"]] = Field(min_length=1)
    max_posts: int = Field(default=3, ge=1, le=20)
    max_age_days: int = Field(default=60, ge=1, le=365)


class GatheredFor(ApiModel):
    kind: Literal["person", "company"]
    id: UUID
    company_linkedin: int | None = None
    linkedin: int | None = None
    instagram: int | None = None
    new: int = 0
    enough: bool = False
    stopped_at: str | None = None
    enough_in: list[str] = []  # every source that gave enough
    reason: str = ""


class PostGatherResult(ApiModel):
    results: list[GatheredFor]
    cost_usd: float = 0.0  # Apify
    claude_cost_usd: float = 0.0
    error: str | None = None


class PostFetchResult(ApiModel):
    results: list[LeadPostsResult]
    cost_usd: float | None  # Apify
    error: str | None = None
    claude_cost_usd: float = 0.0  # deciding, after each source, whether there's enough


class PostOut(ApiModel):
    id: UUID
    lead_id: UUID
    source: str
    url: str
    posted_at: datetime | None
    text: str
    media_type: str
    is_repost: bool
    fetched_at: datetime


# ---------- Company pipeline ----------


class ApolloFindIn(ApiModel):
    # Default to the ICP's search terms and places when empty.
    keywords: list[str] = Field(default=[], max_length=30)
    locations: list[str] = Field(default=[], max_length=30)
    employee_ranges: list[str] = Field(default=[], max_length=10)
    page: int = Field(default=1, ge=1, le=500)
    per_page: int = Field(default=25, ge=1, le=100)


class GoogleMapsFindIn(ApiModel):
    # Default to the ICP's search terms and places when empty.
    search_terms: list[str] = Field(default=[], max_length=10)
    locations: list[str] = Field(default=[], max_length=10)
    radius_miles: float = Field(default=10, ge=1, le=50)
    # Google charges per request ($0.035 each), so the run stops here, whatever is left to search.
    max_searches: int = Field(default=20, ge=1, le=500)


class FindCompaniesIn(ApiModel):
    icp_id: UUID
    sources: list[Literal["lead_intel", "apollo", "google_maps"]] = Field(default=["lead_intel", "apollo"], min_length=1)
    # Only these of the ICP's Lead Intel companies; all of them when not given.
    lead_intel_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=2000)
    apollo: ApolloFindIn = ApolloFindIn()
    google_maps: GoogleMapsFindIn = GoogleMapsFindIn()


class SourceFindResult(ApiModel):
    found: int = 0
    added: int = 0
    # Already in the pipeline (same company id or the same website domain).
    duplicates: int = 0
    # Apollo companies that turned out to be in Lead Intel too.
    upgraded: int = 0
    total_available: int | None = None
    error: str | None = None
    # On the do-not-contact list, so not added.
    excluded: int = 0
    # Google Maps: requests made, what they cost, and anything worth knowing about the run.
    searches: int | None = None
    cost_usd: float | None = None
    notes: list[str] = []


class FindCompaniesResult(ApiModel):
    lead_intel: SourceFindResult | None
    apollo: SourceFindResult | None
    google_maps: SourceFindResult | None = None


class PipelineCompanyOut(ApiModel):
    id: UUID
    icp_id: UUID | None
    source: str
    lead_intel_business_id: UUID | None
    apollo_org_id: str | None
    google_place_id: str | None
    name: str
    domain: str | None
    website_url: str | None
    linkedin_url: str | None
    facebook_url: str | None
    twitter_url: str | None
    instagram_url: str | None
    city: str | None
    state: str | None
    phone: str | None
    status: str
    status_reason: str | None
    overridden: bool
    rule_fit: bool | None
    rule_detail: dict[str, Any] | None
    score: int | None
    score_detail: dict[str, Any] | None
    research: dict[str, Any] | None
    researched_at: datetime | None
    created_at: datetime


class PipelineCompanyDetailOut(PipelineCompanyOut):
    """A company for its page: everything stored about it, grouped ({group: {label: value}})."""

    data: dict[str, dict[str, str]] = {}


class ResearchIn(ApiModel):
    company_ids: list[UUID] = Field(min_length=1, max_length=10)
    # Research again even if done before; the new verdict replaces one set by hand.
    force: bool = False


class ResearchResult(ApiModel):
    researched: int
    skipped: int
    qualified: int
    not_fit: int
    errors: int
    cost_usd: float
    notes: list[str]
    to_approve: int = 0  # waiting for a person to approve or reject


class CompanyStatusIn(ApiModel):
    status: Literal["qualified", "not_fit"]


class RereadResult(ApiModel):
    """Reading a company's website again: what it found (no AI used)."""

    reached: bool
    via: str | None  # own_crawl | jina
    pages: int
    socials: dict[str, str]
    emails: list[str]
    phone: str | None
    detail: str | None
    company: "PipelineCompanyOut"


class CompanyDecisionIn(ApiModel):
    """Approve (qualified) or reject (not_fit) companies, from the To approve queue."""

    company_ids: list[UUID] = Field(min_length=1, max_length=2000)
    status: Literal["qualified", "not_fit"]


class CompanyDecisionResult(ApiModel):
    changed: int


class PipelineStakeholdersIn(ApiModel):
    company_ids: list[UUID] = Field(min_length=1, max_length=25)
    source: Literal["apollo", "linkedin"] = "apollo"
    titles: list[str] = Field(default=[], max_length=30)
    seniorities: list[str] = Field(default=[], max_length=11)
    max_per_company: int = Field(default=10, ge=1, le=50)
    # LinkedIn only: search for each person's work email too ($0.012 a person instead of $0.003).
    find_emails: bool = True


# ---------- Usage ----------


class UsageTotals(ApiModel):
    calls: int
    failed: int
    credits: float
    cost_usd: float
    input_tokens: int
    output_tokens: int


class ProviderUsage(UsageTotals):
    provider: str


class ActionUsage(UsageTotals):
    provider: str
    action: str


class ProviderCallOut(ApiModel):
    idempotency_key: str = Field(serialization_alias="id")
    provider: str
    endpoint: str | None
    action: str | None
    status: str
    credits: float | None
    cost_usd: float
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    called_at: datetime


class UsageOut(ApiModel):
    since: datetime
    providers: list[ProviderUsage]
    actions: list[ActionUsage]
    recent: list[ProviderCallOut]


# ---------- Prompts ----------

PromptKind = Literal["verify", "hook", "email", "score", "followup2", "followup3", "sort", "sort_people"]


class PromptVersionIn(ApiModel):
    system_prompt: str = Field(default="", max_length=20000)
    user_prompt: str = Field(min_length=1, max_length=40000)
    model: str = Field(min_length=1, max_length=100)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=600, gt=0, le=64000)
    # Hook and email prompts: how many of the newest posts {{items}} holds. None means 15.
    max_posts: int | None = Field(default=None, ge=1, le=50)


class PromptVersionOut(PromptVersionIn):
    id: UUID
    prompt_id: UUID = Field(serialization_alias="templateId")
    version: int
    note: str
    created_at: datetime


class PromptOut(ApiModel):
    id: UUID
    name: str
    kind: PromptKind
    is_default: bool
    current_version_id: UUID | None
    created_at: datetime
    updated_at: datetime


class PromptsOut(ApiModel):
    prompts: list[PromptOut]
    versions: list[PromptVersionOut]


class PromptCreateIn(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    kind: PromptKind
    version: PromptVersionIn
    note: str = Field(default="", max_length=500)
    is_default: bool = False


class NewVersionIn(PromptVersionIn):
    note: str = Field(default="", max_length=500)


class PromptUpdateIn(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_default: Literal[True] | None = None


class PromptWithVersion(ApiModel):
    prompt: PromptOut
    version: PromptVersionOut


# ---------- What research read for a company ----------


class CompanyPageOut(ApiModel):
    url: str
    role: str
    via: str
    status: int | None
    text: str
    fetched_at: datetime


class CompanyPostOut(ApiModel):
    id: UUID
    source: str
    url: str
    posted_at: datetime | None
    text: str
    media_type: str


class CompanyContentOut(ApiModel):
    pages: list[CompanyPageOut]
    posts: list[CompanyPostOut]


# ---------- Emails: hook selection and email generation ----------


class GenerateIn(ApiModel):
    lead_ids: list[UUID] = Field(min_length=1, max_length=25)
    # An extra instruction for the email step, e.g. "shorter, mention their new store".
    instruction: str = Field(default="", max_length=1000)


class GenerateOutcome(ApiModel):
    lead_id: UUID
    name: str | None
    status: Literal["drafted", "failed", "skipped"]
    reason: str | None = None
    email_id: UUID | None = None


class GenerateResult(ApiModel):
    results: list[GenerateOutcome]
    drafted: int
    cost_usd: float


class EmailPointOut(ApiModel):
    """One of the ranked hooks, best first (rank 1 is the one the email opens with when usable)."""

    rank: int
    point: str
    hook: str
    source: str
    why: str
    relevance: int


class EmailPostOut(ApiModel):
    source: str
    url: str
    posted_at: datetime | None
    text: str
    whose: Literal["person", "company"]


class EmailLeadOut(ApiModel):
    id: UUID
    icp_id: UUID | None = Field(validation_alias="lead_intel_icp_id")
    first_name: str
    last_name: str
    title: str
    company: str
    email: str | None
    # valid | risky | ... from the LinkedIn email search (or Apollo's status); risky addresses need their own approval.
    email_status: str | None = None
    linkedin_url: str | None


class FeedbackOut(ApiModel):
    rating: Literal["good", "bad"]
    tags: list[str]
    note: str
    updated_at: datetime


class EmailOut(ApiModel):
    id: UUID
    lead: EmailLeadOut
    status: str
    subject: str
    body: str
    original_body: str
    edited: bool
    hook: str
    relevance: int
    confidence: int
    flags: list[str]
    chosen_post: EmailPostOut | None
    points: list[EmailPointOut]
    cost_usd: float
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None
    feedback: FeedbackOut | None = None
    # How many rated emails Claude was shown as examples when it wrote this one.
    examples_used: int = 0
    # The Email Scoring prompt's verdict on this draft (None when it wasn't scored).
    score: int | None = None
    score_reasons: list[str] = []


class EmailUpdateIn(ApiModel):
    subject: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = Field(default=None, min_length=1, max_length=10000)
    # Never "sent": nothing here sends email.
    status: Literal["draft", "approved", "skipped"] | None = None


# ---------- Feedback on generated emails ----------

FeedbackTag = Literal[
    "strong_hook", "right_tone", "clear_cta", "send_as_is",
    "too_long", "too_generic", "wrong_hook", "tone_off", "made_up_facts", "weak_cta", "wrong_person",
]  # fmt: skip


class FeedbackIn(ApiModel):
    rating: Literal["good", "bad"]
    tags: list[FeedbackTag] = Field(default=[], max_length=11)
    note: str = Field(default="", max_length=2000)


class FeedbackStat(ApiModel):
    icp_id: UUID | None
    good: int
    bad: int


class SuggestIn(ApiModel):
    icp_id: UUID


class SuggestOut(ApiModel):
    prompt_id: UUID
    prompt_name: str
    based_on_version: int
    # Saving the suggestion as a new version changes the prompt for every ICP using it: those that
    # chose it, and, when it's the default, every ICP left on "Use default".
    chosen_by_icps: int
    is_default: bool
    examples: int
    summary: str
    changes: list[str]
    system_prompt: str
    user_prompt: str
    cost_usd: float


# ---------- Settings: learning from feedback ----------


class FeedbackSettings(ApiModel):
    """How many examples Claude sees per new email: from the example library, and rated good and bad;
    and how many rated emails it reads per prompt suggestion."""

    sample_examples: int = Field(ge=0, le=10)
    good_examples: int = Field(ge=0, le=10)
    bad_examples: int = Field(ge=0, le=10)
    improve_examples: int = Field(ge=3, le=50)


class ScoringSettings(ApiModel):
    # The email scoring prompt; null uses the workspace's default scoring prompt.
    prompt_id: UUID | None = None
    # Emails scoring at or above this may go out on their own once auto-send is on.
    threshold: int = Field(ge=0, le=100)
    auto_send: bool
    daily_auto_send_cap: int = Field(ge=0, le=10000)


class CompanyApprovalSettings(ApiModel):
    """Company verification, like email scoring: with auto-approve on, companies scoring at least the threshold
    are approved; the rest wait for you to approve or reject them."""

    threshold: int = Field(ge=0, le=100)
    auto_approve: bool


class GeneralSettings(ApiModel):
    """The Settings page's defaults and review settings, kept for the whole workspace. The budget and scoring are
    the admins' (the Roles tab's "Administration"); the rest is Management's."""

    default_model: str = Field(pattern=r"^claude-[a-z0-9.-]+$", max_length=60)
    posts_per_lead: int = Field(ge=1, le=20)
    max_post_age_days: int = Field(ge=1, le=365)
    monthly_budget_usd: float = Field(ge=0, le=1_000_000)
    scoring: ScoringSettings
    company_approval: CompanyApprovalSettings | None = None  # left out by an older page: kept as saved


class ResearchSettings(ApiModel):
    """How much of a company's website research reads while Claude chooses each next page."""

    research_max_pages: int = Field(ge=1, le=15)
    research_max_seconds: int = Field(ge=30, le=600)


# ---------- People added by hand or from a CSV ----------


class PersonIn(ApiModel):
    """One person typed in. Same fields as the CSV template (Apollo's export columns)."""

    icp_id: UUID | None = None
    first_name: str = Field(default="", max_length=100)
    last_name: str = Field(default="", max_length=100)
    title: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    email_status: str = Field(default="", max_length=40)
    seniority: str = Field(default="", max_length=40)
    linkedin_url: str = Field(default="", max_length=500)
    website: str = Field(default="", max_length=500)
    city: str = Field(default="", max_length=100)
    state: str = Field(default="", max_length=100)
    country: str = Field(default="", max_length=100)
    phone: str = Field(default="", max_length=60)
    apollo_contact_id: str = Field(default="", max_length=100)


class PersonAdded(ApiModel):
    person: StakeholderOut
    new: bool
    lead_created: bool
    company_name: str | None


class PeopleRowError(ApiModel):
    row: int
    reason: str


class PeopleImportResult(ApiModel):
    dry_run: bool
    total_rows: int
    valid: int
    added: int
    updated: int
    leads_created: int
    linked_to_companies: int
    duplicates_in_file: int
    errors: list[PeopleRowError]
    columns: dict[str, str]


# ---------- Products ----------


class ProductIn(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    value: str = Field(default="", max_length=2000)
    proof: str = Field(default="", max_length=4000)
    offer: str = Field(default="", max_length=1000)
    cta: str = Field(default="", max_length=500)
    url: str = Field(default="", max_length=500)


class ProductOut(ProductIn):
    id: UUID
    created_at: datetime
    updated_at: datetime
    # The ICPs that chose this product (Lead Intel ICP ids).
    icp_ids: list[UUID] = []


class OutputSchemaOut(ApiModel):
    """One JSON shape a prompt kind answers in, and when it's used."""

    name: str
    note: str
    json_schema: dict[str, Any]


# ---------- Example emails ----------


class ExampleEmailIn(ApiModel):
    subject: str = Field(default="", max_length=300)
    body: str = Field(min_length=1, max_length=10000)
    note: str = Field(default="", max_length=2000)
    all_icps: bool = False
    icp_ids: list[UUID] = Field(default=[], max_length=100)


class ExampleEmailOut(ExampleEmailIn):
    id: UUID
    source: str
    source_email_id: UUID | None
    created_at: datetime
    updated_at: datetime


class SaveAsExampleIn(ApiModel):
    note: str = Field(default="", max_length=2000)


class LeadIdsIn(ApiModel):
    lead_ids: list[UUID] = Field(min_length=1, max_length=2000)


class LeadMoveIn(LeadIdsIn):
    icp_id: UUID


class LeadsChanged(ApiModel):
    changed: int


# ---------- Background jobs (background.py) ----------


class JobIn(ApiModel):
    # A long-running endpoint under /api/v1, like "/pipeline/research" (background.LONG_RUNNING).
    path: str = Field(max_length=100)
    # Its request body, exactly as the endpoint takes it.
    body: Any = None


JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]


class JobOut(ApiModel):
    id: UUID
    kind: str
    label: str
    status: JobStatus
    # What's happening now, in words: "Waiting for a worker", say.
    step: str | None = None
    # How far along the step is, for a progress bar: `done` of `total` (total null when unknown).
    done: int = 0
    total: int | None = None
    # Why it failed.
    detail: str | None = None
    # The endpoint's answer once finished: its status code and its body, as a request would have had them.
    status_code: int | None = None
    result: Any = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


# ---------- Quick Start ----------


class QuickstartIn(ApiModel):
    """An ICP, start to drafts in review: what to search, how much of each step, and the most it may spend."""

    icp_id: UUID
    search_terms: list[str] = Field(min_length=1, max_length=10)
    places: list[str] = Field(min_length=1, max_length=10)
    radius_miles: float = Field(default=10, ge=1, le=50)
    # Google charges per request; each returns up to 20 businesses (plus one lookup per place).
    google_requests: int = Field(default=20, ge=2, le=100)
    companies: int = Field(default=10, ge=1, le=100)  # how many of those to verify
    people_per_company: int = Field(default=2, ge=1, le=10)
    titles: list[str] = Field(default=[], max_length=30)
    find_emails: bool = True
    leads: int = Field(default=20, ge=1, le=300)  # the most people made leads
    only_with_email: bool = True
    posts_per_lead: int = Field(default=5, ge=0, le=20)  # 0: write without their posts
    max_post_age_days: int = Field(default=60, ge=1, le=365)
    spend_limit_usd: float = Field(gt=0, le=500)


class QuickstartStep(ApiModel):
    state: Literal["waiting", "running", "done", "skipped", "failed"] = "waiting"
    done: int = 0
    total: int | None = None
    note: str = ""


class QuickstartRunOut(ApiModel):
    id: UUID
    icp_id: UUID
    icp_name: str
    status: str
    step: str
    settings: dict[str, Any]
    steps: dict[str, QuickstartStep]
    spent_usd: float
    spend_limit_usd: float
    detail: str | None
    created_at: datetime
    finished_at: datetime | None


class QuickstartCompanyOut(ApiModel):
    id: UUID
    name: str
    domain: str | None
    status: str  # found | qualified | not_fit | error
    reason: str | None
    score: int | None


class QuickstartLeadOut(ApiModel):
    id: UUID
    name: str
    title: str
    company: str
    email: str | None
    email_status: str | None
    posts: int
    status: str


class QuickstartDraftOut(ApiModel):
    id: UUID
    lead_id: UUID
    lead_name: str
    company: str
    email: str | None
    email_status: str | None
    subject: str
    body: str
    status: str
    score: int | None
    flags: list[str]


class QuickstartDetailOut(QuickstartRunOut):
    companies: list[QuickstartCompanyOut]
    leads: list[QuickstartLeadOut]
    drafts: list[QuickstartDraftOut]


# ---------- Smart lists ----------


class SmartListIcpDraft(ApiModel):
    """Claude's base ICP for a company list. Fields may be empty."""

    name: str = Field(default="", max_length=100)
    industry: str = Field(default="", max_length=100)
    description: str = ""
    search_terms: list[str] = Field(default=[], max_length=50)
    any_of: list[str] = Field(default=[], max_length=100)
    titles: list[str] = Field(default=[], max_length=30)
    places: list[str] = Field(default=[], max_length=100)


class SmartListOut(ApiModel):
    id: UUID
    kind: str
    name: str
    count: int
    icp_draft: SmartListIcpDraft | None
    lead_intel_icp_id: UUID | None
    created_at: datetime


class SmartListCompanyOut(ApiModel):
    id: UUID
    name: str
    domain: str | None
    city: str | None
    state: str | None
    source: str
    status: str
    reason: str


class SmartListPersonOut(ApiModel):
    id: UUID
    name: str
    title: str
    company: str | None
    email: str | None
    email_status: str | None
    linkedin_url: str | None
    reason: str


class SmartListDetailOut(SmartListOut):
    companies: list[SmartListCompanyOut]
    people: list[SmartListPersonOut]
    # This page of members: `matching` of them fit the search (all of them without one).
    page: int = 1
    page_size: int = 50
    matching: int = 0


class MoveMembersIn(ApiModel):
    """Move companies or people to an existing list, or to a new one by name."""

    kind: Literal["company", "person"]
    member_ids: list[UUID] = Field(min_length=1, max_length=5000)
    to_list_id: UUID | None = None
    new_list_name: str | None = Field(default=None, min_length=1, max_length=60)


class MoveMembersResult(ApiModel):
    moved: int
    to_list_id: UUID
    to_list_name: str


class SmartListsSummary(ApiModel):
    lists: list[SmartListOut]
    unsorted: int


class SortListsIn(ApiModel):
    kind: Literal["company", "person"]
    limit: int = Field(default=2000, ge=1, le=5000)


class SortListsResult(ApiModel):
    sorted: int
    by_claude: int
    duplicates: int  # joined a twin's list without asking Claude
    new_lists: list[str]
    sites_read: int
    matched_icp: int = 0  # companies put under an existing ICP
    drafted: list[str] = []  # lists given a draft ICP, because no ICP fits their companies
    left: int
    cost_usd: float
    errors: list[str]


class SmartListUpdateIn(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    lead_intel_icp_id: UUID | None = None


class EnrichOptions(ApiModel):
    website: bool = False
    instagram: bool = False
    linkedin: bool = False
    people_linkedin: bool = False


class EnrichCompaniesIn(ApiModel):
    company_ids: list[UUID] = Field(min_length=1, max_length=2000)
    website: bool = False
    instagram: bool = False
    linkedin: bool = False


class EnrichPeopleIn(ApiModel):
    stakeholder_ids: list[UUID] = Field(min_length=1, max_length=2000)


class EnrichResult(ApiModel):
    sites_read: int = 0
    sites_failed: int = 0
    instagram: int = 0
    linkedin_companies: int = 0
    linkedin_people: int = 0
    posts_saved: int = 0
    cost_usd: float = 0.0
    errors: list[str] = []


class ImportRunIn(ApiModel):
    """A people CSV imported as a background job: the file's text, the ICP to give them (or none), and whether
    to sort what it brings in into smart lists and ICPs with Claude afterwards."""

    csv: str = Field(min_length=1, max_length=5_000_000)
    icp_id: UUID | None = None
    sort: bool = True
    # Filling in what it brings in (services/enrich.py), each opted into: companies' websites (free), Instagram
    # profiles and LinkedIn pages, and people's LinkedIn profiles (paid).
    enrich: EnrichOptions | None = None


class ImportRunResult(ApiModel):
    total_rows: int
    valid: int
    added: int
    updated: int
    leads_created: int
    companies_added: int
    duplicates_in_file: int
    errors: list[PeopleRowError]
    companies_sorted: SortListsResult | None = None
    people_sorted: SortListsResult | None = None
    enriched: EnrichResult | None = None
    cost_usd: float = 0.0
