"""Tables owned by this service. All of them live in the `outreach` schema and start with `eo_`.

Lead Intel ids (ICPs, businesses) are stored as plain uuids with no foreign key: a foreign key
would make Lead Intel's own deletes and merges fail, which is a change to its tables in effect.
Foreign keys between our own `eo_` tables are fine and used throughout.

Everything a workspace owns carries `workspace_id`; nothing is shared between workspaces.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


OUTREACH_SCHEMA = "outreach"
TABLE_PREFIX = "eo_"

# Constraint and index names derive from the table name, so they carry the eo_ prefix too.
NAMING = {
    "pk": "%(table_name)s_pkey",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "ix": "%(table_name)s_%(column_0_N_name)s_idx",
    "ck": "%(table_name)s_%(constraint_name)s_check",
}

LEAD_STATUSES = ("imported", "enriching", "enriched", "no_content", "generated", "approved", "sent", "skipped", "failed")
SOURCES = ("linkedin", "instagram", "facebook", "website")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} in ({', '.join(repr(v) for v in values)})"


def _uuid_pk() -> Mapped[UUID]:
    return mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _updated() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


def _workspace() -> Mapped[UUID]:
    return mapped_column(ForeignKey("eo_workspaces.id", ondelete="CASCADE"), index=True)


def _user_ref() -> Mapped[UUID | None]:
    return mapped_column(ForeignKey("eo_users.id", ondelete="SET NULL"), default=None)


FALLBACK_2 = "Hi {{first_name}}, just bringing this back to the top of your inbox in case it got buried. Worth a look?"
FALLBACK_3 = (
    "Hi {{first_name}}, I'll leave it here so I'm not cluttering your inbox. If it's ever useful, just reply and I'll send the details."
)
FALLBACK_4 = "Hi {{first_name}}, one more thought on this in case the timing is better now. Happy to share how it would work for you."
FALLBACK_5 = (
    "Hi {{first_name}}, I'll leave it here so I'm not cluttering your inbox. If it's ever useful, just reply and I'll send the details."
)


class Base(DeclarativeBase):
    metadata = MetaData(schema=OUTREACH_SCHEMA, naming_convention=NAMING)


# ---------- People and workspaces ----------


class Workspace(Base):
    __tablename__ = "eo_workspaces"

    id: Mapped[UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, unique=True)
    description: Mapped[str] = mapped_column(Text, server_default="")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class User(Base):
    """A person who signs in. Admins open every workspace; everyone else only their memberships."""

    __tablename__ = "eo_users"
    __table_args__ = (
        CheckConstraint(_in("role", ("sales", "management", "admin")), name="role"),
        CheckConstraint(_in("status", ("active", "invited", "deactivated")), name="status"),
    )

    id: Mapped[UUID] = _uuid_pk()
    # Supabase auth user, once they've signed in. No foreign key: auth.users isn't ours to constrain.
    auth_user_id: Mapped[UUID | None] = mapped_column(unique=True, default=None)
    name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, unique=True)  # stored lower-cased
    role: Mapped[str] = mapped_column(Text, server_default="sales")
    status: Mapped[str] = mapped_column(Text, server_default="invited")
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class WorkspaceMember(Base):
    """Which workspaces a Sales or Management user can open."""

    __tablename__ = "eo_workspace_members"

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("eo_workspaces.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("eo_users.id", ondelete="CASCADE"), primary_key=True, index=True)
    created_at: Mapped[datetime] = _created()


EXCLUSION_KEY_TYPES = ("domain", "email", "phone", "place_id", "name_location")
EXCLUSION_REASONS = ("current_client", "past_client", "do_not_contact", "competitor", "other")


class Exclusion(Base):
    """Do not contact: a business or person never found, made a lead, or emailed, in any workspace.

    Ported from Lead Intel's exclusions. Matched by the same identity keys (services/identity.py): a
    company by its place id, domain, phone or name and city; a person by their email or its domain.
    """

    __tablename__ = "eo_exclusions"
    __table_args__ = (
        CheckConstraint(_in("key_type", EXCLUSION_KEY_TYPES), name="key_type"),
        CheckConstraint(_in("reason", EXCLUSION_REASONS), name="reason"),
        Index("eo_exclusions_key_key", "key_type", "key_value", unique=True),
    )

    id: Mapped[UUID] = _uuid_pk()
    key_type: Mapped[str] = mapped_column(Text)
    key_value: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, server_default="do_not_contact")
    # For people to recognise the entry; never used for matching.
    business_name: Mapped[str] = mapped_column(Text, server_default="")
    note: Mapped[str] = mapped_column(Text, server_default="")
    added_by: Mapped[UUID | None] = _user_ref()
    created_at: Mapped[datetime] = _created()


class IcpCampaign(Base):
    """The CheapSequencer campaign an ICP's leads are queued into ("Coldbrew: <ICP>"). One CheapSequencer
    account serves every workspace, so it's per ICP. A workspace can still pick another on the ICP profile."""

    __tablename__ = "eo_icp_campaigns"

    lead_intel_icp_id: Mapped[UUID] = mapped_column(primary_key=True)
    campaign_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class WorkspaceCompany(Base):
    """A Lead Intel business pulled into a workspace. Only the link is stored; the company stays in Lead Intel."""

    __tablename__ = "eo_workspace_companies"

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("eo_workspaces.id", ondelete="CASCADE"), primary_key=True)
    # Lead Intel's businesses.id. No foreign key, see module docstring.
    lead_intel_business_id: Mapped[UUID] = mapped_column(primary_key=True, index=True)
    imported_by: Mapped[UUID | None] = _user_ref()
    imported_at: Mapped[datetime] = _created()


COMPANY_STATUSES = ("found", "review", "qualified", "not_fit", "error")  # review: waits for a person to approve


class Company(Base):
    """A company in a workspace's pipeline for one ICP: found, researched, then qualified or not.

    Found in Lead Intel (already verified, so qualified straight away), or in Apollo or on Google
    Maps (researched: website and Instagram read, the ICP's rule checked, scored by Claude).
    Deduplicated per workspace by Lead Intel id, Apollo id, Google place id, and website domain; a
    company found in Lead Intel too is the Lead Intel one.
    """

    __tablename__ = "eo_companies"
    __table_args__ = (
        CheckConstraint(_in("source", ("lead_intel", "apollo", "google_maps")), name="source"),
        CheckConstraint(_in("status", COMPANY_STATUSES), name="status"),
        CheckConstraint("score is null or score between 0 and 100", name="score"),
        Index("eo_companies_workspace_domain_key", "workspace_id", "domain", unique=True,
              postgresql_where=text("domain is not null")),
        Index("eo_companies_workspace_business_key", "workspace_id", "lead_intel_business_id", unique=True,
              postgresql_where=text("lead_intel_business_id is not null")),
        Index("eo_companies_workspace_apollo_key", "workspace_id", "apollo_org_id", unique=True,
              postgresql_where=text("apollo_org_id is not null")),
        Index("eo_companies_workspace_place_key", "workspace_id", "google_place_id", unique=True,
              postgresql_where=text("google_place_id is not null")),
        Index("eo_companies_workspace_icp_status_idx", "workspace_id", "icp_id", "status"),
    )  # fmt: skip

    id: Mapped[UUID] = _uuid_pk()
    archived: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)
    workspace_id: Mapped[UUID] = _workspace()
    # The Lead Intel ICP this company was found for (no foreign key: Lead Intel's table).
    icp_id: Mapped[UUID | None] = mapped_column(default=None)
    source: Mapped[str] = mapped_column(Text)
    lead_intel_business_id: Mapped[UUID | None] = mapped_column(default=None)
    apollo_org_id: Mapped[str | None] = mapped_column(Text, default=None)
    # Google's place id, for companies found on Google Maps.
    google_place_id: Mapped[str | None] = mapped_column(Text, default=None)

    name: Mapped[str] = mapped_column(Text)
    # Lower-case, no www: the dedupe key across sources.
    domain: Mapped[str | None] = mapped_column(Text, default=None)
    website_url: Mapped[str | None] = mapped_column(Text, default=None)
    linkedin_url: Mapped[str | None] = mapped_column(Text, default=None)
    facebook_url: Mapped[str | None] = mapped_column(Text, default=None)
    twitter_url: Mapped[str | None] = mapped_column(Text, default=None)
    instagram_url: Mapped[str | None] = mapped_column(Text, default=None)
    city: Mapped[str | None] = mapped_column(Text, default=None)
    state: Mapped[str | None] = mapped_column(Text, default=None)
    phone: Mapped[str | None] = mapped_column(Text, default=None)

    status: Mapped[str] = mapped_column(Text, server_default="found")
    # Why it qualified or didn't, in one sentence.
    status_reason: Mapped[str | None] = mapped_column(Text, default=None)
    # Set by hand to keep or drop a company whatever the research said.
    overridden: Mapped[bool] = mapped_column(Boolean, server_default="false")
    rule_fit: Mapped[bool | None] = mapped_column(Boolean, default=None)
    rule_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    score: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    score_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    # What was read: website text and pages, socials, Instagram profile, how it was read.
    research: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    researched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


# ---------- Settings and prompts ----------


class Settings(Base):
    """One row per workspace: defaults, budget and the auto-send rule."""

    __tablename__ = "eo_settings"
    __table_args__ = (
        CheckConstraint("score_threshold between 0 and 100", name="score_threshold"),
        CheckConstraint("posts_per_lead between 1 and 20", name="posts_per_lead"),
        CheckConstraint("max_post_age_days >= 1", name="max_post_age_days"),
        CheckConstraint("monthly_budget_usd >= 0", name="monthly_budget_usd"),
        CheckConstraint("daily_auto_send_cap >= 0", name="daily_auto_send_cap"),
        CheckConstraint("good_examples between 0 and 10", name="good_examples"),
        CheckConstraint("bad_examples between 0 and 10", name="bad_examples"),
        CheckConstraint("improve_examples between 3 and 50", name="improve_examples"),
        CheckConstraint("research_max_pages between 1 and 15", name="research_max_pages"),
        CheckConstraint("sample_examples between 0 and 10", name="sample_examples"),
        CheckConstraint("research_max_seconds between 30 and 600", name="research_max_seconds"),
        CheckConstraint("followup_lead_hours between 2 and 72", name="followup_lead_hours"),
    )

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("eo_workspaces.id", ondelete="CASCADE"), primary_key=True)
    default_model: Mapped[str] = mapped_column(Text, server_default="gpt-6.1-sol")
    posts_per_lead: Mapped[int] = mapped_column(SmallInteger, server_default="5")
    max_post_age_days: Mapped[int] = mapped_column(Integer, server_default="60")
    monthly_budget_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default="150")
    # Null uses the workspace's default scoring prompt.
    scoring_prompt_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_prompts.id", ondelete="SET NULL"), default=None)
    score_threshold: Mapped[int] = mapped_column(SmallInteger, server_default="80")
    auto_send: Mapped[bool] = mapped_column(Boolean, server_default="false")
    # Company verification, like email scoring: with auto-approve on, a company scoring at least the threshold is
    # approved; every other verified company waits for a person to approve or reject it.
    company_threshold: Mapped[int] = mapped_column(SmallInteger, server_default="60")
    company_auto_approve: Mapped[bool] = mapped_column(Boolean, server_default="true")
    daily_auto_send_cap: Mapped[int] = mapped_column(Integer, server_default="50")
    # Learning from feedback: how many rated emails Claude sees as examples when it writes an email
    # (the ICP's latest good and bad ones), and how many it reads when suggesting a better prompt.
    good_examples: Mapped[int] = mapped_column(SmallInteger, server_default="3")
    bad_examples: Mapped[int] = mapped_column(SmallInteger, server_default="3")
    improve_examples: Mapped[int] = mapped_column(SmallInteger, server_default="20")
    # How many example emails from the library (eo_example_emails) each email and score prompt gets.
    sample_examples: Mapped[int] = mapped_column(SmallInteger, server_default="3")
    # Research: the most pages read per company (homepage included) while Claude chooses the next
    # page, and the most time spent on one company.
    research_max_pages: Mapped[int] = mapped_column(SmallInteger, server_default="6")
    research_max_seconds: Mapped[int] = mapped_column(SmallInteger, server_default="120")
    # Sending: who the emails are from (company and website; sender_address is no longer used), the generic follow-ups
    # a lead holds until the personal one is written, and how many hours before a follow-up is due
    # it gets written.
    sender_company: Mapped[str] = mapped_column(Text, server_default="")
    sender_address: Mapped[str] = mapped_column(Text, server_default="")
    sender_website: Mapped[str] = mapped_column(Text, server_default="")
    followup_fallback_2: Mapped[str] = mapped_column(Text, server_default=FALLBACK_2)
    followup_fallback_3: Mapped[str] = mapped_column(Text, server_default=FALLBACK_3)
    followup_fallback_4: Mapped[str] = mapped_column(Text, server_default=FALLBACK_4)
    followup_fallback_5: Mapped[str] = mapped_column(Text, server_default=FALLBACK_5)
    followup_lead_hours: Mapped[int] = mapped_column(SmallInteger, server_default="24")
    # The cost estimator (schemas.CostEstimate); null until someone saves one.
    cost_estimate: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    updated_at: Mapped[datetime] = _updated()


class Prompt(Base):
    __tablename__ = "eo_prompts"
    __table_args__ = (
        # verify: company verification, hook: hook selection, email: email generation, score: email scoring.
        CheckConstraint(_in("kind", ("verify", "hook", "email", "score", "followup2", "followup3", "followup4", "followup5", "sort", "sort_people", "product", "analysis")), name="kind"),
        # At most one default per kind in a workspace.
        Index(
            "eo_prompts_one_default_idx",
            "workspace_id",
            "kind",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, server_default="false")
    # Prompts and versions point at each other, so this key is added after both tables exist.
    current_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("eo_prompt_versions.id", ondelete="SET NULL", use_alter=True), default=None
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class PromptVersion(Base):
    """Versions are never edited; saving a prompt adds the next version."""

    __tablename__ = "eo_prompt_versions"
    __table_args__ = (
        UniqueConstraint("prompt_id", "version"),
        CheckConstraint("temperature between 0 and 2", name="temperature"),
        CheckConstraint("max_tokens > 0", name="max_tokens"),
        CheckConstraint("max_posts is null or max_posts between 1 and 50", name="max_posts"),
    )

    id: Mapped[UUID] = _uuid_pk()
    prompt_id: Mapped[UUID] = mapped_column(ForeignKey("eo_prompts.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    system_prompt: Mapped[str] = mapped_column(Text, server_default="")
    user_prompt: Mapped[str] = mapped_column(Text, server_default="")
    model: Mapped[str] = mapped_column(Text)
    temperature: Mapped[Decimal] = mapped_column(Numeric(3, 2), server_default="0.7")
    max_tokens: Mapped[int] = mapped_column(Integer, server_default="600")
    # How many of the newest posts a hook or email prompt is given ({{items}}). Null: the default, 15.
    max_posts: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    note: Mapped[str] = mapped_column(Text, server_default="")
    created_by: Mapped[UUID | None] = _user_ref()
    created_at: Mapped[datetime] = _created()


# What research may read to verify a business: its website (our own reader), Jina when the reader is
# blocked, its Instagram profile (Apify), and Claude's fit score.
VERIFICATION_SOURCES = ("website", "jina", "instagram", "score")


class IcpProfile(Base):
    """What the outreach side needs to know about a Lead Intel ICP to write emails for it."""

    __tablename__ = "eo_icp_profiles"

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("eo_workspaces.id", ondelete="CASCADE"), primary_key=True)
    lead_intel_icp_id: Mapped[UUID] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Stakeholder titles to look for at each company (used by the Apollo search).
    titles: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    company_size: Mapped[str] = mapped_column(Text, default="", server_default="")
    pains: Mapped[str] = mapped_column(Text, default="", server_default="")
    offer: Mapped[str] = mapped_column(Text, default="", server_default="")
    proof: Mapped[str] = mapped_column(Text, default="", server_default="")
    cta: Mapped[str] = mapped_column(Text, default="", server_default="")
    tone: Mapped[str] = mapped_column(Text, default="", server_default="")
    words_to_avoid: Mapped[str] = mapped_column(Text, default="", server_default="")
    hook_guidance: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Lead analysis: whose posts are read for the opening line (linkedin, instagram).
    enabled_sources: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    # Verification: what research reads to decide whether a business is worth contacting.
    verification_sources: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=lambda: list(VERIFICATION_SOURCES), server_default="{website,jina,instagram,score}"
    )
    # Lead analysis limits for this ICP; None means the workspace setting.
    posts_per_source: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    max_post_age_days: Mapped[int | None] = mapped_column(Integer, default=None)
    # Null uses the workspace's default prompt of that kind.
    hook_prompt_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_prompts.id", ondelete="SET NULL"), default=None)
    # The CheapSequencer campaign this ICP's emails are queued into.
    sequencer_campaign_id: Mapped[str | None] = mapped_column(Text, default=None)
    email_prompt_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_prompts.id", ondelete="SET NULL"), default=None)
    verify_prompt_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_prompts.id", ondelete="SET NULL"), default=None)
    # What we sell to this ICP (eo_products). Its details fill the {{product.*}} placeholders.
    product_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_products.id", ondelete="SET NULL"), default=None)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


# ---------- Leads ----------


class Lead(Base):
    """A person we may email: a stakeholder at a company, from Lead Intel, Apollo, a CSV or by hand."""

    __tablename__ = "eo_leads"
    __table_args__ = (
        CheckConstraint(_in("status", LEAD_STATUSES), name="status"),
        CheckConstraint(_in("origin", ("apollo", "apify", "csv", "manual")), name="origin"),
        CheckConstraint("score is null or score between 0 and 100", name="score"),
        # Dedupe within a workspace: same LinkedIn profile or same email is the same person.
        Index("eo_leads_workspace_linkedin_key", "workspace_id", text("lower(linkedin_url)"), unique=True,
              postgresql_where=text("linkedin_url is not null and linkedin_url <> ''")),
        Index("eo_leads_workspace_email_key", "workspace_id", text("lower(email)"), unique=True,
              postgresql_where=text("email is not null and email <> ''")),
        Index("eo_leads_workspace_status_idx", "workspace_id", "status"),
        Index("eo_leads_business_idx", "lead_intel_business_id"),
    )  # fmt: skip

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    # Lead Intel references (no foreign keys, see module docstring).
    lead_intel_icp_id: Mapped[UUID | None] = mapped_column(default=None, index=True)
    lead_intel_business_id: Mapped[UUID | None] = mapped_column(default=None)

    first_name: Mapped[str] = mapped_column(Text, server_default="")
    last_name: Mapped[str] = mapped_column(Text, server_default="")
    email: Mapped[str | None] = mapped_column(Text, default=None)
    # As reported by the provider: verified, likely, guessed, unavailable, invalid...
    email_status: Mapped[str | None] = mapped_column(Text, default=None)
    title: Mapped[str] = mapped_column(Text, server_default="")
    company: Mapped[str] = mapped_column(Text, server_default="")
    website: Mapped[str] = mapped_column(Text, server_default="")
    domain: Mapped[str | None] = mapped_column(Text, default=None)
    linkedin_url: Mapped[str | None] = mapped_column(Text, default=None)
    instagram_url: Mapped[str | None] = mapped_column(Text, default=None)
    facebook_url: Mapped[str | None] = mapped_column(Text, default=None)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")

    origin: Mapped[str] = mapped_column(Text)
    # The provider's id for this person (Apollo person id, Apify profile id), for re-lookups.
    external_id: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(Text, server_default="imported")

    score: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    score_reasons: Mapped[list[str] | None] = mapped_column(JSONB, default=None)
    score_prompt_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("eo_prompt_versions.id", ondelete="SET NULL"), default=None
    )
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    created_by: Mapped[UUID | None] = _user_ref()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Stakeholder(Base):
    """A person found at a company by an Apollo or Apify search, before anyone picks them.

    Searching is free; revealing an email costs credits. Candidates wait here until chosen, and
    choosing one creates the lead (`lead_id`).
    """

    __tablename__ = "eo_stakeholders"
    __table_args__ = (
        # manual: added by hand or from a CSV (one with an Apollo Contact Id is stored as apollo).
        CheckConstraint(_in("source", ("apollo", "apify_linkedin", "manual")), name="source"),
        UniqueConstraint("workspace_id", "source", "external_id"),
        Index("eo_stakeholders_business_idx", "workspace_id", "lead_intel_business_id"),
        Index("eo_stakeholders_icp_idx", "workspace_id", "lead_intel_icp_id"),
    )

    id: Mapped[UUID] = _uuid_pk()
    archived: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)
    workspace_id: Mapped[UUID] = _workspace()
    lead_intel_business_id: Mapped[UUID | None] = mapped_column(default=None)
    # The pipeline company they work at, when found through one.
    company_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_companies.id", ondelete="SET NULL"), default=None, index=True)
    # The Lead Intel ICP whose search found them, when found by ICP rather than by company.
    lead_intel_icp_id: Mapped[UUID | None] = mapped_column(default=None)
    # Their employer as Apollo names it; the only company we know for people found by ICP.
    organization_name: Mapped[str | None] = mapped_column(Text, default=None)
    source: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    first_name: Mapped[str] = mapped_column(Text, server_default="")
    last_name: Mapped[str] = mapped_column(Text, server_default="")
    title: Mapped[str] = mapped_column(Text, server_default="")
    seniority: Mapped[str | None] = mapped_column(Text, default=None)
    linkedin_url: Mapped[str | None] = mapped_column(Text, default=None)
    has_email: Mapped[bool | None] = mapped_column(Boolean, default=None)
    email: Mapped[str | None] = mapped_column(Text, default=None)
    email_status: Mapped[str | None] = mapped_column(Text, default=None)
    revealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    lead_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_leads.id", ondelete="SET NULL"), default=None)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    found_at: Mapped[datetime] = _created()


# ---------- Jobs and spend ----------


class Job(Base):
    """A long-running piece of work (Apify run, bulk reveal, enrichment, drafting) and its progress."""

    __tablename__ = "eo_jobs"
    __table_args__ = (
        CheckConstraint(_in("status", ("queued", "running", "done", "failed", "cancelled")), name="status"),
        Index("eo_jobs_workspace_status_idx", "workspace_id", "status"),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    kind: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)  # what the person sees: "Finding owners at 12 companies"
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    total: Mapped[int | None] = mapped_column(Integer, default=None)  # null until the job knows its size
    done: Mapped[int] = mapped_column(Integer, server_default="0")
    step: Mapped[str | None] = mapped_column(Text, default=None)
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    external_run_id: Mapped[str | None] = mapped_column(Text, default=None)  # e.g. the Apify run id
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), server_default="0")
    created_by: Mapped[UUID | None] = _user_ref()
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Last time the worker reported anything; a running job that stops moving is stalled.
    updated_at: Mapped[datetime] = _updated()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class QuickstartRun(Base):
    """One Quick Start: an ICP taken from a Google Maps search to drafts waiting for review, step by step in the
    background (services/quickstart.py). Each step's batches run as ordinary jobs, so spend is counted by job."""

    __tablename__ = "eo_quickstart_runs"
    __table_args__ = (
        CheckConstraint(_in("status", ("queued", "running", "waiting", "done", "stopped", "failed", "cancelled")), name="status"),
        Index("eo_quickstart_runs_workspace_idx", "workspace_id", "created_at"),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    icp_id: Mapped[UUID] = mapped_column()  # Lead Intel's ICP (no foreign key into Lead Intel)
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    step: Mapped[str] = mapped_column(Text, server_default="find")  # find | research | people | leads | posts | write | done
    # What the person asked for (schemas.QuickstartIn) and how each step went: {step: {state, done, total, note}}.
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    company_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid), server_default="{}")
    lead_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid), server_default="{}")
    job_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid), server_default="{}")
    spend_limit_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), server_default="0")
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    # Set while a worker is on a step, so two workers never run the same run at once.
    busy_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_by: Mapped[UUID | None] = _user_ref()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class SmartList(Base):
    """A high-level group Claude sorted companies or people into, like GoHighLevel's smart lists: "Retail",
    "Manufacturing" for companies, "Owners & Executives", "Marketing" for people. Never a sub-category. A company
    list can hold a draft base ICP, and the Lead Intel ICP made from it."""

    __tablename__ = "eo_smart_lists"
    __table_args__ = (
        CheckConstraint(_in("kind", ("company", "person")), name="kind"),
        # The name compared without case or punctuation: "Food & Beverage" and "food and beverage" are one list.
        UniqueConstraint("workspace_id", "kind", "name_key"),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    name_key: Mapped[str] = mapped_column(Text)
    # Claude's draft ICP for a company list (schemas.SmartListIcpDraft), until someone creates it.
    icp_draft: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    lead_intel_icp_id: Mapped[UUID | None] = mapped_column(default=None)  # no foreign key: Lead Intel's table
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class SmartListMember(Base):
    """Which list a company (eo_companies) or person (eo_stakeholders) is in: one list each. `dedupe_key` is how
    duplicates are recognised (domain, email...): a duplicate joins its twin's list without asking Claude again."""

    __tablename__ = "eo_smart_list_members"
    __table_args__ = (
        CheckConstraint(_in("kind", ("company", "person")), name="kind"),
        Index("eo_smart_list_members_key_idx", "workspace_id", "kind", "dedupe_key"),
    )

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("eo_workspaces.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    member_id: Mapped[UUID] = mapped_column(primary_key=True)  # a company's or a person's id, by kind
    list_id: Mapped[UUID] = mapped_column(ForeignKey("eo_smart_lists.id", ondelete="CASCADE"), index=True)
    dedupe_key: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, server_default="")
    sorted_at: Mapped[datetime] = _created()


class QueuedAddress(Base):
    """Every email address ever queued for sending, for good: nobody is queued twice, whichever lead, ICP or
    workspace they turn up under again, and even after their lead is deleted. The key holds it for two queue
    runs at once."""

    __tablename__ = "eo_queued_addresses"

    email: Mapped[str] = mapped_column(Text, primary_key=True)  # lower case
    workspace_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_workspaces.id", ondelete="SET NULL"), default=None)
    email_id: Mapped[UUID | None] = mapped_column(default=None)  # the first email it went with (no key: it may be deleted)
    campaign_id: Mapped[str | None] = mapped_column(Text, default=None)
    queued_at: Mapped[datetime] = _created()


class ProviderCall(Base):
    """Every paid call: the spend ledger, and the check that stops a retry paying twice."""

    __tablename__ = "eo_provider_calls"
    __table_args__ = (
        CheckConstraint(_in("status", ("succeeded", "failed")), name="status"),
        Index("eo_provider_calls_workspace_called_idx", "workspace_id", "called_at"),
    )

    idempotency_key: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[UUID] = _workspace()
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_jobs.id", ondelete="SET NULL"), default=None)
    lead_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_leads.id", ondelete="SET NULL"), default=None)
    provider: Mapped[str] = mapped_column(Text)  # apollo | apify | claude | cheapsequencer
    endpoint: Mapped[str | None] = mapped_column(Text, default=None)
    action: Mapped[str | None] = mapped_column(Text, default=None)  # what the user did: "Find companies", "Research"...
    model: Mapped[str | None] = mapped_column(Text, default=None)  # Claude model, when provider is claude
    input_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    output_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(Text)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), server_default="0")
    credits: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), default=None)  # Apollo credits, when relevant
    called_at: Mapped[datetime] = _created()


# ---------- Content and insights ----------


class Post(Base):
    """A recent post: a lead's (LinkedIn or Instagram), or a pipeline company's own Instagram post."""

    __tablename__ = "eo_posts"
    __table_args__ = (
        CheckConstraint(_in("source", SOURCES), name="source"),
        CheckConstraint(_in("media_type", ("text", "image", "video", "carousel", "article", "page")), name="media_type"),
        CheckConstraint("lead_id is not null or company_id is not null", name="owner"),
        UniqueConstraint("lead_id", "url"),
        UniqueConstraint("company_id", "url"),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    lead_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_leads.id", ondelete="CASCADE"), default=None)
    company_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_companies.id", ondelete="CASCADE"), default=None)
    source: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text, default=None)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    text: Mapped[str] = mapped_column(Text, server_default="")
    media_type: Mapped[str] = mapped_column(Text, server_default="text")
    is_repost: Mapped[bool] = mapped_column(Boolean, server_default="false")
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_jobs.id", ondelete="SET NULL"), default=None)
    fetched_at: Mapped[datetime] = _created()


class Insight(Base):
    """A ranked hook for a lead: a detail from their posts, their company's posts or site, or their profile."""

    __tablename__ = "eo_insights"
    __table_args__ = (
        CheckConstraint("relevance between 0 and 5", name="relevance"),
        Index("eo_insights_lead_rank_idx", "lead_id", "rank"),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    lead_id: Mapped[UUID] = mapped_column(ForeignKey("eo_leads.id", ondelete="CASCADE"))
    post_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_posts.id", ondelete="CASCADE"), default=None)
    rank: Mapped[int] = mapped_column(SmallInteger)  # 1 is the strongest
    point: Mapped[str] = mapped_column(Text)
    # The opening sentence written from it, and where it came from ("their LinkedIn post", "company website").
    hook: Mapped[str] = mapped_column(Text, server_default="")
    source: Mapped[str] = mapped_column(Text, server_default="")
    why: Mapped[str] = mapped_column(Text, server_default="")
    relevance: Mapped[int] = mapped_column(SmallInteger)
    prompt_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("eo_prompt_versions.id", ondelete="SET NULL"), default=None
    )
    model: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = _created()


# ---------- Emails and sending ----------


class Email(Base):
    """A drafted email for a lead, from draft through approval to sent."""

    __tablename__ = "eo_emails"
    __table_args__ = (
        CheckConstraint(_in("status", ("draft", "edited", "approved", "queued", "sent", "skipped")), name="status"),
        CheckConstraint("sent_by is null or " + _in("sent_by", ("auto", "manual")), name="sent_by"),
        CheckConstraint("step between 1 and 5", name="step"),
        Index("eo_emails_sequence_idx", "workspace_id", "sequence_status", postgresql_where=text("sequence_status is not null")),
        CheckConstraint("relevance between 0 and 5", name="relevance"),
        CheckConstraint("confidence between 0 and 100", name="confidence"),
        Index("eo_emails_workspace_status_idx", "workspace_id", "status"),
        Index("eo_emails_lead_idx", "lead_id"),
        Index("eo_emails_analysis_cohort_idx", "workspace_id", "step", "sent_at"),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    lead_id: Mapped[UUID] = mapped_column(ForeignKey("eo_leads.id", ondelete="CASCADE"))
    hook_prompt_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("eo_prompt_versions.id", ondelete="SET NULL"), default=None
    )
    email_prompt_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("eo_prompt_versions.id", ondelete="SET NULL"), default=None
    )
    chosen_post_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_posts.id", ondelete="SET NULL"), default=None)
    hook: Mapped[str] = mapped_column(Text, server_default="")
    relevance: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    edited_body: Mapped[str | None] = mapped_column(Text, default=None)
    confidence: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    flags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    status: Mapped[str] = mapped_column(Text, server_default="draft")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), server_default="0")
    approved_by: Mapped[UUID | None] = _user_ref()
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    sent_by: Mapped[str | None] = mapped_column(Text, default=None)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # 1 is the first email; 2 to 5 are the follow-ups, written only when they're nearly due.
    step: Mapped[int] = mapped_column(SmallInteger, server_default="1")
    # Where CheapSequencer holds it once queued.
    sequencer_list_id: Mapped[str | None] = mapped_column(Text, default=None)
    sequencer_lead_id: Mapped[str | None] = mapped_column(Text, default=None)
    # The mailbox CheapSequencer assigned the lead: all three emails go out from it.
    sequencer_inbox_id: Mapped[str | None] = mapped_column(Text, default=None)
    sequencer_inbox_email: Mapped[str | None] = mapped_column(Text, default=None)
    sequencer_campaign_id: Mapped[str | None] = mapped_column(Text, default=None)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # On the step-1 email: where the lead's enrollment stands, as last read from CheapSequencer.
    sequence_status: Mapped[str | None] = mapped_column(Text, default=None)  # active | stopped | completed | ...
    sequence_sent_count: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    sequence_last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    sequence_stop_reason: Mapped[str | None] = mapped_column(Text, default=None)
    sequence_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    sequence_raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    # The rated emails shown to Claude as examples when this one was written (see EmailFeedback).
    examples_used: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid), server_default="{}")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class SendEvent(Base):
    """What happened after sending, from CheapSequencer's webhooks."""

    __tablename__ = "eo_send_events"
    __table_args__ = (
        CheckConstraint(
            _in("type", ("queued", "sent", "opened", "replied", "bounced", "unsubscribed", "failed")), name="type"
        ),
        Index("eo_send_events_email_idx", "email_id"),
        Index("eo_send_events_analysis_lead_idx", "workspace_id", "lead_id", "occurred_at"),
        Index("eo_send_events_analysis_email_idx", "workspace_id", "email_id", "occurred_at"),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    email_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_emails.id", ondelete="SET NULL"), default=None)
    lead_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_leads.id", ondelete="SET NULL"), default=None)
    type: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text, server_default="cheapsequencer")
    # The provider's event id: a webhook delivered twice is stored once.
    provider_event_id: Mapped[str | None] = mapped_column(Text, unique=True, default=None)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = _created()


class CompanyPage(Base):
    """A page of a pipeline company's website as it was read: its full text, up to 50,000 characters."""

    __tablename__ = "eo_company_pages"
    __table_args__ = (UniqueConstraint("company_id", "url"),)

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    company_id: Mapped[UUID] = mapped_column(ForeignKey("eo_companies.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)  # homepage | contact | social | about
    via: Mapped[str] = mapped_column(Text)  # own_crawl | jina
    status: Mapped[int | None] = mapped_column(Integer, default=None)
    text: Mapped[str] = mapped_column(Text, server_default="")
    fetched_at: Mapped[datetime] = _created()


FEEDBACK_TAGS = (
    # Good
    "strong_hook", "right_tone", "clear_cta", "send_as_is",
    # Bad
    "too_long", "too_generic", "wrong_hook", "tone_off", "made_up_facts", "weak_cta", "wrong_person",
)  # fmt: skip


class EmailFeedback(Base):
    """A person's verdict on a generated email: good or bad, why, and the text as it was rated.

    Rated emails for an ICP are shown to Claude as examples when it writes the next ones, and feed
    the "improve prompt" suggestion. Nothing is trained; the feedback is used in the prompt.
    """

    __tablename__ = "eo_email_feedback"
    __table_args__ = (
        CheckConstraint(_in("rating", ("good", "bad")), name="rating"),
        Index("eo_email_feedback_icp_idx", "workspace_id", "lead_intel_icp_id", "rating", "updated_at"),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    email_id: Mapped[UUID] = mapped_column(ForeignKey("eo_emails.id", ondelete="CASCADE"), unique=True)
    # The lead's ICP when rated, so examples can be picked per ICP (Lead Intel reference, no foreign key).
    lead_intel_icp_id: Mapped[UUID | None] = mapped_column(default=None)
    rating: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    note: Mapped[str] = mapped_column(Text, server_default="")
    # The email as it was when rated (edits included), so later edits don't change what was judged.
    subject: Mapped[str] = mapped_column(Text, server_default="")
    body: Mapped[str] = mapped_column(Text, server_default="")
    hook: Mapped[str] = mapped_column(Text, server_default="")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Product(Base):
    """Something the workspace sells. Each ICP picks the one it's offered (IcpProfile.product_id)."""

    __tablename__ = "eo_products"
    __table_args__ = (Index("eo_products_workspace_name_key", "workspace_id", text("lower(name)"), unique=True),)

    id: Mapped[UUID] = _uuid_pk()
    archived: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)
    workspace_id: Mapped[UUID] = _workspace()
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default="")  # what it is
    value: Mapped[str] = mapped_column(Text, server_default="")  # what it does for the customer
    proof: Mapped[str] = mapped_column(Text, server_default="")  # results we can claim, used word for word
    offer: Mapped[str] = mapped_column(Text, server_default="")  # pricing or the offer, e.g. "first month free"
    cta: Mapped[str] = mapped_column(Text, server_default="")  # the next step we ask for
    url: Mapped[str] = mapped_column(Text, server_default="")
    custom_fields: Mapped[list] = mapped_column(JSONB, server_default="[]", default=list)
    notes: Mapped[list] = mapped_column(JSONB, server_default="[]", default=list)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class ExampleEmail(Base):
    """An email in the voice we want, for the email and scoring prompts ({{examples.samples}}).

    Written by hand, or saved from a generated email on the Feedback page. Used for the ICPs it's
    linked to, or for every ICP.
    """

    __tablename__ = "eo_example_emails"
    __table_args__ = (CheckConstraint(_in("source", ("manual", "generated")), name="source"),)

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    subject: Mapped[str] = mapped_column(Text, server_default="")
    body: Mapped[str] = mapped_column(Text)
    # What makes it a good example, in the reviewer's words. Shown to Claude next to it.
    note: Mapped[str] = mapped_column(Text, server_default="")
    all_icps: Mapped[bool] = mapped_column(Boolean, server_default="false")
    # Lead Intel ICP ids it applies to (references only, no foreign key).
    icp_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid), server_default="{}")
    source: Mapped[str] = mapped_column(Text, server_default="manual")
    # The generated email it was saved from, when it came from the Feedback page.
    source_email_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_emails.id", ondelete="SET NULL"), default=None)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class SavedView(Base):
    __tablename__ = "eo_saved_views"
    __table_args__ = (
        CheckConstraint("visibility in ('personal', 'workspace')", name="visibility"),
        Index("eo_saved_views_lookup_idx", "workspace_id", "page", "visibility", "user_id"),
    )
    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("eo_workspaces.id", ondelete="CASCADE"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("eo_users.id", ondelete="CASCADE"))
    page: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    filters: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class AnalysisReport(Base):
    __tablename__ = "eo_analysis_reports"
    __table_args__ = (Index("eo_analysis_reports_history_idx", "workspace_id", "created_at"),)
    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    created_by: Mapped[UUID | None] = _user_ref()
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_jobs.id", ondelete="SET NULL"), unique=True, default=None)
    range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    filters: Mapped[dict] = mapped_column(JSONB)
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()


# ---------- Coldbrew's own sending (replacing CheapSequencer step by step; migration 0036) ----------


class Persona(Base):
    """Who an email is from: the display name and signature a reader sees."""

    __tablename__ = "eo_personas"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)
    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    name: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, server_default="")
    signature: Mapped[str] = mapped_column(Text, server_default="")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


SECURITY = ("ssl", "starttls")
SENDER_STATUSES = ("untested", "ok", "failed", "paused")


class SenderAccount(Base):
    """A mailbox Coldbrew sends from and reads replies in. Its passwords are in Supabase Vault (services/vault.py);
    only the secret ids are here, and neither ever leaves the server."""

    __tablename__ = "eo_sender_accounts"
    __table_args__ = (
        CheckConstraint("channel in ('email')", name="channel"),
        CheckConstraint(_in("smtp_security", SECURITY), name="smtp_security"),
        CheckConstraint(f"imap_security is null or {_in('imap_security', SECURITY)}", name="imap_security"),
        CheckConstraint(_in("status", SENDER_STATUSES), name="status"),
        CheckConstraint("daily_limit between 1 and 500", name="daily_limit"),
        CheckConstraint(_in("pool", ("sending", "warming")), name="pool"),
        CheckConstraint("warmup_daily_target between 1 and 100", name="warmup_daily_target"),
        CheckConstraint("warmup_reply_percent between 0 and 100", name="warmup_reply_percent"),
        UniqueConstraint("workspace_id", "from_email"),
    )
    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    persona_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_personas.id", ondelete="SET NULL"), default=None)
    channel: Mapped[str] = mapped_column(Text, server_default="email")
    label: Mapped[str] = mapped_column(Text, server_default="")
    from_name: Mapped[str] = mapped_column(Text, server_default="")
    from_email: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text, server_default="custom")
    smtp_host: Mapped[str] = mapped_column(Text)
    smtp_port: Mapped[int] = mapped_column(Integer)
    smtp_security: Mapped[str] = mapped_column(Text)
    smtp_username: Mapped[str] = mapped_column(Text)
    smtp_secret_id: Mapped[UUID | None] = mapped_column(Uuid, default=None)
    imap_host: Mapped[str | None] = mapped_column(Text, default=None)
    imap_port: Mapped[int | None] = mapped_column(Integer, default=None)
    imap_security: Mapped[str | None] = mapped_column(Text, default=None)
    imap_username: Mapped[str | None] = mapped_column(Text, default=None)
    imap_secret_id: Mapped[UUID | None] = mapped_column(Uuid, default=None)
    daily_limit: Mapped[int] = mapped_column(Integer, server_default="30")
    # sending: campaigns send from it. warming: it only trades warm-up emails with the other warming mailboxes.
    pool: Mapped[str] = mapped_column(Text, server_default="sending")
    warmup_daily_target: Mapped[int] = mapped_column(SmallInteger, server_default="10")
    warmup_reply_percent: Mapped[int] = mapped_column(SmallInteger, server_default="30")
    warmup_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    status: Mapped[str] = mapped_column(Text, server_default="untested")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[UUID | None] = _user_ref()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class SenderUsage(Base):
    """How many an account sent on a day, so its daily limit holds across restarts."""

    __tablename__ = "eo_sender_usage"
    sender_account_id: Mapped[UUID] = mapped_column(ForeignKey("eo_sender_accounts.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[Any] = mapped_column(Date, primary_key=True)
    sent: Mapped[int] = mapped_column(Integer, server_default="0")


class Sequence(Base):
    """A campaign: its steps, sending window and status."""

    __tablename__ = "eo_sequences"
    __table_args__ = (CheckConstraint(_in("status", ("draft", "active", "paused", "archived")), name="status"),)
    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="draft")
    lead_intel_icp_id: Mapped[UUID | None] = mapped_column(Uuid, default=None)
    schedule: Mapped[dict] = mapped_column(JSONB)
    created_by: Mapped[UUID | None] = _user_ref()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class SequenceStep(Base):
    __tablename__ = "eo_sequence_steps"
    __table_args__ = (
        CheckConstraint(_in("thread_mode", ("new", "reply")), name="thread_mode"),
        CheckConstraint("delay_days between 0 and 90", name="delay_days"),
        UniqueConstraint("sequence_id", "step_order"),
    )
    id: Mapped[UUID] = _uuid_pk()
    sequence_id: Mapped[UUID] = mapped_column(ForeignKey("eo_sequences.id", ondelete="CASCADE"))
    step_order: Mapped[int] = mapped_column(SmallInteger)
    delay_days: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    thread_mode: Mapped[str] = mapped_column(Text, server_default="reply")
    subject: Mapped[str] = mapped_column(Text, server_default="")
    body: Mapped[str] = mapped_column(Text, server_default="")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Enrollment(Base):
    """A lead in a sequence: the account that sends to them, the next step and when, and why it stopped."""

    __tablename__ = "eo_enrollments"
    __table_args__ = (
        CheckConstraint(_in("status", ("active", "paused", "completed", "stopped")), name="status"),
        UniqueConstraint("sequence_id", "lead_id"),
        Index("eo_enrollments_due_idx", "status", "next_run_at"),
    )
    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    sequence_id: Mapped[UUID] = mapped_column(ForeignKey("eo_sequences.id", ondelete="CASCADE"))
    lead_id: Mapped[UUID] = mapped_column(ForeignKey("eo_leads.id", ondelete="CASCADE"), index=True)
    sender_account_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_sender_accounts.id", ondelete="SET NULL"), default=None)
    email_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_emails.id", ondelete="SET NULL"), default=None)
    status: Mapped[str] = mapped_column(Text, server_default="active")
    stop_reason: Mapped[str | None] = mapped_column(Text, default=None)
    next_step_order: Mapped[int] = mapped_column(SmallInteger, server_default="1")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    thread_message_id: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class StepRun(Base):
    """One attempt to send a step: sent (with its Message-ID), failed, skipped, or a dry run that only logged."""

    __tablename__ = "eo_step_runs"
    __table_args__ = (CheckConstraint(_in("status", ("sent", "failed", "skipped", "dry_run")), name="status"),)
    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    enrollment_id: Mapped[UUID] = mapped_column(ForeignKey("eo_enrollments.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_sequence_steps.id", ondelete="SET NULL"), default=None)
    sender_account_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_sender_accounts.id", ondelete="SET NULL"), default=None)
    status: Mapped[str] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(Text, default=None, index=True)
    subject: Mapped[str] = mapped_column(Text, server_default="")
    body: Mapped[str] = mapped_column(Text, server_default="")
    error: Mapped[str | None] = mapped_column(Text, default=None)
    ran_at: Mapped[datetime] = _created()


class InboundEvent(Base):
    """A reply, bounce, unsubscribe or auto-reply the inbox reader found. A database trigger stops every enrollment
    of the lead on a reply, bounce or unsubscribe (migration 0036)."""

    __tablename__ = "eo_inbound_events"
    __table_args__ = (
        CheckConstraint(_in("type", ("reply", "bounce", "unsubscribe", "auto_reply")), name="type"),
        UniqueConstraint("sender_account_id", "message_id"),
    )
    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    sender_account_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_sender_accounts.id", ondelete="SET NULL"), default=None)
    enrollment_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_enrollments.id", ondelete="SET NULL"), default=None)
    lead_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_leads.id", ondelete="SET NULL"), default=None, index=True)
    type: Mapped[str] = mapped_column(Text)
    message_id: Mapped[str] = mapped_column(Text)
    in_reply_to: Mapped[str | None] = mapped_column(Text, default=None)
    from_email: Mapped[str] = mapped_column(Text, server_default="")
    subject: Mapped[str] = mapped_column(Text, server_default="")
    snippet: Mapped[str] = mapped_column(Text, server_default="")
    folder: Mapped[str] = mapped_column(Text, server_default="INBOX")
    raw: Mapped[dict | None] = mapped_column(JSONB, default=None)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class WarmupMessage(Base):
    """One warm-up email between two warming mailboxes, and what the receiving one did with it (migration 0037)."""

    __tablename__ = "eo_warmup_messages"
    __table_args__ = (
        CheckConstraint(_in("status", ("sent", "dry_run", "failed")), name="status"),
        CheckConstraint("landed_in is null or landed_in in ('inbox', 'spam')", name="landed_in"),
        UniqueConstraint("message_id"),
        Index("eo_warmup_messages_from_idx", "from_account_id", "sent_at"),
        Index("eo_warmup_messages_to_idx", "to_account_id", "sent_at"),
    )
    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    from_account_id: Mapped[UUID] = mapped_column(ForeignKey("eo_sender_accounts.id", ondelete="CASCADE"))
    to_account_id: Mapped[UUID] = mapped_column(ForeignKey("eo_sender_accounts.id", ondelete="CASCADE"))
    message_id: Mapped[str] = mapped_column(Text)
    reply_to_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_warmup_messages.id", ondelete="SET NULL"), default=None)
    subject: Mapped[str] = mapped_column(Text, server_default="")
    status: Mapped[str] = mapped_column(Text, server_default="sent")
    sent_at: Mapped[datetime] = _created()
    landed_in: Mapped[str | None] = mapped_column(Text, default=None)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    starred: Mapped[bool] = mapped_column(Boolean, server_default="false")
    rescued_from_spam: Mapped[bool] = mapped_column(Boolean, server_default="false")
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class OwnSendingSettings(Base):
    """A workspace's sending limits and warm-up settings (the sending dashboard's Settings). No row: the defaults.
    Live sending stays a server switch (OWN_SENDING_LIVE, OWN_WARMUP_LIVE); these can only hold back."""

    __tablename__ = "eo_own_sending_settings"
    __table_args__ = (
        CheckConstraint("default_daily_limit between 1 and 500", name="default_daily_limit"),
        CheckConstraint("workspace_daily_cap is null or workspace_daily_cap between 1 and 100000", name="workspace_daily_cap"),
        CheckConstraint("min_gap_minutes between 1 and 240 and max_gap_minutes between min_gap_minutes and 240", name="gap"),
        CheckConstraint("warmup_start_per_day between 1 and 50 and warmup_ramp_per_day between 0 and 20", name="ramp"),
        CheckConstraint("warmup_default_target between 1 and 100", name="warmup_default_target"),
        CheckConstraint("warmup_default_reply_percent between 0 and 100 and warmup_star_percent between 0 and 100", name="percent"),
    )
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("eo_workspaces.id", ondelete="CASCADE"), primary_key=True)
    sending_paused: Mapped[bool] = mapped_column(Boolean, server_default="false")
    default_daily_limit: Mapped[int] = mapped_column(SmallInteger, server_default="30")
    workspace_daily_cap: Mapped[int | None] = mapped_column(Integer, default=None)
    min_gap_minutes: Mapped[int] = mapped_column(SmallInteger, server_default="5")
    max_gap_minutes: Mapped[int] = mapped_column(SmallInteger, server_default="15")
    warmup_enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")
    warmup_timezone: Mapped[str] = mapped_column(Text, server_default="America/New_York")
    warmup_start: Mapped[str] = mapped_column(Text, server_default="09:00")
    warmup_end: Mapped[str] = mapped_column(Text, server_default="19:00")
    warmup_days: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger), server_default="{1,2,3,4,5,6,7}")
    warmup_start_per_day: Mapped[int] = mapped_column(SmallInteger, server_default="3")
    warmup_ramp_per_day: Mapped[int] = mapped_column(SmallInteger, server_default="2")
    warmup_default_target: Mapped[int] = mapped_column(SmallInteger, server_default="20")
    warmup_default_reply_percent: Mapped[int] = mapped_column(SmallInteger, server_default="30")
    warmup_star_percent: Mapped[int] = mapped_column(SmallInteger, server_default="25")
    updated_at: Mapped[datetime] = _updated()


class SocialAccount(Base):
    """A LinkedIn or Instagram account connected through Unipile, and the mailbox it belongs to (migration 0038).
    Unipile holds the login; this keeps its account id, what to show and its status."""

    __tablename__ = "eo_social_accounts"
    __table_args__ = (
        CheckConstraint(_in("provider", ("linkedin", "instagram")), name="provider"),
        CheckConstraint(_in("status", ("ok", "connecting", "credentials", "permissions", "error", "stopped")), name="status"),
        UniqueConstraint("unipile_account_id"),
    )
    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace()
    provider: Mapped[str] = mapped_column(Text)
    unipile_account_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, server_default="")
    username: Mapped[str] = mapped_column(Text, server_default="")
    status: Mapped[str] = mapped_column(Text, server_default="ok")
    sender_account_id: Mapped[UUID | None] = mapped_column(ForeignKey("eo_sender_accounts.id", ondelete="SET NULL"), default=None, index=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
