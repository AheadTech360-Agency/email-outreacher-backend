"""Reads and writes for our own `outreach.eo_` tables. Lead Intel is read in lead_intel.py, never here."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Text, Uuid, and_, case, cast, delete, exists, func, inspect, literal, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY, array, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, defer, load_only

from models import (
    Company, CompanyPage, Email, EmailFeedback, ExampleEmail, IcpCampaign, IcpProfile, Product, Settings, Insight, Lead, Post, Prompt, PromptVersion, ProviderCall, Stakeholder,
    Exclusion, Job, QueuedAddress, QuickstartRun, SendEvent, SmartList, SmartListMember, User, Workspace, WorkspaceCompany, WorkspaceMember, FALLBACK_2, FALLBACK_3, FALLBACK_4, FALLBACK_5,
)  # fmt: skip
from services.apify import Employee, FetchedPost, linkedin_slug, search_name
from services.cost_estimate import DEFAULT_ESTIMATE, upgrade as upgrade_estimate
from services.apollo import ApolloOrganization, FoundPerson, RevealedPerson
from services.google_places import Place
from services.identity import identity_keys
from services.leads import Existing, LeadInput, clean_lead
from services.prompts import DEFAULTS
from services import listing
from services.smart_lists import company_key, person_key
from services.website import normalize_domain


def _skip(*columns: Any) -> list[Any]:
    """Leave these columns out of a read (they're most of the row, and every byte read is database egress). Reading
    one afterwards raises instead of quietly loading it, so a list that starts needing it fails loudly in tests."""
    return [defer(c, raiseload=True) for c in columns]


class IcpCampaignRepo:
    """Which CheapSequencer campaign each ICP's leads go into (outreach.eo_icp_campaigns only)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def all(self) -> dict[UUID, str]:
        return {row.lead_intel_icp_id: row.campaign_id for row in await self.session.scalars(select(IcpCampaign))}

    async def link(self, icp_id: UUID, campaign_id: str) -> None:
        row = await self.session.get(IcpCampaign, icp_id)
        if row is None:
            self.session.add(IcpCampaign(lead_intel_icp_id=icp_id, campaign_id=campaign_id))
        else:
            row.campaign_id = campaign_id
        await self.session.commit()

    async def unlink(self, icp_id: UUID) -> None:
        await self.session.execute(delete(IcpCampaign).where(IcpCampaign.lead_intel_icp_id == icp_id))
        await self.session.commit()

    async def usage(self, icp_id: UUID) -> dict[str, int]:
        """What in Coldbrew, in any workspace, still belongs to the ICP. Deleting it would orphan these."""
        counts = {}
        for label, model, column in (
            ("leads", Lead, Lead.lead_intel_icp_id), ("companies", Company, Company.icp_id),
            ("stakeholders", Stakeholder, Stakeholder.lead_intel_icp_id), ("rated emails", EmailFeedback, EmailFeedback.lead_intel_icp_id),
        ):  # fmt: skip
            counts[label] = int(await self.session.scalar(select(func.count()).select_from(model).where(column == icp_id)) or 0)
        return counts

    async def sending(self, icp_id: UUID) -> int:
        """Emails queued or sent for the ICP's leads: its campaign is in use, so it isn't deleted."""
        query = select(func.count()).select_from(Email).join(Lead, Lead.id == Email.lead_id).where(
            Lead.lead_intel_icp_id == icp_id, Email.status.in_(("queued", "sent"))
        )
        return int(await self.session.scalar(query) or 0)

    async def detach(self, icp_id: UUID) -> None:
        """A deleted ICP's companies, people, leads, ratings and smart lists are kept, with no ICP: they show
        under "No ICP" until they're given another."""
        for model, column in (
            (Company, Company.icp_id), (Stakeholder, Stakeholder.lead_intel_icp_id), (Lead, Lead.lead_intel_icp_id),
            (EmailFeedback, EmailFeedback.lead_intel_icp_id), (SmartList, SmartList.lead_intel_icp_id),
        ):  # fmt: skip
            await self.session.execute(update(model).where(column == icp_id).values({column.key: None}))
        await self.session.commit()

    async def forget(self, icp_id: UUID) -> None:
        """Remove a deleted ICP's settings: its profiles, its campaign link, and it from example emails."""
        await self.session.execute(delete(IcpProfile).where(IcpProfile.lead_intel_icp_id == icp_id))
        await self.session.execute(delete(IcpCampaign).where(IcpCampaign.lead_intel_icp_id == icp_id))
        await self.session.execute(
            update(ExampleEmail).where(ExampleEmail.icp_ids.any(icp_id)).values(icp_ids=func.array_remove(ExampleEmail.icp_ids, icp_id))
        )
        await self.session.commit()


class IcpProfileRepo:
    """Reads and writes `outreach.eo_icp_profiles` only."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def workspace_exists(self, workspace_id: UUID) -> bool:
        return (await self.session.get(Workspace, workspace_id)) is not None

    async def get_many(self, workspace_id: UUID, icp_ids: list[UUID]) -> dict[UUID, IcpProfile]:
        if not icp_ids:
            return {}
        result = await self.session.scalars(
            select(IcpProfile).where(IcpProfile.workspace_id == workspace_id, IcpProfile.lead_intel_icp_id.in_(icp_ids))
        )
        return {p.lead_intel_icp_id: p for p in result}

    async def upsert(self, workspace_id: UUID, icp_id: UUID, values: dict) -> IcpProfile:
        statement = (
            insert(IcpProfile)
            .values(workspace_id=workspace_id, lead_intel_icp_id=icp_id, **values)
            .on_conflict_do_update(
                index_elements=[IcpProfile.workspace_id, IcpProfile.lead_intel_icp_id],
                set_={**values, "updated_at": func.now()},
            )
            .returning(IcpProfile)
        )
        profile = (await self.session.scalars(statement)).one()
        await self.session.commit()
        return profile


class PromptRepo:
    """Prompt templates and their versions. `outreach.eo_prompts` and `eo_prompt_versions` only.

    Every workspace has one default prompt of each kind; missing ones are created from the
    built-in defaults the first time the workspace's prompts are read.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def workspace_exists(self, workspace_id: UUID) -> bool:
        return (await self.session.get(Workspace, workspace_id)) is not None

    async def ensure_defaults(self, workspace_id: UUID) -> None:
        have = set(await self.session.scalars(select(Prompt.kind).where(Prompt.workspace_id == workspace_id).distinct()))
        missing = [k for k in DEFAULTS if k not in have]
        if not missing:
            return
        for kind in missing:
            d = DEFAULTS[kind]
            prompt = Prompt(workspace_id=workspace_id, name=d.name, kind=kind, is_default=True)
            self.session.add(prompt)
            await self.session.flush()
            version = PromptVersion(
                prompt_id=prompt.id, version=1, system_prompt=d.system_prompt, user_prompt=d.user_prompt, model=d.model,
                temperature=d.temperature, max_tokens=d.max_tokens, max_posts=d.max_posts, note="Built-in default",
            )  # fmt: skip
            self.session.add(version)
            await self.session.flush()
            prompt.current_version_id = version.id
        await self.session.commit()

    async def list(self, workspace_id: UUID) -> tuple[list[Prompt], list[PromptVersion]]:
        prompts = list(
            await self.session.scalars(
                select(Prompt).where(Prompt.workspace_id == workspace_id).order_by(Prompt.kind, Prompt.created_at)
            )
        )
        if not prompts:
            return [], []
        versions = await self.session.scalars(
            select(PromptVersion)
            .where(PromptVersion.prompt_id.in_([p.id for p in prompts]))
            .order_by(PromptVersion.prompt_id, PromptVersion.version)
        )
        return prompts, list(versions)

    async def get(self, workspace_id: UUID, prompt_id: UUID) -> Prompt | None:
        prompt = await self.session.get(Prompt, prompt_id)
        return prompt if prompt and prompt.workspace_id == workspace_id else None

    async def current_version(self, prompt: Prompt) -> PromptVersion | None:
        return await self.session.get(PromptVersion, prompt.current_version_id) if prompt.current_version_id else None

    async def default(self, workspace_id: UUID, kind: str) -> Prompt | None:
        return await self.session.scalar(
            select(Prompt).where(Prompt.workspace_id == workspace_id, Prompt.kind == kind, Prompt.is_default)
        )

    async def for_icp(self, workspace_id: UUID, kind: str, chosen_id: UUID | None) -> PromptVersion | None:
        """The current version of the ICP's chosen prompt of this kind, else the workspace default's."""
        prompt = await self.get(workspace_id, chosen_id) if chosen_id else None
        if prompt is None or prompt.kind != kind:
            await self.ensure_defaults(workspace_id)
            prompt = await self.default(workspace_id, kind)
        return await self.current_version(prompt) if prompt else None

    async def create(
        self, workspace_id: UUID, name: str, kind: str, draft: dict[str, Any], note: str
    ) -> tuple[Prompt, PromptVersion]:
        await self.ensure_defaults(workspace_id)
        prompt = Prompt(workspace_id=workspace_id, name=name, kind=kind, is_default=False)
        self.session.add(prompt)
        await self.session.flush()
        version = await self._add_version(prompt, draft, note)
        await self.session.commit()
        return prompt, version

    async def add_version(self, prompt: Prompt, draft: dict[str, Any], note: str) -> PromptVersion:
        version = await self._add_version(prompt, draft, note)
        await self.session.commit()
        return version

    async def _add_version(self, prompt: Prompt, draft: dict[str, Any], note: str) -> PromptVersion:
        latest = await self.session.scalar(
            select(func.max(PromptVersion.version)).where(PromptVersion.prompt_id == prompt.id)
        )
        version = PromptVersion(prompt_id=prompt.id, version=(latest or 0) + 1, note=note, **draft)
        self.session.add(version)
        await self.session.flush()
        prompt.current_version_id = version.id
        prompt.updated_at = datetime.now(UTC)
        return version

    async def rename(self, prompt: Prompt, name: str) -> None:
        prompt.name = name
        prompt.updated_at = datetime.now(UTC)
        await self.session.commit()
        await self.session.refresh(prompt)

    async def make_default(self, prompt: Prompt) -> None:
        # One default per kind (a unique index): clear the old one before setting the new one.
        old = await self.default(prompt.workspace_id, prompt.kind)
        if old and old.id != prompt.id:
            old.is_default = False
            await self.session.flush()
        prompt.is_default = True
        await self.session.commit()
        # The row's update timestamp is set by the database; read it back.
        await self.session.refresh(prompt)

    async def in_use(self, prompt: Prompt) -> int:
        """How many ICP profiles chose this prompt."""
        column = {
            "verify": IcpProfile.verify_prompt_id, "hook": IcpProfile.hook_prompt_id, "email": IcpProfile.email_prompt_id,
        }.get(prompt.kind)  # fmt: skip
        if column is None:
            return 0
        return await self.session.scalar(select(func.count()).select_from(IcpProfile).where(column == prompt.id)) or 0

    async def delete(self, prompt: Prompt) -> None:
        await self.session.delete(prompt)
        await self.session.commit()


_full_name = func.lower(func.trim(func.concat(Lead.first_name, " ", Lead.last_name)))


@dataclass
class LeadFilters:
    """The Leads pages' filters (app/leads/page.tsx, app/icps/[id]/leads/page.tsx)."""

    queued: bool = True
    icp_id: UUID | None = None
    status: str | None = None
    source: str | None = None
    email: str | None = None
    linkedin: str | None = None
    posts: str | None = None
    score: str | None = None
    queued_within: str | None = None
    q: str | None = None
    q_title: bool = True  # the Leads page searches titles too; an ICP's Leads tab doesn't


class LeadRepo:
    """Reads and writes `outreach.eo_leads` only."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def workspace_exists(self, workspace_id: UUID) -> bool:
        return (await self.session.get(Workspace, workspace_id)) is not None

    async def find_existing(self, workspace_id: UUID, leads: list[LeadInput]) -> Existing:
        """Which of these leads' emails, LinkedIn URLs and domain+name pairs the workspace already has."""
        emails = {lead.email for lead in leads if lead.email}
        linkedins = {lead.linkedin_url.lower() for lead in leads if lead.linkedin_url}
        name_keys = {lead.name_key for lead in leads if lead.name_key}
        conditions = []
        if emails:
            conditions.append(func.lower(Lead.email).in_(emails))
        if linkedins:
            conditions.append(func.lower(Lead.linkedin_url).in_(linkedins))
        if name_keys:
            conditions.append(tuple_(Lead.domain, _full_name).in_(list(name_keys)))
        found = Existing()
        if not conditions:
            return found
        rows = await self.session.execute(
            select(func.lower(Lead.email), func.lower(Lead.linkedin_url), Lead.domain, _full_name).where(
                Lead.workspace_id == workspace_id, or_(*conditions)
            )
        )
        for email, linkedin, domain, name in rows:
            if email:
                found.emails.add(email)
            if linkedin:
                found.linkedins.add(linkedin)
            if domain and name:
                found.name_keys.add((domain, name))
        return found

    async def insert_many(
        self, workspace_id: UUID, icp_id: UUID | None, origin: str, leads: list[LeadInput]
    ) -> list[UUID | None]:
        """Insert in order; the id of each new lead, or None where the database found it a duplicate.

        The unique indexes on email and LinkedIn URL are the final word: a lead added by someone else
        between the duplicate check and this insert is skipped, not doubled.
        """
        ids: list[UUID | None] = []
        for lead in leads:
            statement = (
                insert(Lead)
                .values(
                    workspace_id=workspace_id,
                    lead_intel_icp_id=icp_id,
                    first_name=lead.first_name,
                    last_name=lead.last_name,
                    email=lead.email or None,
                    title=lead.title,
                    company=lead.company,
                    website=lead.website,
                    domain=lead.domain or None,
                    linkedin_url=lead.linkedin_url or None,
                    instagram_url=lead.instagram_url or None,
                    facebook_url=lead.facebook_url or None,
                    extra=lead.extra,
                    origin=origin,
                )
                .on_conflict_do_nothing()
                .returning(Lead.id)
            )
            ids.append((await self.session.execute(statement)).scalar_one_or_none())
        await self.session.commit()
        return ids

    async def get(self, workspace_id: UUID, lead_id: UUID) -> Lead | None:
        return await self.session.scalar(select(Lead).where(Lead.workspace_id == workspace_id, Lead.id == lead_id))

    async def queued_at(self, lead_ids: list[UUID]) -> dict[UUID, datetime]:
        """When each of these leads had its first email queued for sending (leads never queued are left out)."""
        if not lead_ids:
            return {}
        rows = await self.session.execute(
            select(Email.lead_id, func.min(func.coalesce(Email.queued_at, Email.updated_at)))
            .where(Email.lead_id.in_(lead_ids), Email.step == 1, Email.sequencer_campaign_id.is_not(None))
            .group_by(Email.lead_id)
        )
        return dict(rows.all())

    # When a lead's first email was queued (see queued_at): a lead counts as a lead from then on.
    QUEUED_AT = (
        select(func.min(func.coalesce(Email.queued_at, Email.updated_at)))
        .where(Email.lead_id == Lead.id, Email.step == 1, Email.sequencer_campaign_id.is_not(None))
        .scalar_subquery()
    )  # fmt: skip
    QUEUED = exists().where(Email.lead_id == Lead.id, Email.step == 1, Email.sequencer_campaign_id.is_not(None))
    ORDER = (Lead.created_at.desc(), Lead.id)

    def _matching(self, workspace_id: UUID, f: LeadFilters, now: datetime) -> list[Any]:
        """Everything the Leads pages filter on, as the browser did."""
        def url(column: Any) -> Any:  # a source the lead has (leadSources: any non-empty value)
            return func.coalesce(column, "") != ""

        sources = {"linkedin": url(Lead.linkedin_url), "instagram": url(Lead.instagram_url), "facebook": url(Lead.facebook_url),
                   "website": url(Lead.website),
                   "none": and_(~url(Lead.linkedin_url), ~url(Lead.instagram_url), ~url(Lead.facebook_url))}  # fmt: skip
        verified = and_(url(Lead.email), func.lower(Lead.email_status).in_(("verified", "valid")))
        email = {"none": ~url(Lead.email), "has": url(Lead.email), "verified": verified}.get(f.email or "all")
        text = func.concat(func.trim(func.concat(Lead.first_name, " ", Lead.last_name)), " ", func.coalesce(Lead.email, ""), " ",
                           func.coalesce(Lead.company, ""), *([" ", func.coalesce(Lead.title, "")] if f.q_title else []))  # fmt: skip
        source = None
        if f.source and f.source != "all":
            source = sources.get(f.source, literal(False))
        return [Lead.workspace_id == workspace_id, *listing.where(
            self.QUEUED if f.queued else None,
            Lead.lead_intel_icp_id == f.icp_id if f.icp_id else None,
            Lead.status == f.status if f.status and f.status != "all" else None,
            source, email,
            listing.presence(f.linkedin, listing.has_text(Lead.linkedin_url)),
            listing.presence(f.posts, exists().where(Post.lead_id == Lead.id)),
            listing.score_band(f.score, Lead.score),
            listing.recent(f.queued_within, self.QUEUED_AT, now),
            listing.whole_text(f.q, text),
        )]  # fmt: skip

    async def page(self, workspace_id: UUID, f: LeadFilters, limit: int, offset: int, now: datetime) -> tuple[list[Lead], int]:
        """One page of leads (without their score reasons), and how many match."""
        where = self._matching(workspace_id, f, now)
        total = (await self.session.execute(select(func.count()).select_from(Lead).where(*where))).scalar_one()
        query = select(Lead).where(*where).options(*_skip(Lead.score_reasons)).order_by(*self.ORDER).limit(limit).offset(offset)
        return list(await self.session.scalars(query)), total

    BRIEF = (Lead.id, Lead.lead_intel_icp_id, Lead.status, Lead.linkedin_url, Lead.instagram_url)

    async def matching(self, workspace_id: UUID, f: LeadFilters, now: datetime, cap: int) -> list[Any]:
        """Every matching lead in list order, as the few fields selection and the run dialogs read."""
        query = select(*self.BRIEF).where(*self._matching(workspace_id, f, now)).order_by(*self.ORDER).limit(cap)
        return list((await self.session.execute(query)).all())

    async def brief(self, workspace_id: UUID, ids: list[UUID]) -> list[Any]:
        query = select(*self.BRIEF).where(Lead.workspace_id == workspace_id, Lead.id.in_(ids))
        return list((await self.session.execute(query)).all())

    async def queued_counts_by(self, workspace_id: UUID, icp_id: UUID | None) -> tuple[int, int, dict[str, int]]:
        """Leads (queued ones only, as the Leads pages list them), those without an email, and how many are in each
        status, for one ICP or the workspace."""
        where = [Lead.workspace_id == workspace_id, self.QUEUED, *([Lead.lead_intel_icp_id == icp_id] if icp_id else [])]
        no_email = (await self.session.execute(select(func.count()).select_from(Lead).where(*where, func.coalesce(Lead.email, "") == ""))).scalar_one()
        by_status = {s: n for s, n in await self.session.execute(select(Lead.status, func.count()).where(*where).group_by(Lead.status))}
        return sum(by_status.values()), no_email, by_status

    async def summary(self, workspace_id: UUID) -> list[tuple[UUID | None, str, int]]:
        """How many leads each ICP has in each status (every lead, queued or not): the ICP pages' pipelines."""
        query = select(Lead.lead_intel_icp_id, Lead.status, func.count()).where(Lead.workspace_id == workspace_id)
        return list((await self.session.execute(query.group_by(Lead.lead_intel_icp_id, Lead.status))).all())

    async def email_taken(self, workspace_id: UUID, email: str) -> bool:
        """Whether a lead in this workspace already has this email (any case)."""
        query = select(exists().where(Lead.workspace_id == workspace_id, func.lower(Lead.email) == email.strip().lower()))
        return bool(await self.session.scalar(query))

    async def extra_keys(self, workspace_id: UUID) -> list[str]:
        """Every column name found in the leads' extra fields (prompt variables lead.extra.<column>)."""
        keys = func.jsonb_object_keys(Lead.extra)
        found = await self.session.scalars(select(keys).where(Lead.workspace_id == workspace_id, func.jsonb_typeof(Lead.extra) == "object").distinct())
        return sorted(found)

    async def post_counts(self, lead_ids: list[UUID]) -> dict[UUID, int]:
        """How many posts each of these leads has (their own, not their company's)."""
        if not lead_ids:
            return {}
        rows = await self.session.execute(
            select(Post.lead_id, func.count()).where(Post.lead_id.in_(lead_ids)).group_by(Post.lead_id)
        )
        return {lead_id: n for lead_id, n in rows}

    async def delete_many(self, workspace_id: UUID, lead_ids: list[UUID]) -> int:
        """Delete these leads with their posts, hooks, drafts and ratings (foreign keys cascade). The
        people they were made from stay on the People page, unlinked."""
        result = await self.session.execute(delete(Lead).where(Lead.workspace_id == workspace_id, Lead.id.in_(lead_ids)))
        await self.session.commit()
        return result.rowcount

    async def move_many(self, workspace_id: UUID, lead_ids: list[UUID], icp_id: UUID) -> int:
        """Put these leads in another ICP. Their posts and drafts stay; new drafts use the new ICP."""
        result = await self.session.execute(
            update(Lead).where(Lead.workspace_id == workspace_id, Lead.id.in_(lead_ids)).values(lead_intel_icp_id=icp_id)
        )
        await self.session.commit()
        return result.rowcount

    async def list(
        self, workspace_id: UUID, icp_id: UUID | None, status: str | None, q: str | None, limit: int, offset: int
    ) -> tuple[list[Lead], int]:
        where: list[Any] = [Lead.workspace_id == workspace_id]
        if icp_id:
            where.append(Lead.lead_intel_icp_id == icp_id)
        if status:
            where.append(Lead.status == status)
        if q:
            like = f"%{q}%"
            where.append(or_(Lead.email.ilike(like), Lead.company.ilike(like), _full_name.ilike(like)))
        total = (await self.session.execute(select(func.count()).select_from(Lead).where(*where))).scalar_one()
        rows = await self.session.scalars(
            select(Lead).where(*where).options(*_skip(Lead.score_reasons))
            .order_by(Lead.created_at.desc(), Lead.id).limit(limit).offset(offset)
        )  # fmt: skip
        return list(rows), total


class WorkspaceCompanyRepo:
    """Which Lead Intel companies are in a workspace. Reads and writes `outreach.eo_workspace_companies` only."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def workspace_exists(self, workspace_id: UUID) -> bool:
        return (await self.session.get(Workspace, workspace_id)) is not None

    async def imported(self, workspace_id: UUID) -> dict[UUID, datetime]:
        rows = await self.session.execute(
            select(WorkspaceCompany.lead_intel_business_id, WorkspaceCompany.imported_at).where(
                WorkspaceCompany.workspace_id == workspace_id
            )
        )
        return dict(rows.tuples().all())

    async def add(self, workspace_id: UUID, company_ids: list[UUID]) -> int:
        """Adds the ones not already there; returns how many were new."""
        if not company_ids:
            return 0
        statement = (
            insert(WorkspaceCompany)
            .values([{"workspace_id": workspace_id, "lead_intel_business_id": cid} for cid in dict.fromkeys(company_ids)])
            .on_conflict_do_nothing()
            .returning(WorkspaceCompany.lead_intel_business_id)
        )
        added = len((await self.session.execute(statement)).all())
        await self.session.commit()
        return added

    async def remove(self, workspace_id: UUID, company_ids: list[UUID]) -> int:
        statement = (
            delete(WorkspaceCompany)
            .where(WorkspaceCompany.workspace_id == workspace_id, WorkspaceCompany.lead_intel_business_id.in_(company_ids))
            .returning(WorkspaceCompany.lead_intel_business_id)
        )
        removed = len((await self.session.execute(statement)).all())
        await self.session.commit()
        return removed


class WorkspaceRepo:
    """Reads and writes `outreach.eo_workspaces` only.

    Deleting a workspace also deletes everything it owns in our `eo_` tables (their foreign keys
    cascade). Lead Intel is never touched: workspaces only ever point at it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self) -> list[Workspace]:
        return list(await self.session.scalars(select(Workspace).order_by(Workspace.created_at, Workspace.name)))

    async def get(self, workspace_id: UUID) -> Workspace | None:
        return await self.session.get(Workspace, workspace_id)

    async def name_taken(self, name: str, except_id: UUID | None = None) -> bool:
        query = select(Workspace.id).where(func.lower(Workspace.name) == name.lower())
        if except_id:
            query = query.where(Workspace.id != except_id)
        return (await self.session.scalar(query)) is not None

    async def create(self, name: str, description: str) -> Workspace:
        workspace = Workspace(name=name, description=description)
        self.session.add(workspace)
        await self.session.commit()
        await self.session.refresh(workspace)
        return workspace

    async def update(self, workspace: Workspace, values: dict[str, Any]) -> Workspace:
        for key, value in values.items():
            setattr(workspace, key, value)
        await self.session.commit()
        await self.session.refresh(workspace)
        return workspace

    async def delete(self, workspace: Workspace) -> None:
        await self.session.delete(workspace)
        await self.session.commit()


def person_keys(email: str | None, domain: str | None = None) -> list[tuple[str, str]]:
    """The exclusion keys a person matches: their email, its domain, and their company's domain."""
    keys: list[tuple[str, str]] = []
    if email and "@" in email:
        keys += [("email", email.strip().lower()), ("domain", email.split("@")[1].strip().lower())]
    if domain:
        keys.append(("domain", domain.strip().lower()))
    return keys


REASON_LABEL = {
    "current_client": "a current client", "past_client": "a past client", "do_not_contact": "asked not to be contacted",
    "competitor": "a competitor", "other": "on the list",
}  # fmt: skip


def exclusion_note(e: Exclusion) -> str:
    """'On the do-not-contact list: a current client (inkco.com)'."""
    return f"On the do-not-contact list: {REASON_LABEL.get(e.reason, e.reason)} ({e.key_value})"


async def excluded(session: AsyncSession, keys: list[tuple[str, str]]) -> Exclusion | None:
    """The do-not-contact entry any of these keys hits, if one does."""
    if not keys:
        return None
    query = select(Exclusion).where(tuple_(Exclusion.key_type, Exclusion.key_value).in_(keys)).limit(1)
    return await session.scalar(query)


class ExclusionRepo:
    """The do-not-contact list, `outreach.eo_exclusions`. Shared by every workspace."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self) -> list[Exclusion]:
        return list(await self.session.scalars(select(Exclusion).order_by(Exclusion.created_at.desc())))

    async def add(self, entries: list[dict[str, Any]], added_by: UUID | None) -> list[Exclusion]:
        """Add entries ({key_type, key_value, reason, business_name, note}); ones already listed are
        left as they are. Returns the new ones."""
        added = []
        for entry in entries:
            row = await self.session.scalar(
                insert(Exclusion).values(**entry, added_by=added_by).on_conflict_do_nothing().returning(Exclusion)
            )
            if row is not None:
                added.append(row)
        await self.session.commit()
        return added

    async def get(self, exclusion_id: UUID) -> Exclusion | None:
        return await self.session.get(Exclusion, exclusion_id)

    async def delete(self, exclusion: Exclusion) -> None:
        await self.session.delete(exclusion)
        await self.session.commit()

    async def matcher(self):
        """A function telling, from one read of the list, whether any of some keys is excluded."""
        rows = {(e.key_type, e.key_value): e for e in await self.list()}

        def hit(keys: list[tuple[str, str]]) -> Exclusion | None:
            return next((rows[k] for k in keys if k in rows), None)

        return hit

    async def match(self, keys: list[tuple[str, str]]) -> Exclusion | None:
        return await excluded(self.session, keys)


class SendingRepo:
    """Emails on their way through CheapSequencer: queueing the first email, tracking each lead's
    enrollment, and the follow-ups written for it (`outreach.eo_emails`)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def to_queue(self, workspace_id: UUID, email_ids: list[UUID]) -> list[tuple[Email, Lead]]:
        """These approved first emails, with their leads, that aren't queued yet."""
        query = (
            select(Email, Lead)
            .join(Lead, Lead.id == Email.lead_id)
            .where(Email.workspace_id == workspace_id, Email.id.in_(email_ids), Email.step == 1, Email.status == "approved")
        )
        return [(e, lead) for e, lead in await self.session.execute(query)]

    async def queued_before(self, addresses: list[str]) -> dict[str, datetime]:
        """Which of these addresses (lower case) were ever queued, and when."""
        if not addresses:
            return {}
        rows = await self.session.execute(select(QueuedAddress.email, QueuedAddress.queued_at).where(QueuedAddress.email.in_(addresses)))
        return dict(rows.all())

    async def claim(self, workspace_id: UUID, campaign_id: str, emails: list[tuple[Email, str]]) -> set[str]:
        """Record these addresses as queued, before they're sent to CheapSequencer. Returns the ones claimed now: an
        address another queue run claimed first is left out. Committed at once, so the claim holds."""
        if not emails:
            return set()
        values = [{"email": address, "workspace_id": workspace_id, "email_id": e.id, "campaign_id": campaign_id} for e, address in emails]
        claimed = await self.session.scalars(
            insert(QueuedAddress).values(values).on_conflict_do_nothing(index_elements=[QueuedAddress.email]).returning(QueuedAddress.email)
        )
        out = set(claimed)
        await self.session.commit()
        return out

    async def release(self, addresses: set[str]) -> None:
        """Give back claims whose import failed, so they can be queued once it works."""
        if addresses:
            await self.session.execute(delete(QueuedAddress).where(QueuedAddress.email.in_(addresses)))
            await self.session.commit()

    async def mark_queued(self, emails: list[Email], campaign_id: str, list_id: str) -> None:
        now = datetime.now(UTC)
        for e in emails:
            e.status, e.queued_at, e.sequencer_campaign_id, e.sequencer_list_id = "queued", now, campaign_id, list_id
            e.sequence_status = "queued"
        await self.session.commit()

    async def in_sequence(self, workspace_id: UUID) -> list[tuple[Email, Lead]]:
        """First emails queued into a campaign whose sequence hasn't ended: only the columns the follow-up check reads
        for each of them (it runs every half hour over every one). `load_full` reads the rest for the few that need it."""
        query = (
            select(Email, Lead)
            .join(Lead, Lead.id == Email.lead_id)
            .where(
                Email.workspace_id == workspace_id, Email.step == 1, Email.sequencer_campaign_id.is_not(None),
                or_(Email.sequence_status.is_(None), Email.sequence_status.in_(("queued", "active"))),
            )
            .options(
                load_only(Email.id, Email.workspace_id, Email.lead_id, Email.step, Email.status, Email.sent_at,
                          Email.sequencer_campaign_id, Email.sequencer_list_id, Email.sequencer_lead_id, Email.sequencer_inbox_id,
                          Email.sequencer_inbox_email, Email.sequence_status, Email.sequence_sent_count,
                          Email.sequence_stop_reason, Email.sequence_last_sent_at, Email.sequence_synced_at, raiseload=True),
                load_only(Lead.id, Lead.workspace_id, Lead.email, raiseload=True),
            )
        )  # fmt: skip
        return [(e, lead) for e, lead in await self.session.execute(query)]

    async def load_full(self, *rows: Any) -> None:
        """Read every column of rows `in_sequence` loaded only in part (keeping what was already changed on them)."""
        for row in rows:
            await self.session.refresh(row, attribute_names=[a.key for a in inspect(row).mapper.column_attrs
                                                             if a.key in inspect(row).unloaded])  # fmt: skip

    async def follow_ups(self, lead_ids: list[UUID]) -> dict[tuple[UUID, int], Email]:
        rows = await self.session.scalars(select(Email).where(Email.lead_id.in_(lead_ids), Email.step > 1))
        return {(e.lead_id, e.step): e for e in rows}

    async def add_follow_up(self, first: Email, step: int, body: str, version_id: UUID | None, confidence: int, cost: float) -> Email:
        row = Email(
            workspace_id=first.workspace_id, lead_id=first.lead_id, step=step, email_prompt_version_id=version_id,
            subject="", body=body, confidence=max(0, min(100, confidence)), status="queued", cost_usd=cost,
            queued_at=datetime.now(UTC), sequencer_campaign_id=first.sequencer_campaign_id,
            sequencer_list_id=first.sequencer_list_id,
        )  # fmt: skip
        self.session.add(row)
        await self.session.commit()
        return row

    async def first_email_for(self, campaign_id: str | None, lead_id: str | None = None, address: str | None = None) -> Email | None:
        """The first email a CheapSequencer lead was queued with: by its CheapSequencer lead id (known after the
        first follow-up check), or else by its address, in that campaign."""
        query = select(Email).where(Email.step == 1, Email.sequencer_campaign_id.is_not(None))
        if campaign_id:
            query = query.where(Email.sequencer_campaign_id == campaign_id)
        if address:
            query = query.join(Lead, Lead.id == Email.lead_id).where(func.lower(Lead.email) == address.lower())
        elif lead_id:
            query = query.where(Email.sequencer_lead_id == lead_id)
        else:
            return None
        return await self.session.scalar(query.order_by(Email.queued_at.desc().nulls_last()).limit(1))

    async def has_event(self, email_id: UUID, kind: str) -> bool:
        """Whether a webhook event of this kind (replied, bounced...) is already stored for this email."""
        return bool(await self.session.scalar(select(exists().where(SendEvent.email_id == email_id, SendEvent.type == kind))))

    async def lead(self, lead_id: UUID) -> Lead | None:
        return await self.session.get(Lead, lead_id)

    async def record_stop(self, email: Email, stop: Any, payload: dict[str, Any]) -> bool:
        """Store a webhook's stop once (by its event id) and stop the lead's sequence here. False if it was
        already stored: CheapSequencer delivers at least once."""
        statement = (
            insert(SendEvent)
            .values(
                workspace_id=email.workspace_id, email_id=email.id, lead_id=email.lead_id, type=stop.type,
                provider_event_id=stop.event_id, payload=payload, occurred_at=stop.occurred_at,
            )
            .on_conflict_do_nothing(index_elements=[SendEvent.provider_event_id])
            .returning(SendEvent.id)
        )  # fmt: skip
        added = await self.session.scalar(statement) is not None
        if added:
            email.sequence_stop_reason = email.sequence_stop_reason or stop.type
            email.sequencer_lead_id = email.sequencer_lead_id or stop.lead_id
        await self.session.commit()
        return added

    async def hooks(self, lead_id: UUID) -> list[Insight]:
        query = select(Insight).where(Insight.lead_id == lead_id).order_by(Insight.rank)
        return list(await self.session.scalars(query))

    async def commit(self) -> None:
        await self.session.commit()

    async def outbox(self, workspace_id: UUID, limit: int = 2000) -> list[tuple[Email, Lead]]:
        """Every first email Coldbrew put into a campaign, newest first."""
        query = (
            select(Email, Lead)
            .join(Lead, Lead.id == Email.lead_id)
            .where(Email.workspace_id == workspace_id, Email.step == 1, Email.sequencer_campaign_id.is_not(None))
            .order_by(Email.queued_at.desc().nulls_last())
            .limit(limit)
        )
        return [(e, lead) for e, lead in await self.session.execute(query)]

    async def leads_by_email(self, workspace_id: UUID, emails: list[str]) -> dict[str, Lead]:
        """The workspace's leads with these email addresses, by lowercased address."""
        if not emails:
            return {}
        rows = await self.session.scalars(
            select(Lead).where(Lead.workspace_id == workspace_id, func.lower(Lead.email).in_([e.lower() for e in emails]))
        )
        return {(lead.email or "").lower(): lead for lead in rows}

    async def queued_counts(self, workspace_id: UUID) -> dict[str, int]:
        """Leads queued into each CheapSequencer campaign."""
        query = (
            select(Email.sequencer_campaign_id, func.count())
            .where(Email.workspace_id == workspace_id, Email.step == 1, Email.sequencer_campaign_id.is_not(None))
            .group_by(Email.sequencer_campaign_id)
        )
        return {cid: n for cid, n in await self.session.execute(query)}


class UserRepo:
    """People who sign in, with their role and workspaces. `outreach.eo_users` and
    `eo_workspace_members` only: Supabase's own auth.users is never written from here."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def workspace_ids(self, user_id: UUID) -> list[UUID]:
        query = select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user_id)
        return list(await self.session.scalars(query))

    async def members(self) -> dict[UUID, list[UUID]]:
        """Each user's workspaces, for the user list."""
        out: dict[UUID, list[UUID]] = {}
        for user_id, workspace_id in await self.session.execute(select(WorkspaceMember.user_id, WorkspaceMember.workspace_id)):
            out.setdefault(user_id, []).append(workspace_id)
        return out

    async def list(self) -> list[User]:
        return list(await self.session.scalars(select(User).order_by(User.created_at, User.email)))

    async def get(self, user_id: UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def by_email(self, email: str) -> User | None:
        return await self.session.scalar(select(User).where(User.email == email.strip().lower()))

    async def for_sign_in(self, auth_user_id: UUID, email: str) -> User | None:
        """The user for a Supabase account, linking a row an admin added by email on its first sign-in. None
        for an account nobody added: Supabase sign-in is shared with Lead Intel, so having an account there
        isn't enough to get in here. The last-active time moves at most every 5 minutes."""
        user = await self.session.scalar(select(User).where(User.auth_user_id == auth_user_id))
        if user is None:
            user = await self.by_email(email)
            if user is None or user.auth_user_id is not None:
                return None
            user.auth_user_id = auth_user_id
        now = datetime.now(UTC)
        if user.status == "invited":
            user.status = "active"
        if user.last_active_at is None or (now - user.last_active_at).total_seconds() > 300:
            user.last_active_at = now
        if self.session.dirty or self.session.new:
            await self.session.commit()
            await self.session.refresh(user)
        return user

    async def create(self, name: str, email: str, role: str, workspace_ids: list[UUID], auth_user_id: UUID | None = None) -> User:
        user = User(name=name, email=email.strip().lower(), role=role, status="invited", auth_user_id=auth_user_id)
        self.session.add(user)
        await self.session.flush()
        await self._set_workspaces(user.id, workspace_ids)
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def update(self, user: User, values: dict[str, Any], workspace_ids: list[UUID] | None) -> User:
        for key, value in values.items():
            setattr(user, key, value)
        if workspace_ids is not None:
            await self._set_workspaces(user.id, workspace_ids)
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def delete(self, user: User) -> None:
        await self.session.delete(user)
        await self.session.commit()

    async def set_workspace_members(self, workspace_id: UUID, user_ids: list[UUID]) -> None:
        """Exactly these users are members of the workspace (admins open every workspace anyway)."""
        await self.session.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id))
        for user_id in dict.fromkeys(user_ids):
            self.session.add(WorkspaceMember(workspace_id=workspace_id, user_id=user_id))
        await self.session.commit()

    async def _set_workspaces(self, user_id: UUID, workspace_ids: list[UUID]) -> None:
        await self.session.execute(delete(WorkspaceMember).where(WorkspaceMember.user_id == user_id))
        for workspace_id in dict.fromkeys(workspace_ids):
            self.session.add(WorkspaceMember(workspace_id=workspace_id, user_id=user_id))


def _person_values(icp_id: UUID | None, person: Any, company: Company | None, origin: str) -> dict[str, Any]:
    """A typed-in or imported person (services.people_csv.PersonRow) as eo_stakeholders columns."""
    return {
        "first_name": person.first_name, "last_name": person.last_name, "title": person.title,
        "seniority": person.seniority or None, "linkedin_url": person.linkedin_url or None,
        "email": person.email or None, "email_status": person.email_status or None,
        "has_email": True if person.email else None,
        "organization_name": (company.name if company else None) or person.company or None,
        "company_id": company.id if company else None,
        "lead_intel_business_id": company.lead_intel_business_id if company else None,
        "lead_intel_icp_id": icp_id or (company.icp_id if company else None),
        "raw": {
            "added": origin, "company": person.company, "website": person.website, "phone": person.phone,
            "city": person.city, "state": person.state, "country": person.country,
            "company_linkedin_url": person.company_linkedin_url, "facebook_url": person.facebook_url,
            "twitter_url": person.twitter_url, "apollo_account_id": person.apollo_account_id, "extra": person.extra,
        },
    }  # fmt: skip


@dataclass
class PeopleFilters:
    """The People page's filters (components/people/people-view.tsx)."""

    archive: str = "active"
    q: str | None = None
    found_by: str | None = None
    status: str | None = None
    email: str | None = None
    linkedin: str | None = None
    post_state: str | None = None
    email_state: str | None = None
    company_text: str | None = None
    pipeline_company_id: UUID | None = None


class StakeholderRepo:
    """People found at a company, before and after their email is revealed. `outreach.eo_stakeholders`."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(
        self, workspace_id: UUID, company_id: UUID | None = None, icp_id: UUID | None = None, light: bool = False
    ) -> list[Stakeholder]:
        """With `light`, without the raw provider record (most of the row): what list pages show."""
        query = select(Stakeholder).where(Stakeholder.workspace_id == workspace_id)
        if light:
            query = query.options(*_skip(Stakeholder.raw))
        if company_id:
            query = query.where(Stakeholder.lead_intel_business_id == company_id)
        if icp_id:
            query = query.where(Stakeholder.lead_intel_icp_id == icp_id)
        return list(await self.session.scalars(query.order_by(Stakeholder.found_at.desc(), Stakeholder.id)))

    # Where they were found, as the People page shows it: the pipeline company's name, else the organisation's.
    COMPANY = func.coalesce(
        func.nullif(select(Company.name).where(Company.id == Stakeholder.company_id).scalar_subquery(), ""),
        func.nullif(Stakeholder.organization_name, ""), "—",
    )  # fmt: skip
    QUEUED = exists().where(Email.lead_id == Stakeholder.lead_id, Email.step == 1, Email.sequencer_campaign_id.is_not(None))
    POSTS = exists().where(Post.lead_id == Stakeholder.lead_id)
    ORDER = (Stakeholder.found_at.desc(), Stakeholder.id)

    @staticmethod
    def _base(workspace_id: UUID, f: PeopleFilters) -> list[Any]:
        out: list[Any] = [Stakeholder.workspace_id == workspace_id]
        if f.pipeline_company_id:
            out.append(Stakeholder.company_id == f.pipeline_company_id)
        if f.archive == "active":
            out.append(Stakeholder.archived.is_(False))
        elif f.archive == "archived":
            out.append(Stakeholder.archived.is_(True))
        return out

    def _matching(self, workspace_id: UUID, f: PeopleFilters) -> list[Any]:
        """Everything the People page filters on, as the browser did."""
        found = {
            "company": and_(Stakeholder.source == "apollo", Stakeholder.lead_intel_icp_id.is_(None)),
            "linkedin": Stakeholder.source == "apify_linkedin", "manual": Stakeholder.source == "manual",
        }  # fmt: skip
        found_by = None
        if f.found_by and f.found_by != "all":
            found_by = found.get(f.found_by)
            if found_by is None:
                try:
                    found_by = Stakeholder.lead_intel_icp_id == UUID(f.found_by)
                except ValueError:
                    found_by = literal(False)
        status = {"lead": self.QUEUED, "notlead": ~self.QUEUED, "revealed": Stakeholder.revealed_at.is_not(None),
                  "hidden": Stakeholder.revealed_at.is_(None)}.get(f.status or "all")  # fmt: skip
        verified = and_(func.coalesce(Stakeholder.email, "") != "", func.lower(Stakeholder.email_status).in_(("verified", "valid")))
        email = {"none": ~listing.has_text(Stakeholder.email), "has": listing.has_text(Stakeholder.email), "verified": verified}.get(f.email or "all")
        email_state = func.coalesce(func.nullif(Stakeholder.email_status, ""), "unknown")
        text = func.concat(Stakeholder.first_name, " ", Stakeholder.last_name, " ", Stakeholder.title, " ", self.COMPANY, " ",
                           func.coalesce(Stakeholder.email, ""))  # fmt: skip
        return [*self._base(workspace_id, f), *listing.where(
            listing.presence(f.linkedin, listing.has_text(Stakeholder.linkedin_url)),
            listing.presence(f.post_state, self.POSTS),
            listing.all_terms(f.company_text, self.COMPANY),
            email_state == f.email_state if f.email_state and f.email_state != "all" else None,
            found_by, status, email,
            listing.whole_text(f.q, text),
        )]  # fmt: skip

    async def page(self, workspace_id: UUID, f: PeopleFilters, limit: int, offset: int) -> tuple[list[tuple[Stakeholder, str]], int]:
        """One page of the People list with each person's company name, and how many match."""
        where = self._matching(workspace_id, f)
        total = (await self.session.execute(select(func.count()).select_from(Stakeholder).where(*where))).scalar_one()
        query = select(Stakeholder, self.COMPANY).where(*where).options(*_skip(Stakeholder.raw)).order_by(*self.ORDER).limit(limit).offset(offset)
        return [(s, name) for s, name in await self.session.execute(query)], total

    async def matching(self, workspace_id: UUID, f: PeopleFilters, cap: int) -> list[Any]:
        """Every matching person in list order, as the few fields selection and bulk actions read."""
        query = (
            select(Stakeholder.id, Stakeholder.email, Stakeholder.revealed_at, Stakeholder.linkedin_url, Stakeholder.lead_id)
            .where(*self._matching(workspace_id, f)).order_by(*self.ORDER).limit(cap)
        )  # fmt: skip
        return list((await self.session.execute(query)).all())

    async def base_total(self, workspace_id: UUID, f: PeopleFilters) -> int:
        return (await self.session.execute(select(func.count()).select_from(Stakeholder).where(*self._base(workspace_id, f)))).scalar_one()

    async def email_statuses(self, workspace_id: UUID, f: PeopleFilters) -> list[str]:
        """The verification statuses in the list (archive filter only): the Email verification filter's options."""
        state = func.coalesce(func.nullif(Stakeholder.email_status, ""), "unknown")
        found = await self.session.scalars(select(state).where(*self._base(workspace_id, f)).distinct())
        return sorted((s for s in found if s and s.strip()), key=lambda s: (s.lower(), s))

    async def post_counts(self, lead_ids: list[UUID]) -> dict[UUID, int]:
        return await LeadRepo(self.session).post_counts(lead_ids)

    async def lead_ids(self, workspace_id: UUID, ids: list[UUID]) -> dict[UUID, UUID | None]:
        query = select(Stakeholder.id, Stakeholder.lead_id).where(Stakeholder.workspace_id == workspace_id, Stakeholder.id.in_(ids))
        return {i: lead for i, lead in await self.session.execute(query)}

    async def get_many(self, workspace_id: UUID, ids: list[UUID]) -> list[Stakeholder]:
        query = select(Stakeholder).where(Stakeholder.workspace_id == workspace_id, Stakeholder.id.in_(ids))
        return list(await self.session.scalars(query))

    async def save_found(
        self,
        workspace_id: UUID,
        company_id: UUID | None,
        people: list[FoundPerson],
        icp_id: UUID | None = None,
        pipeline_company_id: UUID | None = None,
    ) -> int:
        """Add new search hits and refresh known ones; returns how many were new.

        A person who was already revealed keeps their full name and email: search results only
        carry a masked last name and must not overwrite what was paid for.
        """
        if not people:
            return 0
        known = set(
            await self.session.scalars(
                select(Stakeholder.external_id).where(
                    Stakeholder.workspace_id == workspace_id,
                    Stakeholder.source == "apollo",
                    Stakeholder.external_id.in_([p.apollo_id for p in people]),
                )
            )
        )
        for p in people:
            statement = insert(Stakeholder).values(
                workspace_id=workspace_id,
                lead_intel_business_id=company_id,
                company_id=pipeline_company_id,
                lead_intel_icp_id=icp_id,
                organization_name=p.organization_name,
                source="apollo",
                external_id=p.apollo_id,
                first_name=p.first_name,
                last_name=p.last_name_masked,
                title=p.title,
                has_email=p.has_email,
                raw=p.raw,
            )
            statement = statement.on_conflict_do_update(
                index_elements=[Stakeholder.workspace_id, Stakeholder.source, Stakeholder.external_id],
                set_={
                    "title": statement.excluded.title,
                    "has_email": statement.excluded.has_email,
                    # A search by ICP knows no Lead Intel company; it must not wipe one a company search set.
                    "lead_intel_business_id": func.coalesce(
                        statement.excluded.lead_intel_business_id, Stakeholder.lead_intel_business_id
                    ),
                    "lead_intel_icp_id": func.coalesce(Stakeholder.lead_intel_icp_id, statement.excluded.lead_intel_icp_id),
                    "organization_name": func.coalesce(statement.excluded.organization_name, Stakeholder.organization_name),
                    "company_id": func.coalesce(statement.excluded.company_id, Stakeholder.company_id),
                    "raw": case((Stakeholder.revealed_at.is_(None), statement.excluded.raw), else_=Stakeholder.raw),
                    "last_name": case(
                        (Stakeholder.revealed_at.is_(None), statement.excluded.last_name), else_=Stakeholder.last_name
                    ),
                },
            )
            await self.session.execute(statement)
        await self.session.commit()
        return sum(p.apollo_id not in known for p in people)

    async def save_employees(
        self,
        workspace_id: UUID,
        company_id: UUID | None,
        people: list[Employee],
        pipeline_company_id: UUID | None = None,
        icp_id: UUID | None = None,
    ) -> int:
        """Add people found on LinkedIn (via Apify) and refresh known ones; returns how many were new.

        Unlike an Apollo search these come with full names and a LinkedIn URL, and, from an email search, a work
        email. An email already known (revealed through Apollo, say) is kept; a lead made from the person that has
        no email yet gets the new one.
        """
        if not people:
            return 0
        known = set(
            await self.session.scalars(
                select(Stakeholder.external_id).where(
                    Stakeholder.workspace_id == workspace_id,
                    Stakeholder.source == "apify_linkedin",
                    Stakeholder.external_id.in_([p.external_id for p in people]),
                )
            )
        )
        for p in people:
            statement = insert(Stakeholder).values(
                workspace_id=workspace_id,
                lead_intel_business_id=company_id,
                company_id=pipeline_company_id,
                lead_intel_icp_id=icp_id,
                source="apify_linkedin",
                external_id=p.external_id,
                first_name=p.first_name,
                last_name=p.last_name,
                title=p.title,
                linkedin_url=p.linkedin_url,
                organization_name=p.company_name,
                raw=p.raw,
                email=p.email,
                email_status=p.email_status,
                has_email=True if p.email else None,
            )
            statement = statement.on_conflict_do_update(
                index_elements=[Stakeholder.workspace_id, Stakeholder.source, Stakeholder.external_id],
                set_={
                    "title": statement.excluded.title,
                    "linkedin_url": func.coalesce(statement.excluded.linkedin_url, Stakeholder.linkedin_url),
                    "organization_name": func.coalesce(statement.excluded.organization_name, Stakeholder.organization_name),
                    "lead_intel_business_id": func.coalesce(
                        statement.excluded.lead_intel_business_id, Stakeholder.lead_intel_business_id
                    ),
                    "company_id": func.coalesce(statement.excluded.company_id, Stakeholder.company_id),
                    "lead_intel_icp_id": func.coalesce(statement.excluded.lead_intel_icp_id, Stakeholder.lead_intel_icp_id),
                    # An email found now fills a gap, never replaces one already known (possibly paid for).
                    "email_status": case((Stakeholder.email.is_(None), statement.excluded.email_status), else_=Stakeholder.email_status),
                    "email": func.coalesce(Stakeholder.email, statement.excluded.email),
                    "has_email": func.coalesce(Stakeholder.has_email, statement.excluded.has_email),
                    # The full profile from an email search replaces a short one, never the other way round.
                    "raw": case((statement.excluded.raw.has_key("emails"), statement.excluded.raw), else_=Stakeholder.raw),
                },
            )
            await self.session.execute(statement)
        # People who are already leads, still without an email, get the one just found.
        found = [p.external_id for p in people if p.email]
        other_lead = aliased(Lead)
        if found:
            await self.session.execute(
                update(Lead)
                .where(
                    Lead.email.is_(None),
                    Lead.id == Stakeholder.lead_id,
                    Stakeholder.workspace_id == workspace_id,
                    Stakeholder.source == "apify_linkedin",
                    Stakeholder.external_id.in_(found),
                    Stakeholder.email.is_not(None),
                    # Never an email another lead in the workspace already has (one lead per address).
                    ~exists().where(
                        other_lead.workspace_id == Lead.workspace_id, func.lower(other_lead.email) == func.lower(Stakeholder.email)
                    ),
                )
                .values(email=Stakeholder.email, email_status=Stakeholder.email_status)
            )
        await self.session.commit()
        return sum(p.external_id not in known for p in people)

    async def save_person(
        self, workspace_id: UUID, icp_id: UUID | None, person: Any, company: Company | None, origin: str
    ) -> tuple[Stakeholder, bool]:
        """Add a person typed in or imported (services.people_csv.PersonRow), or update the one already
        stored under the same id. Returns (the person, whether they're new). A known email is never
        replaced by an empty one."""
        source, external_id = person.external_id
        values = _person_values(icp_id, person, company, origin)
        known = await self.session.scalar(
            select(Stakeholder).where(
                Stakeholder.workspace_id == workspace_id, Stakeholder.source == source, Stakeholder.external_id == external_id
            )
        )
        if known is None:
            row = Stakeholder(workspace_id=workspace_id, source=source, external_id=external_id,
                              revealed_at=func.now() if person.email else None, **values)  # fmt: skip
            self.session.add(row)
            await self.session.flush()
            return row, True
        # Blank cells never wipe what's stored; what the CSV adds is merged into the stored details.
        for key, value in values.items():
            if key == "raw":
                known.raw = {**(known.raw or {}), **value}
            elif value not in (None, ""):
                setattr(known, key, value)
        if person.email and known.revealed_at is None:
            known.revealed_at = func.now()
        return known, False

    async def queued_at(self, lead_ids: list[UUID]) -> dict[UUID, datetime]:
        """When each of these people's leads had its first email queued (see LeadRepo.queued_at)."""
        return await LeadRepo(self.session).queued_at(lead_ids)

    async def import_batch(
        self, workspace_id: UUID, icp_id: UUID | None, items: list[tuple[Any, Company | None]], origin: str,
        blocked: set[tuple[str, str]],
    ) -> tuple[int, int, int, list[UUID]]:  # fmt: skip
        """save_person and lead_from_stakeholder for many people in a few round trips (an import of 1,805 took an
        hour one at a time). Same rules: a known person is updated, blank cells never wipe what's stored, and
        anyone with an email or LinkedIn URL who isn't on the do-not-contact list (`blocked`) becomes a lead, or
        is linked to the lead already here with that email or LinkedIn URL. Leads go in first, so each person is
        saved with their lead already linked (one update per person made a batch take 30 seconds). Returns
        (added, updated, leads made, the people's ids)."""
        pairs = [person.external_id for person, _ in items]
        known = {
            (s.source, s.external_id): s
            for s in await self.session.scalars(
                select(Stakeholder).where(
                    Stakeholder.workspace_id == workspace_id, tuple_(Stakeholder.source, Stakeholder.external_id).in_(pairs)
                )
            )
        }
        prepared = []
        for person, company in items:
            values = _person_values(icp_id, person, company, origin)
            row = known.get(person.external_id)
            prepared.append((person, company, values, row))

        # 1. Leads, for people without one yet.
        want: list[tuple[int, LeadInput]] = []
        lead_rows: list[dict[str, Any]] = []
        for i, (person, company, values, row) in enumerate(prepared):
            if (row is not None and row.lead_id) or not (person.email or person.linkedin_url):
                continue
            def pick(key: str) -> Any:
                return values.get(key) if values.get(key) not in (None, "") or row is None else getattr(row, key)
            lead = clean_lead({
                "first_name": person.first_name, "last_name": person.last_name, "email": person.email,
                "title": person.title, "company": pick("organization_name") or person.company,
                "website": person.website or (company.website_url if company else "") or "", "linkedin_url": person.linkedin_url,
            })  # fmt: skip
            if any(key in blocked for key in person_keys(lead.email, lead.domain)):
                continue
            want.append((i, lead))
            lead_rows.append({
                "workspace_id": workspace_id, "lead_intel_icp_id": pick("lead_intel_icp_id"),
                "lead_intel_business_id": pick("lead_intel_business_id"), "first_name": lead.first_name, "last_name": lead.last_name,
                "email": lead.email or None, "email_status": pick("email_status"), "title": lead.title, "company": lead.company,
                "website": lead.website, "domain": lead.domain or None, "linkedin_url": lead.linkedin_url or None,
                "origin": origin, "external_id": person.external_id[1],
            })  # fmt: skip
        made = 0
        lead_for: dict[int, UUID | None] = {}
        if lead_rows:
            made = len((await self.session.execute(insert(Lead).values(lead_rows).on_conflict_do_nothing().returning(Lead.id))).all())
            # Every lead, made now or already here, by its email and LinkedIn URL: the unique indexes decided which.
            emails = {lead.email for _, lead in want if lead.email}
            profiles = {lead.linkedin_url.lower() for _, lead in want if lead.linkedin_url}
            found = await self.session.execute(
                select(Lead.id, func.lower(Lead.email), func.lower(Lead.linkedin_url)).where(
                    Lead.workspace_id == workspace_id,
                    or_(func.lower(Lead.email).in_(emails or {""}), func.lower(Lead.linkedin_url).in_(profiles or {""})),
                )
            )
            by_email, by_profile = {}, {}
            for lead_id, email, profile in found:
                if email:
                    by_email.setdefault(email, lead_id)
                if profile:
                    by_profile.setdefault(profile, lead_id)
            for i, lead in want:
                lead_for[i] = (by_email.get(lead.email) if lead.email else None) or (
                    by_profile.get(lead.linkedin_url.lower()) if lead.linkedin_url else None
                )

        # 2. The people, each with their lead.
        added = updated = 0
        rows: list[Stakeholder] = []
        for i, (person, company, values, row) in enumerate(prepared):
            source, external_id = person.external_id
            if row is None:
                row = Stakeholder(workspace_id=workspace_id, source=source, external_id=external_id, lead_id=lead_for.get(i),
                                  revealed_at=func.now() if person.email else None, **values)  # fmt: skip
                self.session.add(row)
                known[(source, external_id)] = row
                added += 1
            else:
                for key, value in values.items():
                    if key == "raw":
                        row.raw = {**(row.raw or {}), **value}
                    elif value not in (None, ""):
                        setattr(row, key, value)
                if person.email and row.revealed_at is None:
                    row.revealed_at = func.now()
                if lead_for.get(i) and not row.lead_id:
                    row.lead_id = lead_for[i]
                updated += 1
            rows.append(row)
        await self.session.flush()
        await self.session.commit()
        return added, updated, made, [row.id for row in rows]

    async def commit(self) -> None:
        await self.session.commit()

    async def link_lead(self, stakeholder: Stakeholder, lead_id: UUID) -> None:
        stakeholder.lead_id = lead_id
        await self.session.commit()

    async def save_revealed(self, stakeholder: Stakeholder, person: RevealedPerson | None, lead_id: UUID | None) -> None:
        stakeholder.revealed_at = func.now()
        if person is None:
            # Apollo found no match and charged nothing; remember that so nobody pays to ask again.
            stakeholder.email_status = "no_match"
        else:
            stakeholder.first_name = person.first_name or stakeholder.first_name
            stakeholder.last_name = person.last_name
            stakeholder.title = person.title or stakeholder.title
            stakeholder.seniority = person.seniority
            stakeholder.email = person.email
            stakeholder.email_status = person.email_status
            stakeholder.linkedin_url = person.linkedin_url
            stakeholder.raw = person.raw
        stakeholder.lead_id = lead_id
        await self.session.commit()
        await self.session.refresh(stakeholder)


class ProviderCallRepo:
    """The ledger of calls to paid providers. `outreach.eo_provider_calls`."""

    # Measured per lead rather than per call: one email for a lead is several Claude calls.
    PER_LEAD_ACTIONS = ("Write emails",)

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claude_per_use(self, workspace_id: UUID, since: datetime) -> list[dict[str, Any]]:
        """Average tokens and cost per use of each Claude operation since `since` (successful calls only)."""
        query = (
            select(
                ProviderCall.action, func.count(), func.count(func.distinct(ProviderCall.lead_id)),
                func.coalesce(func.sum(ProviderCall.input_tokens), 0), func.coalesce(func.sum(ProviderCall.output_tokens), 0),
                func.coalesce(func.sum(ProviderCall.cost_usd), 0),
            )
            .where(ProviderCall.workspace_id == workspace_id, ProviderCall.provider == "claude", ProviderCall.status == "succeeded",
                   ProviderCall.action.is_not(None), ProviderCall.called_at >= since)
            .group_by(ProviderCall.action)
        )  # fmt: skip
        out = []
        for action, calls, leads, tokens_in, tokens_out, cost in await self.session.execute(query):
            uses = leads if action in self.PER_LEAD_ACTIONS and leads else calls
            out.append({"action": action, "uses": uses, "tokens_in": round(tokens_in / uses), "tokens_out": round(tokens_out / uses),
                        "cost_usd": round(float(cost) / uses, 6)})  # fmt: skip
        return sorted(out, key=lambda r: r["action"])

    async def record(
        self,
        workspace_id: UUID,
        provider: str,
        endpoint: str,
        status: str,
        *,
        key: str | None = None,
        lead_id: UUID | None = None,
        credits: float | None = None,
        cost_usd: float | None = None,
        action: str | None = None,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        from background import current_job  # here: background imports this module

        job = current_job.get()
        await self.session.execute(
            insert(ProviderCall)
            .values(
                idempotency_key=key or f"{provider}:{endpoint}:{uuid4()}",
                workspace_id=workspace_id,
                # Made inside a background job: counted against it (a Quick Start adds up its jobs' spend).
                job_id=job.job_id if job else None,
                lead_id=lead_id,
                provider=provider,
                endpoint=endpoint,
                status=status,
                credits=credits,
                cost_usd=cost_usd or 0,
                action=action,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            .on_conflict_do_nothing()
        )
        await self.session.commit()

    async def used_this_month(self, provider: str) -> int:
        """Units (credits) logged for a provider since the 1st of this month (UTC), every workspace: Brave searches."""
        start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = await self.session.scalar(select(func.coalesce(func.sum(ProviderCall.credits), 0))
                                          .where(ProviderCall.provider == provider, ProviderCall.called_at >= start))  # fmt: skip
        return int(total or 0)

    async def spent_by_jobs(self, job_ids: list[UUID]) -> float:
        """What these jobs' paid calls cost, as logged (Apify's figures are corrected as they settle)."""
        if not job_ids:
            return 0.0
        total = await self.session.scalar(select(func.coalesce(func.sum(ProviderCall.cost_usd), 0)).where(ProviderCall.job_id.in_(job_ids)))
        return float(total or 0)

    async def apify_runs_since(self, since: datetime) -> list[ProviderCall]:
        """Apify runs logged since `since`, every workspace, whose cost may still be settling at Apify."""
        query = select(ProviderCall).where(
            ProviderCall.provider == "apify", ProviderCall.idempotency_key.like("apify:run:%"), ProviderCall.called_at >= since
        )
        return list(await self.session.scalars(query))

    async def usage(self, workspace_id: UUID, since: datetime, recent: int = 100) -> dict[str, Any]:
        """Spend since a date: totals per provider, per provider and action, and the latest calls."""
        where = (ProviderCall.workspace_id == workspace_id, ProviderCall.called_at >= since)
        sums = (
            func.count().label("calls"),
            func.count().filter(ProviderCall.status == "failed").label("failed"),
            func.coalesce(func.sum(ProviderCall.credits), 0).label("credits"),
            func.coalesce(func.sum(ProviderCall.cost_usd), 0).label("cost_usd"),
            func.coalesce(func.sum(ProviderCall.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(ProviderCall.output_tokens), 0).label("output_tokens"),
        )
        action = func.coalesce(ProviderCall.action, ProviderCall.endpoint, "Other").label("action")
        by_provider = await self.session.execute(
            select(ProviderCall.provider, *sums).where(*where).group_by(ProviderCall.provider)
        )
        by_action = await self.session.execute(
            select(ProviderCall.provider, action, *sums).where(*where).group_by(ProviderCall.provider, action)
        )
        latest = await self.session.scalars(
            select(ProviderCall).where(*where).order_by(ProviderCall.called_at.desc()).limit(recent)
        )
        return {
            "providers": [dict(r._mapping) for r in by_provider],
            "actions": [dict(r._mapping) for r in by_action],
            "recent": list(latest),
        }


async def lead_from_stakeholder(
    session: AsyncSession, workspace_id: UUID, icp_id: UUID | None, company_id: UUID | None, lead: LeadInput, apollo_id: str,
    email_status: str | None, origin: str = "apollo",
) -> tuple[UUID | None, bool]:  # fmt: skip
    """The lead for a revealed person: created, or the existing one with the same email or LinkedIn.

    Returns (lead id, whether it was created). The unique indexes decide what counts as the same person.
    Someone on the do-not-contact list doesn't become a lead: (None, False).
    """
    if await excluded(session, person_keys(lead.email, lead.domain)):
        return None, False
    statement = (
        insert(Lead)
        .values(
            workspace_id=workspace_id,
            lead_intel_icp_id=icp_id,
            lead_intel_business_id=company_id,
            first_name=lead.first_name,
            last_name=lead.last_name,
            email=lead.email or None,
            email_status=email_status,
            title=lead.title,
            company=lead.company,
            website=lead.website,
            domain=lead.domain or None,
            linkedin_url=lead.linkedin_url or None,
            origin=origin,
            external_id=apollo_id,
        )
        .on_conflict_do_nothing()
        .returning(Lead.id)
    )
    created = (await session.execute(statement)).scalar_one_or_none()
    await session.commit()
    if created:
        return created, True
    matches = []
    if lead.email:
        matches.append(func.lower(Lead.email) == lead.email)
    if lead.linkedin_url:
        matches.append(func.lower(Lead.linkedin_url) == lead.linkedin_url.lower())
    if not matches:
        return None, False
    existing = await session.scalar(select(Lead.id).where(Lead.workspace_id == workspace_id, or_(*matches)).limit(1))
    return existing, False


class PostRepo:
    """Leads' recent posts. Writes `outreach.eo_posts`, and the lead's status once its posts are in."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def leads(self, workspace_id: UUID, lead_ids: list[UUID]) -> list[Lead]:
        query = select(Lead).where(Lead.workspace_id == workspace_id, Lead.id.in_(lead_ids))
        return list(await self.session.scalars(query))

    async def max_age_days(self, workspace_id: UUID) -> int:
        """The longest post age limit in use: Settings', or an ICP's longer one. Nothing older is fetched or kept."""
        setting = await self.session.scalar(select(Settings.max_post_age_days).where(Settings.workspace_id == workspace_id))
        icp = await self.session.scalar(select(func.max(IcpProfile.max_post_age_days)).where(IcpProfile.workspace_id == workspace_id))
        return max(setting or GENERAL_DEFAULTS["max_post_age_days"], icp or 0)

    async def _recent(self, workspace_id: UUID, posts: list[FetchedPost]) -> list[FetchedPost]:
        """Only posts within the longest age limit in use (Settings', or an ICP's longer one), and none without a
        date: an older post is never stored, whichever path read it (2026-11-06: 2,705 posts over 60 days old had
        come in, mostly with a company's Instagram profile, and were written from)."""
        cutoff = datetime.now(UTC) - timedelta(days=await self.max_age_days(workspace_id))
        aware = lambda d: d if d.tzinfo else d.replace(tzinfo=UTC)  # noqa: E731
        return [p for p in posts if p.posted_at is not None and aware(p.posted_at) >= cutoff]

    async def save(self, workspace_id: UUID, lead_id: UUID, posts: list[FetchedPost]) -> int:
        """Store posts a lead doesn't have yet (same URL = same post), recent ones only; returns how many were new."""
        new = 0
        for p in await self._recent(workspace_id, posts):
            statement = (
                insert(Post)
                .values(
                    workspace_id=workspace_id,
                    lead_id=lead_id,
                    source=p.source,
                    url=p.url,
                    external_id=p.external_id,
                    posted_at=p.posted_at,
                    text=p.text,
                    media_type=p.media_type,
                    is_repost=p.is_repost,
                    raw=p.raw,
                )
                .on_conflict_do_nothing()
                .returning(Post.id)
            )
            new += (await self.session.execute(statement)).scalar_one_or_none() is not None
        await self.session.commit()
        return new

    async def set_status(self, lead: Lead, status: str) -> None:
        lead.status = status
        await self.session.commit()

    async def save_company(self, workspace_id: UUID, company_id: UUID, posts: list[FetchedPost]) -> int:
        """Store a pipeline company's own posts it doesn't have yet, recent ones only; returns how many were new."""
        new = 0
        for p in await self._recent(workspace_id, posts):
            statement = (
                insert(Post)
                .values(
                    workspace_id=workspace_id, company_id=company_id, source=p.source, url=p.url, external_id=p.external_id,
                    posted_at=p.posted_at, text=p.text, media_type=p.media_type, is_repost=p.is_repost, raw=p.raw,
                )
                .on_conflict_do_nothing()
                .returning(Post.id)
            )  # fmt: skip
            new += (await self.session.execute(statement)).scalar_one_or_none() is not None
        await self.session.commit()
        return new

    async def company_for(self, workspace_id: UUID, lead: Lead) -> Company | None:
        """The company the lead was found at (as email writing finds it)."""
        return await EmailRepo(self.session).company_for(workspace_id, lead)

    async def stored(self, workspace_id: UUID, lead_id: UUID | None, company_id: UUID | None, limit: int = 20) -> list[Post]:
        """The lead's own posts and their company's, newest first (a company on its own: lead_id None)."""
        owners = ([Post.lead_id == lead_id] if lead_id else []) + ([Post.company_id == company_id] if company_id else [])
        if not owners:
            return []
        query = select(Post).where(Post.workspace_id == workspace_id, or_(*owners))
        return list(await self.session.scalars(query.order_by(Post.posted_at.desc().nulls_last(), Post.id).limit(limit)))

    async def of_owner(self, workspace_id: UUID, owner: str, network: str) -> list[Post]:
        """The posts of this network stored for an owner ("lead:<id>" or "company:<id>"), newest first."""
        kind, _, raw_id = owner.partition(":")
        column = Post.lead_id if kind == "lead" else Post.company_id
        query = select(Post).where(Post.workspace_id == workspace_id, column == UUID(raw_id), Post.source == network)
        return list(await self.session.scalars(query.order_by(Post.posted_at.desc().nulls_last(), Post.id)))

    async def count_source(self, workspace_id: UUID, owner: str, network: str) -> int:
        """Posts of this network already stored for an owner ("lead:<id>" or "company:<id>")."""
        kind, _, raw_id = owner.partition(":")
        column = Post.lead_id if kind == "lead" else Post.company_id
        query = select(func.count()).where(Post.workspace_id == workspace_id, column == UUID(raw_id), Post.source == network)
        return int(await self.session.scalar(query) or 0)

    async def save_for(self, workspace_id: UUID, owner: str, posts: list[FetchedPost]) -> int:
        kind, _, raw_id = owner.partition(":")
        if kind == "lead":
            return await self.save(workspace_id, UUID(raw_id), posts)
        return await self.save_company(workspace_id, UUID(raw_id), posts)

    async def for_company(self, workspace_id: UUID, company_id: UUID) -> list[Post]:
        query = select(Post).where(Post.workspace_id == workspace_id, Post.company_id == company_id)
        return list(await self.session.scalars(query.order_by(Post.posted_at.desc().nulls_last(), Post.id)))

    async def list(self, workspace_id: UUID, lead_id: UUID | None) -> list[Post]:
        # Leads' posts only; a company's own posts are read with for_company. Without the raw provider record (most of
        # the row): the list never shows it.
        query = select(Post).where(Post.workspace_id == workspace_id, Post.lead_id.is_not(None)).options(*_skip(Post.raw))
        if lead_id:
            query = query.where(Post.lead_id == lead_id)
        return list(await self.session.scalars(query.order_by(Post.posted_at.desc().nulls_last(), Post.id)))


@dataclass
class CompanyFilters:
    """The Companies page's filters (components/companies/companies-view.tsx)."""

    icp_id: UUID | None = None
    archive: str = "active"
    status: str | None = None
    q: str | None = None
    source: str | None = None
    location: str | None = None
    website: str | None = None
    contacts: str | None = None
    score_band: str | None = None
    icp_state: str | None = None


class CompanyRepo:
    """The workspace's company pipeline. Reads and writes `outreach.eo_companies` and `eo_company_pages` only."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def workspace_exists(self, workspace_id: UUID) -> bool:
        return (await self.session.get(Workspace, workspace_id)) is not None

    @staticmethod
    def _base(workspace_id: UUID, f: "CompanyFilters") -> list[Any]:
        """The ICP and the archive filter: what the tab counts and the source options cover."""
        out: list[Any] = [Company.workspace_id == workspace_id]
        if f.icp_id:
            out.append(Company.icp_id == f.icp_id)
        if f.archive == "active":
            out.append(Company.archived.is_(False))
        elif f.archive == "archived":
            out.append(Company.archived.is_(True))
        return out

    @staticmethod
    def _people_at() -> Any:
        return exists().where(Stakeholder.company_id == Company.id)

    def _matching(self, workspace_id: UUID, f: "CompanyFilters") -> list[Any]:
        """Everything the page filters on, as the browser did: tab, source, location, website, people found, score,
        ICP assignment and the search (the whole query inside "name domain")."""
        website = case((Company.website_url != "", Company.website_url), else_=Company.domain)
        return [*self._base(workspace_id, f), *listing.where(
            Company.status == f.status if f.status and f.status != "all" else None,
            Company.source == f.source if f.source and f.source != "all" else None,
            listing.all_terms(f.location, Company.city, Company.state),
            listing.presence(f.website, listing.has_text(website)),
            listing.presence(f.contacts, self._people_at()),
            listing.score_band(f.score_band, Company.score),
            listing.presence(f.icp_state, Company.icp_id.is_not(None)),
            listing.whole_text(f.q, func.concat(Company.name, " ", func.coalesce(Company.domain, ""))),
        )]  # fmt: skip

    ORDER = (Company.score.desc().nulls_last(), Company.created_at.desc(), Company.id)

    async def page(self, workspace_id: UUID, f: "CompanyFilters", limit: int, offset: int) -> tuple[list[Company], int]:
        """One page of the Companies list (without research and the score and rule breakdowns), and how many match."""
        where = self._matching(workspace_id, f)
        total = (await self.session.execute(select(func.count()).select_from(Company).where(*where))).scalar_one()
        query = (
            select(Company).where(*where)
            .options(*_skip(Company.research, Company.raw, Company.score_detail, Company.rule_detail))
            .order_by(*self.ORDER).limit(limit).offset(offset)
        )  # fmt: skip
        return list(await self.session.scalars(query)), total

    async def matching(self, workspace_id: UUID, f: "CompanyFilters", cap: int) -> list[Any]:
        """Every matching company in list order, as the few fields selection and bulk actions read."""
        query = (
            select(Company.id, Company.status, Company.source, Company.researched_at, Company.name, Company.domain)
            .where(*self._matching(workspace_id, f)).order_by(*self.ORDER).limit(cap)
        )  # fmt: skip
        return list((await self.session.execute(query)).all())

    async def status_counts(self, workspace_id: UUID, f: "CompanyFilters") -> dict[str, int]:
        """Companies per status under the ICP and archive filter only (the tabs ignore search and facets)."""
        query = select(Company.status, func.count()).where(*self._base(workspace_id, f)).group_by(Company.status)
        return {s: n for s, n in await self.session.execute(query)}

    async def sources(self, workspace_id: UUID, f: "CompanyFilters") -> list[str]:
        query = select(Company.source).where(*self._base(workspace_id, f)).distinct()
        return sorted(s for s in await self.session.scalars(query) if s and s.strip())

    async def people_counts(self, company_ids: list[UUID]) -> dict[UUID, int]:
        """People found at each of these companies (archived people too, as before)."""
        if not company_ids:
            return {}
        query = select(Stakeholder.company_id, func.count()).where(Stakeholder.company_id.in_(company_ids)).group_by(Stakeholder.company_id)
        return {cid: n for cid, n in await self.session.execute(query)}

    async def lead_intel_ids(self, workspace_id: UUID) -> list[UUID]:
        """The Lead Intel businesses already in the pipeline (the import dialog marks them)."""
        query = select(Company.lead_intel_business_id).where(
            Company.workspace_id == workspace_id, Company.lead_intel_business_id.is_not(None)
        )
        return list(await self.session.scalars(query))

    async def get_many(self, workspace_id: UUID, ids: list[UUID]) -> list[Company]:
        query = select(Company).where(Company.workspace_id == workspace_id, Company.id.in_(ids))
        return list(await self.session.scalars(query))

    async def get(self, workspace_id: UUID, company_id: UUID) -> Company | None:
        return await self.session.scalar(select(Company).where(Company.workspace_id == workspace_id, Company.id == company_id))

    async def matcher(self, workspace_id: UUID):
        """A function finding the pipeline company for a domain or name, from one read of the pipeline.
        The domain is tried first; the name only when it's unique in the workspace."""
        rows = list(await self.session.scalars(select(Company).where(Company.workspace_id == workspace_id)))
        by_domain = {c.domain: c for c in rows if c.domain}
        names: dict[str, list[Company]] = {}
        for c in rows:
            names.setdefault(c.name.strip().lower(), []).append(c)

        def find(domain: str | None, name: str | None) -> Company | None:
            if domain and domain in by_domain:
                return by_domain[domain]
            same = names.get((name or "").strip().lower(), [])
            return same[0] if len(same) == 1 else None

        return find

    async def save_pages(self, workspace_id: UUID, company_id: UUID, via: str, pages: list[dict[str, Any]]) -> None:
        """Store the pages read from a company's website; a page read again replaces its old text."""
        for page in pages:
            values = {"role": page["role"], "via": via, "status": page.get("status"), "text": page.get("text") or ""}
            await self.session.execute(
                insert(CompanyPage)
                .values(workspace_id=workspace_id, company_id=company_id, url=page["url"], **values)
                .on_conflict_do_update(
                    index_elements=[CompanyPage.company_id, CompanyPage.url], set_={**values, "fetched_at": func.now()}
                )
            )
        await self.session.commit()

    async def brief_material(self, workspace_id: UUID, company: Company) -> dict[str, Any]:
        """What a company's brief is written from, besides the company itself: its website pages, its recent posts and
        its people's, and the people found there."""
        pages = list(await self.session.scalars(
            select(CompanyPage).where(CompanyPage.workspace_id == workspace_id, CompanyPage.company_id == company.id)
            .order_by(CompanyPage.fetched_at, CompanyPage.url).limit(4)
        ))  # fmt: skip
        people = list(await self.session.scalars(
            select(Stakeholder).options(*_skip(Stakeholder.raw))
            .where(Stakeholder.workspace_id == workspace_id, Stakeholder.company_id == company.id).limit(10)
        ))  # fmt: skip
        lead_ids = [p.lead_id for p in people if p.lead_id]
        owners = [Post.company_id == company.id] + ([Post.lead_id.in_(lead_ids)] if lead_ids else [])
        posts = list(await self.session.scalars(
            select(Post).options(*_skip(Post.raw)).where(Post.workspace_id == workspace_id, or_(*owners))
            .order_by(Post.posted_at.desc().nulls_last()).limit(12)
        ))  # fmt: skip
        return {
            "website_pages": [{"url": p.url, "text": (p.text or "")[:3000]} for p in pages],
            "posts": [{"source": p.source, "whose": "person" if p.lead_id else "company", "posted": p.posted_at.date().isoformat() if p.posted_at else None,
                       "text": (p.text or "")[:800], "url": p.url} for p in posts],  # fmt: skip
            "people": [{"name": f"{p.first_name} {p.last_name}".strip(), "title": p.title, "email": bool(p.email), "linkedin": p.linkedin_url} for p in people],
        }

    async def pages(self, workspace_id: UUID, company_id: UUID) -> list[CompanyPage]:
        query = select(CompanyPage).where(CompanyPage.workspace_id == workspace_id, CompanyPage.company_id == company_id)
        return list(await self.session.scalars(query.order_by(CompanyPage.fetched_at, CompanyPage.url)))

    async def list(self, workspace_id: UUID, icp_id: UUID | None, status: str | None, light: bool = False) -> list[Company]:
        """With `light`, without research, raw provider data and the score and rule breakdowns: what list pages show."""
        query = select(Company).where(Company.workspace_id == workspace_id)
        if light:
            query = query.options(*_skip(Company.research, Company.raw, Company.score_detail, Company.rule_detail))
        if icp_id:
            query = query.where(Company.icp_id == icp_id)
        if status:
            query = query.where(Company.status == status)
        query = query.order_by(Company.score.desc().nulls_last(), Company.created_at.desc(), Company.id)
        return list(await self.session.scalars(query))

    async def _by_keys(self, workspace_id: UUID) -> tuple[dict[UUID, Company], dict[str, Company], dict[str, Company]]:
        rows = await self.session.scalars(select(Company).where(Company.workspace_id == workspace_id))
        by_business: dict[UUID, Company] = {}
        by_apollo: dict[str, Company] = {}
        by_domain: dict[str, Company] = {}
        for row in rows:
            if row.lead_intel_business_id:
                by_business[row.lead_intel_business_id] = row
            if row.apollo_org_id:
                by_apollo[row.apollo_org_id] = row
            if row.domain:
                by_domain[row.domain] = row
        return by_business, by_apollo, by_domain

    async def add_lead_intel(self, workspace_id: UUID, icp_id: UUID, companies: list[dict[str, Any]]) -> tuple[int, int, int]:
        """Add Lead Intel companies, qualified straight away (Lead Intel already verified them).

        Returns (added, upgraded, already_had). A company already here from Apollo with the same
        domain becomes the Lead Intel one rather than a second row.
        """
        by_business, _, by_domain = await self._by_keys(workspace_id)
        added = upgraded = had = 0
        for c in companies:
            if c["id"] in by_business:
                had += 1
                continue
            existing = by_domain.get(c["domain"]) if c.get("domain") else None
            if existing is not None:
                existing.source = "lead_intel"
                existing.lead_intel_business_id = c["id"]
                existing.instagram_url = existing.instagram_url or c.get("instagram_url")
                if not existing.overridden:
                    existing.status = "qualified"
                    existing.status_reason = "In Lead Intel, which already verified it"
                by_business[c["id"]] = existing
                upgraded += 1
                continue
            row = Company(
                workspace_id=workspace_id,
                icp_id=icp_id,
                source="lead_intel",
                lead_intel_business_id=c["id"],
                name=c["name"],
                domain=c.get("domain"),
                website_url=c.get("website_url"),
                instagram_url=c.get("instagram_url"),
                city=c.get("city"),
                state=c.get("state"),
                status="qualified",
                status_reason="In Lead Intel, which already verified it",
            )
            self.session.add(row)
            if row.domain:
                by_domain[row.domain] = row
            by_business[c["id"]] = row
            added += 1
        await self.session.commit()
        return added, upgraded, had

    async def add_apollo(self, workspace_id: UUID, icp_id: UUID, orgs: list[ApolloOrganization]) -> tuple[int, int]:
        """Add Apollo companies to be researched. Returns (added, duplicates): the same Apollo id,
        or the same domain as a company already here from either source, is a duplicate."""
        _, by_apollo, by_domain = await self._by_keys(workspace_id)
        added = dupes = 0
        for org in orgs:
            if org.apollo_id in by_apollo or (org.domain and org.domain in by_domain):
                dupes += 1
                continue
            row = Company(
                workspace_id=workspace_id,
                icp_id=icp_id,
                source="apollo",
                apollo_org_id=org.apollo_id,
                name=org.name,
                domain=org.domain,
                website_url=org.website_url,
                linkedin_url=org.linkedin_url,
                facebook_url=org.facebook_url,
                twitter_url=org.twitter_url,
                status="found",
                raw=org.raw,
            )
            self.session.add(row)
            by_apollo[org.apollo_id] = row
            if org.domain:
                by_domain[org.domain] = row
            added += 1
        await self.session.commit()
        return added, dupes

    async def csv_company_adder(self, workspace_id: UUID, icp_id: UUID | None):
        """A function adding the company of a person from an Apollo export (one with an Apollo Account Id), so CSV
        companies reach the Companies page and smart lists. A company already here (same Apollo account or domain)
        is returned, not added twice. Each one added is committed with the person."""
        _, by_apollo, by_domain = await self._by_keys(workspace_id)

        def add(person: Any, domain: str | None) -> Company | None:
            account = person.apollo_account_id
            if not account or not person.company:
                return None
            if found := by_apollo.get(account) or (by_domain.get(domain) if domain else None):
                return found
            extra = person.extra or {}
            row = Company(
                workspace_id=workspace_id, icp_id=icp_id, source="apollo", apollo_org_id=account, name=person.company,
                domain=domain, website_url=person.website or None, linkedin_url=person.company_linkedin_url or None,
                facebook_url=person.facebook_url or None, twitter_url=person.twitter_url or None,
                city=extra.get("Company City") or person.city or None, state=extra.get("Company State") or person.state or None,
                phone=extra.get("Company Phone") or None, status="qualified",
                status_reason="Imported from Apollo, already verified",
                raw={"added": "csv", "industry": extra.get("Industry") or None, "keywords": extra.get("Keywords") or None,
                     "employees": extra.get("# Employees") or None, "address": extra.get("Company Address") or None,
                     "country": extra.get("Company Country") or person.country or None},
            )  # fmt: skip
            self.session.add(row)
            by_apollo[account] = row
            if domain:
                by_domain[domain] = row
            return row

        return add

    async def _identity_keys(self, workspace_id: UUID) -> set[tuple[str, str]]:
        """Every identity key of the workspace's companies (place id, domain, phone, name and place, LinkedIn page),
        read as those few columns only."""
        rows = await self.session.execute(
            select(Company.google_place_id, Company.domain, Company.phone, Company.name, Company.city, Company.state, Company.linkedin_url)
            .where(Company.workspace_id == workspace_id)
        )  # fmt: skip
        known: set[tuple[str, str]] = set()
        for place_id, domain, phone, name, city, state, linkedin_url in rows:
            known |= set(identity_keys(place_id=place_id, domain=domain, phone=phone, name=name, city=city, state=state))
            if slug := linkedin_slug(linkedin_url):
                known.add(("linkedin", slug))
        return known

    async def add_linkedin(self, workspace_id: UUID, icp_id: UUID, found: list[Any]) -> tuple[list[UUID], int]:
        """Add companies from LinkedIn's company search, to be checked. Returns (the new ids, duplicates). A duplicate
        shares any identity key with a company already here, from any source, or the same LinkedIn page."""
        known = await self._identity_keys(workspace_id)
        new_ids: list[UUID] = []
        dupes = 0
        for c in found:
            domain = normalize_domain(c.website_url) if c.website_url else None
            keys = set(identity_keys(domain=domain, name=c.name, city=c.city, state=c.state))
            if slug := linkedin_slug(c.linkedin_url):
                keys.add(("linkedin", slug))
            if known & keys:
                dupes += 1
                continue
            row = Company(
                workspace_id=workspace_id, icp_id=icp_id, source="linkedin", name=c.name, domain=domain, website_url=c.website_url,
                linkedin_url=c.linkedin_url, city=c.city, state=c.state, status="found",
                raw={"country": c.country, "employees": c.employees, "industry": c.industry, "description": c.description,
                     "linkedin": {k: c.raw.get(k) for k in ("followerCount", "employeeCountRange", "specialities", "foundedOn", "tagline") if c.raw.get(k)}},
            )  # fmt: skip
            self.session.add(row)
            await self.session.flush()
            new_ids.append(row.id)
            known |= keys
        await self.session.commit()
        return new_ids, dupes

    async def add_google_maps(self, workspace_id: UUID, icp_id: UUID, places: list[tuple[Place, str | None]]) -> tuple[int, int]:
        """Add Google Maps businesses to be researched, each with its website domain (or None).
        Returns (added, duplicates). A duplicate shares any identity key with a company already here,
        from any source: place id, domain, phone, or name and city (Lead Intel's keys)."""
        known = await self._identity_keys(workspace_id)
        added = dupes = 0
        for place, domain in places:
            keys = identity_keys(place_id=place.place_id, domain=domain, phone=place.phone, name=place.name, city=place.city, state=place.state)
            if known.intersection(keys):
                dupes += 1
                continue
            row = Company(
                workspace_id=workspace_id,
                icp_id=icp_id,
                source="google_maps",
                google_place_id=place.place_id,
                name=place.name,
                domain=domain,
                website_url=place.website_url,
                phone=place.phone,
                city=place.city,
                state=place.state,
                status="found",
                raw={
                    "address": place.address, "rating": place.rating, "review_count": place.review_count,
                    "business_status": place.business_status, "primary_type": place.primary_type, "country": place.country,
                },
            )  # fmt: skip
            self.session.add(row)
            known |= set(keys)
            added += 1
        await self.session.commit()
        return added, dupes

    async def save_all(self, rows: list[Company]) -> None:
        self.session.add_all(rows)
        await self.session.commit()

    async def save(self, company: Company) -> None:
        await self.session.commit()
        await self.session.refresh(company)


# Feedback tags as the Feedback page labels them (components/feedback/feedback-control.tsx): its search matches them.
TAG_LABEL = {
    "strong_hook": "Strong hook", "right_tone": "Right tone", "clear_cta": "Clear ask", "send_as_is": "Would send as is",
    "too_long": "Too long", "too_generic": "Too generic", "wrong_hook": "Wrong hook", "tone_off": "Tone off",
    "made_up_facts": "Made-up facts", "weak_cta": "Weak ask", "wrong_person": "Wrong person",
}  # fmt: skip
SCORED = ("draft", "edited", "approved", "queued")  # the lead's score belongs to its current draft only


@dataclass
class EmailFilters:
    """Review's and Feedback's filters (components/review/drafts-view.tsx, components/feedback/feedback-view.tsx).
    `icp_id` scopes to an ICP's route; `icp` is the ICP filter on the page. `search` is Review's (the whole query in
    any one field), `terms` Feedback's (every word somewhere in the email, its rating, its tags or its ICP's name)."""

    statuses: list[str] | None = None
    lead_id: UUID | None = None
    icp_id: str | None = None
    icp: str | None = None
    search: str | None = None
    terms: str | None = None
    score_band: str | None = None
    hook: str | None = None
    flags: str | None = None
    rating: str | None = None  # Review's rating filter (good, bad or unrated)
    rated: str | None = None  # Feedback's tab (good, bad or unrated)
    written: str | None = None
    tag: str | None = None
    sort: str = "newest"
    icp_names: dict[UUID, str] | None = None


class EmailRepo:
    """Drafted emails and the ranked points behind them. `outreach.eo_emails`, `eo_insights`, and the lead's status."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def leads(self, workspace_id: UUID, lead_ids: list[UUID]) -> list[Lead]:
        return list(await self.session.scalars(select(Lead).where(Lead.workspace_id == workspace_id, Lead.id.in_(lead_ids))))

    async def in_sequence(self, workspace_id: UUID, lead_ids: list[UUID]) -> set[UUID]:
        """Which of these leads already have an email queued or sent, so they aren't written to again."""
        query = select(Email.lead_id).where(
            Email.workspace_id == workspace_id, Email.lead_id.in_(lead_ids), Email.status.in_(("queued", "sent"))
        )
        return set(await self.session.scalars(query))

    async def company_for(self, workspace_id: UUID, lead: Lead) -> Company | None:
        """The pipeline company the lead was found at, else the one matching their Lead Intel company."""
        company = await self.session.scalar(
            select(Company)
            .join(Stakeholder, Stakeholder.company_id == Company.id)
            .where(Stakeholder.workspace_id == workspace_id, Stakeholder.lead_id == lead.id)
            .limit(1)
        )
        if company is None and lead.lead_intel_business_id:
            company = await self.session.scalar(
                select(Company).where(
                    Company.workspace_id == workspace_id, Company.lead_intel_business_id == lead.lead_intel_business_id
                )
            )
        return company

    async def posts_for(self, workspace_id: UUID, lead: Lead, company: Company | None, limit: int, since: datetime | None = None) -> list[Post]:
        """The lead's own posts and their company's, newest first. With `since`, only posts from then on: an older
        post stored from an earlier read is never written from (2026-11-06: 126 emails opened on posts up to a year
        old), and nor is one with no date."""
        owners = [Post.lead_id == lead.id] + ([Post.company_id == company.id] if company else [])
        query = (
            select(Post)
            .where(Post.workspace_id == workspace_id, or_(*owners), *([Post.posted_at >= since] if since else []))
            .order_by(Post.posted_at.desc().nulls_last(), Post.id)
            .limit(limit)
        )
        return list(await self.session.scalars(query))

    async def person_for(self, workspace_id: UUID, lead: Lead) -> Stakeholder | None:
        """The person record (Apollo, LinkedIn or added by hand) this lead was made from, if any."""
        return await self.session.scalar(
            select(Stakeholder).where(Stakeholder.workspace_id == workspace_id, Stakeholder.lead_id == lead.id).limit(1)
        )

    async def pages_for(self, company: Company, limit: int = 6) -> list[CompanyPage]:
        """The website pages research read for this company, in the order they were read."""
        query = (
            select(CompanyPage)
            .where(CompanyPage.company_id == company.id)
            .order_by(CompanyPage.fetched_at, CompanyPage.url)
            .limit(limit)
        )
        return list(await self.session.scalars(query))

    async def save_draft(
        self,
        workspace_id: UUID,
        lead: Lead,
        *,
        points: list[dict[str, Any]],
        email: dict[str, Any],
        score: dict[str, Any] | None = None,
    ) -> Email:
        """Replace the lead's points and unsent draft with new ones; the lead becomes 'generated'.
        With `score` (score, reasons, prompt_version_id), the lead keeps the draft's email score."""
        await self.session.execute(delete(Insight).where(Insight.lead_id == lead.id))
        for rank, point in enumerate(points, 1):
            self.session.add(Insight(workspace_id=workspace_id, lead_id=lead.id, rank=rank, **point))
        await self.session.execute(delete(Email).where(Email.lead_id == lead.id, Email.status.in_(("draft", "edited"))))
        row = Email(workspace_id=workspace_id, lead_id=lead.id, status="draft", **email)
        self.session.add(row)
        lead.status = "generated"
        lead.score = score["score"] if score else None
        lead.score_reasons = score["reasons"] if score else None
        lead.score_prompt_version_id = score["prompt_version_id"] if score else None
        lead.scored_at = datetime.now(UTC) if score else None
        await self.session.commit()
        await self.session.refresh(row)
        return row

    @staticmethod
    def _first_emails(workspace_id: UUID, statuses: list[str] | None, lead_id: UUID | None, icp_id: str | None) -> list[Any]:
        where: list[Any] = [Email.workspace_id == workspace_id, Email.step == 1]
        if statuses:
            where.append(Email.status.in_(statuses))
        if lead_id:
            where.append(Email.lead_id == lead_id)
        if icp_id == "none":
            where.append(Lead.lead_intel_icp_id.is_(None))
        elif icp_id:
            where.append(Lead.lead_intel_icp_id == UUID(icp_id))
        return where

    async def list(
        self, workspace_id: UUID, status: str | None = None, lead_id: UUID | None = None, *,
        statuses: list[str] | None = None, icp_id: str | None = None, limit: int | None = None, offset: int = 0,
    ) -> list[tuple[Email, Lead]]:
        """First emails only: follow-ups are written and queued on their own, never reviewed here. Newest first.
        `icp_id` "none" is the leads with no ICP."""
        where = self._first_emails(workspace_id, ([status] if status else []) + (statuses or []), lead_id, icp_id)
        query = select(Email, Lead).join(Lead, Lead.id == Email.lead_id).where(*where).order_by(Email.created_at.desc(), Email.id)
        if limit is not None:
            query = query.limit(limit).offset(offset)
        return [(e, lead) for e, lead in await self.session.execute(query)]

    async def count(self, workspace_id: UUID, statuses: list[str] | None, lead_id: UUID | None, icp_id: str | None) -> int:
        where = self._first_emails(workspace_id, statuses, lead_id, icp_id)
        return (await self.session.execute(select(func.count()).select_from(Email).join(Lead, Lead.id == Email.lead_id).where(*where))).scalar_one()

    SCORE = case((Email.status.in_(SCORED), Lead.score), else_=None)
    BODY = func.coalesce(Email.edited_body, Email.body)

    def _filtered(self, workspace_id: UUID, f: EmailFilters, now: datetime, statuses: bool = True, rating: bool = True) -> Any:
        """First emails with their lead and rating, filtered as Review and Feedback did (`statuses` and `rating` off
        for the tab counts: Review's status tabs and Feedback's rating tabs, each under all the other filters)."""
        name = func.coalesce(func.nullif(func.trim(func.concat(Lead.first_name, " ", Lead.last_name)), ""), func.nullif(Lead.email, ""),
                             "Unnamed lead")  # fmt: skip
        conditions = self._first_emails(workspace_id, f.statuses if statuses else None, f.lead_id, f.icp_id)
        if f.icp and f.icp != "all":
            conditions.append(Lead.lead_intel_icp_id.is_(None) if f.icp == "none" else Lead.lead_intel_icp_id == UUID(f.icp))
        hook = {"yes": listing.has_text(Email.hook), "no": ~listing.has_text(Email.hook),
                "with": func.coalesce(Email.hook, "") != "", "without": func.coalesce(Email.hook, "") == ""}.get(f.hook or "all")  # fmt: skip
        rated = {"good": EmailFeedback.rating == "good", "bad": EmailFeedback.rating == "bad", "unrated": EmailFeedback.id.is_(None)}
        conditions += listing.where(
            listing.score_band(f.score_band, self.SCORE), hook,
            listing.presence(f.flags, func.cardinality(Email.flags) > 0),
            rated.get(f.rating, literal(False)) if f.rating and f.rating != "all" else None,
            rated.get(f.rated, literal(False)) if rating and f.rated and f.rated != "all" else None,
            listing.recent(f.written, Email.created_at, now),
            EmailFeedback.tags.any(f.tag) if f.tag and f.tag != "all" else None,
            listing.any_field(f.search, name, Lead.company, Lead.title, Lead.email, Email.subject, self.BODY),
            self._terms(f),
        )  # fmt: skip
        return (
            select(Email, Lead).join(Lead, Lead.id == Email.lead_id)
            .outerjoin(EmailFeedback, EmailFeedback.email_id == Email.id).where(*conditions)
        )  # fmt: skip

    def _terms(self, f: EmailFilters) -> Any | None:
        """Feedback's search: every word in the subject, body, lead, hook, rating note, tag labels or ICP name."""
        words = listing.terms(f.terms)
        if not words:
            return None
        names = f.icp_names or {}
        fields = (Email.subject, self.BODY, Lead.first_name, Lead.last_name, Lead.email, Lead.title, Lead.company, Email.hook, EmailFeedback.note)
        each = []
        for word in words:
            tags = [t for t, label in TAG_LABEL.items() if word in label.lower()]
            icps = [i for i, n in names.items() if word in (n or "").lower()]
            each.append(or_(
                *[listing.any_field(word, field) for field in fields],
                *([EmailFeedback.tags.op("&&")(cast(tags, PG_ARRAY(Text)))] if tags else []),
                *([Lead.lead_intel_icp_id.in_(icps)] if icps else []),
                *([Lead.lead_intel_icp_id.is_(None)] if word in "no icp" else []),
            ))  # fmt: skip
        return and_(*each)

    def _order(self, f: EmailFilters) -> tuple[Any, ...]:
        """Newest first (Review always); Feedback can also sort oldest first, by score (unscored last) or by when it
        was rated (unrated last). Ties keep the newest-first order, as the browser's stable sort did."""
        newest = (Email.created_at.desc(), Email.id)
        return {
            "oldest": (Email.created_at.asc(), Email.id),
            "score-low": (self.SCORE.asc().nulls_last(), *newest),
            "score-high": (self.SCORE.desc().nulls_last(), *newest),
            "rated": (EmailFeedback.updated_at.desc().nulls_last(), *newest),
        }.get(f.sort, newest)

    async def page(self, workspace_id: UUID, f: EmailFilters, limit: int, offset: int, now: datetime) -> tuple[list[tuple[Email, Lead]], int]:
        """One page of first emails in the page's order, and how many match."""
        query = self._filtered(workspace_id, f, now)
        total = (await self.session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
        rows = await self.session.execute(query.order_by(*self._order(f)).limit(limit).offset(offset))
        return [(e, lead) for e, lead in rows], total

    async def ids(self, workspace_id: UUID, f: EmailFilters, now: datetime, cap: int) -> list[UUID]:
        """Every matching email's id in the page's order (the drawer's previous and next)."""
        query = self._filtered(workspace_id, f, now).with_only_columns(Email.id).order_by(*self._order(f)).limit(cap)
        return list(await self.session.scalars(query))

    async def brief(self, workspace_id: UUID, f: EmailFilters, now: datetime, cap: int) -> list[Any]:
        """Matching emails as id, status, the lead's name and email status (Queue all approved)."""
        query = self._filtered(workspace_id, f, now).with_only_columns(
            Email.id, Email.status, Lead.first_name, Lead.last_name, Lead.email, Lead.email_status
        ).order_by(*self._order(f)).limit(cap)  # fmt: skip
        return list((await self.session.execute(query)).all())

    async def breakdown(self, workspace_id: UUID, f: EmailFilters, now: datetime) -> tuple[dict[str, int], dict[str, int]]:
        """Under the same search and filters: how many emails are in each status (Review's tabs) and how many are
        unrated, good or bad (Feedback's tabs)."""
        sub = self._filtered(workspace_id, f, now, statuses=False, rating=False).with_only_columns(
            Email.status.label("status"), func.coalesce(EmailFeedback.rating, "unrated").label("rating")
        ).subquery()  # fmt: skip
        by_status: dict[str, int] = {}
        by_rating: dict[str, int] = {}
        for status, rating, n in await self.session.execute(select(sub.c.status, sub.c.rating, func.count()).group_by(sub.c.status, sub.c.rating)):
            by_status[status] = by_status.get(status, 0) + n
            by_rating[rating] = by_rating.get(rating, 0) + n
        return by_status, by_rating

    async def rated(self, workspace_id: UUID, icp_id: str | None) -> tuple[int, int]:
        """How many first emails there are (in an ICP, or all) and how many of them are rated."""
        where = self._first_emails(workspace_id, None, None, icp_id)
        query = (
            select(func.count(), func.count(EmailFeedback.id)).select_from(Email).join(Lead, Lead.id == Email.lead_id)
            .outerjoin(EmailFeedback, EmailFeedback.email_id == Email.id).where(*where)
        )  # fmt: skip
        total, rated = (await self.session.execute(query)).one()
        return total, rated

    async def counts(self, workspace_id: UUID, icp_id: str | None = None) -> dict[str, int]:
        """How many first emails are in each status: one small row per status, not the emails."""
        where = self._first_emails(workspace_id, None, None, icp_id)
        query = select(Email.status, func.count()).join(Lead, Lead.id == Email.lead_id).where(*where).group_by(Email.status)
        return {s: n for s, n in await self.session.execute(query)}

    async def get(self, workspace_id: UUID, email_id: UUID) -> tuple[Email, Lead] | None:
        row = (
            await self.session.execute(
                select(Email, Lead).join(Lead, Lead.id == Email.lead_id).where(
                    Email.workspace_id == workspace_id, Email.id == email_id
                )
            )
        ).first()
        return (row[0], row[1]) if row else None

    async def insights(self, lead_ids: list[UUID]) -> dict[UUID, list[Insight]]:
        out: dict[UUID, list[Insight]] = {}
        if lead_ids:
            rows = await self.session.scalars(select(Insight).where(Insight.lead_id.in_(lead_ids)).order_by(Insight.rank))
            for i in rows:
                out.setdefault(i.lead_id, []).append(i)
        return out

    async def posts(self, post_ids: list[UUID]) -> dict[UUID, Post]:
        if not post_ids:
            return {}
        return {p.id: p for p in await self.session.scalars(select(Post).where(Post.id.in_(post_ids)).options(*_skip(Post.raw)))}

    async def commit_refresh(self, *rows: Any) -> None:
        await self.session.commit()
        for row in rows:
            await self.session.refresh(row)


class FeedbackRepo:
    """People's ratings of generated emails. `outreach.eo_email_feedback` only."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def for_emails(self, email_ids: list[UUID]) -> dict[UUID, EmailFeedback]:
        if not email_ids:
            return {}
        rows = await self.session.scalars(select(EmailFeedback).where(EmailFeedback.email_id.in_(email_ids)))
        return {f.email_id: f for f in rows}

    async def rate(self, email: Email, lead: Lead, rating: str, tags: list[str], note: str) -> EmailFeedback:
        """Rate an email (again): the text is snapshotted as it is now."""
        values = {
            "lead_intel_icp_id": lead.lead_intel_icp_id, "rating": rating, "tags": tags, "note": note,
            "subject": email.subject, "body": email.edited_body or email.body, "hook": email.hook,
        }  # fmt: skip
        row = (
            await self.session.scalars(
                insert(EmailFeedback)
                .values(workspace_id=email.workspace_id, email_id=email.id, **values)
                .on_conflict_do_update(index_elements=[EmailFeedback.email_id], set_={**values, "updated_at": func.now()})
                .returning(EmailFeedback)
            )
        ).one()
        await self.session.commit()
        return row

    async def clear(self, email_id: UUID) -> None:
        await self.session.execute(delete(EmailFeedback).where(EmailFeedback.email_id == email_id))
        await self.session.commit()

    async def examples(self, workspace_id: UUID, icp_id: UUID, good: int, bad: int) -> list[EmailFeedback]:
        """The ICP's most recent good ratings (up to `good`) and bad ones (up to `bad`)."""
        out: list[EmailFeedback] = []
        for rating, limit in (("good", good), ("bad", bad)):
            if limit <= 0:
                continue
            out += await self.session.scalars(
                select(EmailFeedback)
                .where(
                    EmailFeedback.workspace_id == workspace_id,
                    EmailFeedback.lead_intel_icp_id == icp_id,
                    EmailFeedback.rating == rating,
                )
                .order_by(EmailFeedback.updated_at.desc())
                .limit(limit)
            )
        return out

    async def stats(self, workspace_id: UUID) -> list[tuple[UUID | None, str, int]]:
        rows = await self.session.execute(
            select(EmailFeedback.lead_intel_icp_id, EmailFeedback.rating, func.count())
            .where(EmailFeedback.workspace_id == workspace_id)
            .group_by(EmailFeedback.lead_intel_icp_id, EmailFeedback.rating)
        )
        return [(icp, rating, n) for icp, rating, n in rows]


FEEDBACK_DEFAULTS = {"sample_examples": 3, "good_examples": 3, "bad_examples": 3, "improve_examples": 20}
RESEARCH_DEFAULTS = {"research_max_pages": 6, "research_max_seconds": 120}
# The Settings page's review, auto-send and default values, shared by everyone in the workspace.
GENERAL_DEFAULTS: dict[str, Any] = {
    "default_model": "claude-haiku-5-5", "posts_per_lead": 5, "max_post_age_days": 60, "monthly_budget_usd": 150,
    "score_threshold": 80, "auto_send": False, "daily_auto_send_cap": 50, "scoring_prompt_id": None,
    "company_threshold": 60, "company_auto_approve": True,
}  # fmt: skip
SENDING_DEFAULTS: dict[str, Any] = {
    "sender_company": "", "sender_website": "", "followup_fallback_2": FALLBACK_2, "followup_fallback_3": FALLBACK_3,
    "followup_fallback_4": FALLBACK_4, "followup_fallback_5": FALLBACK_5, "followup_lead_hours": 24,
}  # fmt: skip


class SettingsRepo:
    """The workspace's settings row (`outreach.eo_settings`). Only the fields this app uses so far."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def workspace_exists(self, workspace_id: UUID) -> bool:
        return (await self.session.get(Workspace, workspace_id)) is not None

    async def _get(self, workspace_id: UUID, defaults: dict[str, int]) -> dict[str, int]:
        """These settings, or their defaults while the workspace has no settings row yet."""
        row = await self.session.get(Settings, workspace_id)
        return dict(defaults) if row is None else {k: getattr(row, k) for k in defaults}

    async def _save(self, workspace_id: UUID, values: dict[str, int]) -> None:
        await self.session.execute(
            insert(Settings)
            .values(workspace_id=workspace_id, **values)
            .on_conflict_do_update(index_elements=[Settings.workspace_id], set_={**values, "updated_at": func.now()})
        )
        await self.session.commit()
        # The row may have been read before this save; read it fresh next time.
        self.session.expire_all()

    async def feedback(self, workspace_id: UUID) -> dict[str, int]:
        """How many rated emails are used."""
        return await self._get(workspace_id, FEEDBACK_DEFAULTS)

    async def save_feedback(self, workspace_id: UUID, values: dict[str, int]) -> dict[str, int]:
        await self._save(workspace_id, values)
        return await self.feedback(workspace_id)

    async def sending(self, workspace_id: UUID) -> dict[str, Any]:
        """Who the emails are from, the generic follow-ups, and how early follow-ups are written."""
        return await self._get(workspace_id, SENDING_DEFAULTS)

    async def save_sending(self, workspace_id: UUID, values: dict[str, Any]) -> dict[str, Any]:
        await self._save(workspace_id, values)
        return await self.sending(workspace_id)

    async def cost_estimate(self, workspace_id: UUID) -> dict[str, Any]:
        """The saved cost estimate, or the starting one while none is saved."""
        saved = (await self._get(workspace_id, {"cost_estimate": None}))["cost_estimate"]
        return upgrade_estimate(saved) if saved else DEFAULT_ESTIMATE

    async def save_cost_estimate(self, workspace_id: UUID, estimate: dict[str, Any]) -> dict[str, Any]:
        await self._save(workspace_id, {"cost_estimate": estimate})
        return await self.cost_estimate(workspace_id)

    async def general(self, workspace_id: UUID) -> dict[str, Any]:
        """The review, auto-send and default values. A model that isn't Claude (the column's old default) reads as
        the default one."""
        values = await self._get(workspace_id, GENERAL_DEFAULTS)
        if not str(values["default_model"] or "").startswith("claude-"):
            values["default_model"] = GENERAL_DEFAULTS["default_model"]
        values["monthly_budget_usd"] = float(values["monthly_budget_usd"])
        return values

    async def save_general(self, workspace_id: UUID, values: dict[str, Any]) -> dict[str, Any]:
        await self._save(workspace_id, values)
        return await self.general(workspace_id)

    async def research(self, workspace_id: UUID) -> dict[str, int]:
        """How much of a company's website research may read."""
        return await self._get(workspace_id, RESEARCH_DEFAULTS)

    async def save_research(self, workspace_id: UUID, values: dict[str, int]) -> dict[str, int]:
        await self._save(workspace_id, values)
        return await self.research(workspace_id)


PRODUCT_FIELDS = ("name", "description", "value", "proof", "offer", "cta", "url", "custom_fields", "notes")


class ProductRepo:
    """What the workspace sells. `outreach.eo_products` only."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def workspace_exists(self, workspace_id: UUID) -> bool:
        return (await self.session.get(Workspace, workspace_id)) is not None

    async def list(self, workspace_id: UUID) -> list[Product]:
        query = select(Product).where(Product.workspace_id == workspace_id).order_by(func.lower(Product.name))
        return list(await self.session.scalars(query))

    async def get(self, workspace_id: UUID, product_id: UUID) -> Product | None:
        product = await self.session.get(Product, product_id)
        return product if product and product.workspace_id == workspace_id else None

    async def name_taken(self, workspace_id: UUID, name: str, except_id: UUID | None = None) -> bool:
        query = select(Product.id).where(Product.workspace_id == workspace_id, func.lower(Product.name) == name.lower())
        if except_id:
            query = query.where(Product.id != except_id)
        return (await self.session.scalar(query)) is not None

    async def create(self, workspace_id: UUID, values: dict) -> Product:
        product = Product(workspace_id=workspace_id, **values)
        self.session.add(product)
        await self.session.commit()
        await self.session.refresh(product)
        return product

    async def update(self, product: Product, values: dict) -> Product:
        for key, value in values.items():
            setattr(product, key, value)
        product.updated_at = datetime.now(UTC)
        await self.session.commit()
        await self.session.refresh(product)
        return product

    async def used_by(self, workspace_id: UUID, product_id: UUID) -> list[UUID]:
        """The Lead Intel ICP ids whose outreach settings chose this product."""
        query = select(IcpProfile.lead_intel_icp_id).where(
            IcpProfile.workspace_id == workspace_id, IcpProfile.product_id == product_id
        )
        return list(await self.session.scalars(query))

    async def delete(self, product: Product) -> None:
        await self.session.delete(product)
        await self.session.commit()


class ExampleRepo:
    """The example email library. `outreach.eo_example_emails` only."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def workspace_exists(self, workspace_id: UUID) -> bool:
        return (await self.session.get(Workspace, workspace_id)) is not None

    async def list(self, workspace_id: UUID) -> list[ExampleEmail]:
        query = select(ExampleEmail).where(ExampleEmail.workspace_id == workspace_id).order_by(ExampleEmail.created_at.desc())
        return list(await self.session.scalars(query))

    async def get(self, workspace_id: UUID, example_id: UUID) -> ExampleEmail | None:
        row = await self.session.get(ExampleEmail, example_id)
        return row if row and row.workspace_id == workspace_id else None

    async def for_icp(self, workspace_id: UUID, icp_id: UUID, limit: int) -> list[ExampleEmail]:
        """The newest examples for this ICP: linked to it, or to every ICP."""
        if limit <= 0:
            return []
        query = (
            select(ExampleEmail)
            .where(
                ExampleEmail.workspace_id == workspace_id,
                or_(ExampleEmail.all_icps, ExampleEmail.icp_ids.any(icp_id)),
            )
            .order_by(ExampleEmail.updated_at.desc())
            .limit(limit)
        )
        return list(await self.session.scalars(query))

    async def create(self, workspace_id: UUID, values: dict[str, Any]) -> ExampleEmail:
        row = ExampleEmail(workspace_id=workspace_id, **values)
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def update(self, row: ExampleEmail, values: dict[str, Any]) -> ExampleEmail:
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = datetime.now(UTC)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def delete(self, row: ExampleEmail) -> None:
        await self.session.delete(row)
        await self.session.commit()


class JobRepo:
    """Background jobs (research, writing emails, fetching posts...) and their results. `outreach.eo_jobs`."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, workspace_id: UUID, user_id: UUID, kind: str, label: str, params: dict[str, Any]) -> Job:
        job = Job(workspace_id=workspace_id, created_by=user_id, kind=kind, label=label, params=params, status="queued")
        self.session.add(job)
        await self.session.commit()
        await self.session.refresh(job)
        return job

    async def recent(self, workspace_id: UUID, user_id: UUID | None, limit: int = 50) -> list[Job]:
        query = select(Job).where(Job.workspace_id == workspace_id)
        if user_id is not None:
            query = query.where(Job.created_by == user_id)
        query = query.order_by(case((Job.status.in_(("queued", "running")), 0), else_=1), Job.created_at.desc(), Job.id)
        return list(await self.session.scalars(query.limit(limit)))

    async def quickstart_runs(self, workspace_id: UUID, job_ids: list[UUID]) -> dict[UUID, UUID]:
        """Which Quick Start run each of these jobs is a step of (a run keeps its jobs' ids)."""
        if not job_ids:
            return {}
        wanted = {str(i) for i in job_ids}
        ids = cast(array(list(job_ids)), PG_ARRAY(Uuid))
        query = select(QuickstartRun.id, QuickstartRun.job_ids).where(
            QuickstartRun.workspace_id == workspace_id, QuickstartRun.job_ids.op("&&")(ids)
        )
        return {job: run for run, jobs in (await self.session.execute(query)).all() for job in jobs or [] if str(job) in wanted}

    async def get(self, job_id: UUID) -> Job | None:
        return await self.session.get(Job, job_id)

    async def start(self, job_id: UUID) -> Job | None:
        """Claim a queued job for this worker; None when it isn't queued (already run, or claimed by another)."""
        claimed = await self.session.scalar(
            update(Job).where(Job.id == job_id, Job.status == "queued")
            .values(status="running", started_at=func.now(), updated_at=func.now()).returning(Job.id)
        )  # fmt: skip
        await self.session.commit()
        return await self.session.get(Job, job_id, populate_existing=True) if claimed else None

    async def progress(self, job_id: UUID, step: str, done: int | None, total: int | None) -> None:
        """What a running job is on, for its progress bar: "Saving people", 300 of 1,805."""
        await self.session.execute(
            update(Job).where(Job.id == job_id, Job.status == "running").values(step=step, done=done or 0, total=total)
        )
        await self.session.commit()

    async def finish(self, job_id: UUID, status: str, result: dict[str, Any] | None, detail: str | None) -> None:
        await self.session.execute(
            update(Job).where(Job.id == job_id)
            .values(status=status, result=result, detail=detail, finished_at=func.now(), updated_at=func.now())
        )  # fmt: skip
        await self.session.commit()


class QuickstartRepo:
    """Quick Start runs (`outreach.eo_quickstart_runs`) and what they made: companies, people, leads and drafts.
    Reads come fresh from the database: the run's jobs change these rows between them."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, workspace_id: UUID, user_id: UUID, icp_id: UUID, settings: dict[str, Any], limit: float) -> QuickstartRun:
        run = QuickstartRun(workspace_id=workspace_id, created_by=user_id, icp_id=icp_id, settings=settings, spend_limit_usd=limit)
        self.session.add(run)
        await self.session.commit()
        await self.session.refresh(run)
        return run

    async def get(self, workspace_id: UUID, run_id: UUID) -> QuickstartRun | None:
        return await self.session.scalar(
            select(QuickstartRun).where(QuickstartRun.workspace_id == workspace_id, QuickstartRun.id == run_id)
        )

    async def recent(self, workspace_id: UUID, limit: int = 20) -> list[QuickstartRun]:
        query = select(QuickstartRun).where(QuickstartRun.workspace_id == workspace_id).order_by(QuickstartRun.created_at.desc())
        return list(await self.session.scalars(query.limit(limit)))

    async def claim(self, run_id: UUID, minutes: int) -> QuickstartRun | None:
        """Take the run for one step, unless it's finished or another worker has it."""
        now = datetime.now(UTC)
        claimed = await self.session.scalar(
            update(QuickstartRun)
            .where(
                QuickstartRun.id == run_id, QuickstartRun.status.in_(("queued", "running")),
                or_(QuickstartRun.busy_until.is_(None), QuickstartRun.busy_until < now),
            )
            .values(status="running", busy_until=now + timedelta(minutes=minutes))
            .returning(QuickstartRun.id)
        )  # fmt: skip
        await self.session.commit()
        return await self.session.get(QuickstartRun, run_id, populate_existing=True) if claimed else None

    async def save(self, run: QuickstartRun) -> None:
        await self.session.commit()

    async def companies(self, ids: list[UUID], brief: bool = False) -> list[Company]:
        """With `brief`, only what the run page shows (it's polled every few seconds while a run works)."""
        fresh = select(Company).where(Company.id.in_(ids)).execution_options(populate_existing=True)
        if brief:
            fresh = fresh.options(load_only(Company.id, Company.name, Company.domain, Company.status, Company.status_reason,
                                            Company.score, raiseload=True))  # fmt: skip
        return list(await self.session.scalars(fresh)) if ids else []

    async def new_companies(self, workspace_id: UUID, icp_id: UUID, limit: int) -> list[UUID]:
        """Google Maps companies for the ICP not researched yet, newest first: what this run will verify."""
        query = (
            select(Company.id)
            .where(Company.workspace_id == workspace_id, Company.icp_id == icp_id, Company.source == "google_maps",
                   Company.researched_at.is_(None), Company.status == "found")
            .order_by(Company.created_at.desc())
            .limit(limit)
        )  # fmt: skip
        return list(await self.session.scalars(query))

    async def existing_companies(self, workspace_id: UUID, icp_id: UUID, smart_list_id: UUID | None, limit: int) -> list[UUID]:
        """Companies already here for the ICP (imported, say) that aren't rejected and where no one has an email
        written yet: what a run on existing companies works through. A working record alone (posts read, an email
        revealed) doesn't count: that work is reused. Best score first, then newest. Only those in the smart list,
        when one is given."""
        worked = (
            select(Stakeholder.company_id)
            .join(Email, Email.lead_id == Stakeholder.lead_id)
            .where(Stakeholder.workspace_id == workspace_id, Stakeholder.company_id.is_not(None))
        )
        query = select(Company.id).where(
            Company.workspace_id == workspace_id, Company.icp_id == icp_id, Company.status.not_in(("not_fit", "error")),
            Company.id.not_in(worked),
        )  # fmt: skip
        if smart_list_id is not None:
            members = select(SmartListMember.member_id).where(
                SmartListMember.workspace_id == workspace_id, SmartListMember.kind == "company", SmartListMember.list_id == smart_list_id
            )
            query = query.where(Company.id.in_(members))
        # Companies where someone already has an email first: they're written to without paying for LinkedIn.
        emailed = (
            select(Stakeholder.id)
            .where(Stakeholder.company_id == Company.id, Stakeholder.email.is_not(None))
            .exists()
        )
        query = query.order_by(emailed.desc(), Company.score.desc().nulls_last(), Company.created_at.desc()).limit(limit)
        return list(await self.session.scalars(query))

    async def without_pages(self, ids: list[UUID]) -> list[UUID]:
        """Which of these companies have no website pages read yet."""
        if not ids:
            return []
        read = select(CompanyPage.company_id).where(CompanyPage.company_id.in_(ids))
        return list(await self.session.scalars(select(Company.id).where(Company.id.in_(ids), Company.id.not_in(read))))

    async def read_before(self, ids: list[UUID], since: datetime) -> set[UUID]:
        """Which of these companies had their website read before this moment (a run's start)."""
        if not ids:
            return set()
        query = select(CompanyPage.company_id).where(CompanyPage.company_id.in_(ids)).group_by(CompanyPage.company_id)
        return set(await self.session.scalars(query.having(func.min(CompanyPage.fetched_at) < since)))

    async def contacted(self, workspace_id: UUID, people: list[Stakeholder]) -> set[UUID]:
        """Who among these people was already contacted: an email of theirs queued or sent, or their address ever
        queued (eo_queued_addresses), whichever record it was under."""
        if not people:
            return set()
        lead_ids = [p.lead_id for p in people if p.lead_id]
        sent = set()
        if lead_ids:
            sent = set(await self.session.scalars(
                select(Email.lead_id).where(Email.lead_id.in_(lead_ids), Email.status.in_(("queued", "sent")))
            ))  # fmt: skip
        emails = {p.email.lower() for p in people if p.email}
        queued = set(await self.session.scalars(select(QueuedAddress.email).where(QueuedAddress.email.in_(emails)))) if emails else set()
        return {p.id for p in people if (p.lead_id and p.lead_id in sent) or (p.email and p.email.lower() in queued)}

    async def add_website_contacts(self, workspace_id: UUID, contacts: list[tuple[Company, str]]) -> None:
        """A company's published address as its contact, for companies where LinkedIn found no one with an email.
        Stored as a manual contact (the sources the table allows) marked by its email status, "website". Adding
        the same one again does nothing."""
        for company, email in contacts:
            statement = insert(Stakeholder).values(
                workspace_id=workspace_id, company_id=company.id, lead_intel_icp_id=company.icp_id, source="manual",
                external_id=f"website:{company.id}:{email.lower()}", title="Website contact",
                organization_name=search_name(company.name), email=email, email_status="website", has_email=True,
            )  # fmt: skip
            await self.session.execute(statement.on_conflict_do_nothing(index_elements=[Stakeholder.workspace_id, Stakeholder.source, Stakeholder.external_id]))
        await self.session.commit()

    async def people_at(self, workspace_id: UUID, company_ids: list[UUID]) -> list[Stakeholder]:
        if not company_ids:
            return []
        query = select(Stakeholder).where(Stakeholder.workspace_id == workspace_id, Stakeholder.company_id.in_(company_ids))
        return list(await self.session.scalars(query.order_by(Stakeholder.found_at).execution_options(populate_existing=True)))

    async def leads(self, ids: list[UUID], brief: bool = False) -> list[Lead]:
        fresh = select(Lead).where(Lead.id.in_(ids)).execution_options(populate_existing=True)
        if brief:
            fresh = fresh.options(*_skip(Lead.score_reasons))
        return list(await self.session.scalars(fresh)) if ids else []

    async def first_emails(self, lead_ids: list[UUID]) -> list[Email]:
        if not lead_ids:
            return []
        query = select(Email).where(Email.lead_id.in_(lead_ids), Email.step == 1).order_by(Email.created_at)
        return list(await self.session.scalars(query.execution_options(populate_existing=True)))

    async def briefs(self, company_ids: list[UUID]) -> list[dict[str, Any]]:
        """The analysed companies of a run: what the analysis page shows, with the brief read out of the research JSON
        (not the whole research), and each company's three latest posts (its own or its people's)."""
        if not company_ids:
            return []
        rows = (await self.session.execute(
            select(Company.id, Company.name, Company.domain, Company.website_url, Company.linkedin_url, Company.instagram_url,
                   Company.facebook_url, Company.city, Company.state, Company.score, Company.status, Company.research.op("->")("brief"),
                   Company.raw.op("->>")("employees"), Company.raw.op("->>")("industry"))
            .where(Company.id.in_(company_ids), Company.research.op("?")("brief"))
            .order_by(Company.score.desc().nulls_last(), Company.name)
        )).all()  # fmt: skip
        ids = [r[0] for r in rows]
        people = (await self.session.execute(
            select(Stakeholder.company_id, Stakeholder.lead_id).where(Stakeholder.company_id.in_(ids), Stakeholder.lead_id.is_not(None))
        )).all() if ids else []  # fmt: skip
        company_of_lead = {lead: cid for cid, lead in people}
        recent: dict[UUID, list[dict[str, Any]]] = {}
        if ids:
            owners = [Post.company_id.in_(ids)] + ([Post.lead_id.in_(list(company_of_lead))] if company_of_lead else [])
            posts = await self.session.execute(
                select(Post.company_id, Post.lead_id, Post.source, Post.url, Post.posted_at, func.left(Post.text, 400))
                .where(or_(*owners)).order_by(Post.posted_at.desc().nulls_last()).limit(len(ids) * 12)
            )  # fmt: skip
            for cid, lead, source, url, posted, text_ in posts:
                key = cid or company_of_lead.get(lead)
                if key and len(recent.setdefault(key, [])) < 3:
                    recent[key].append({"source": source, "url": url, "posted_at": posted, "text": text_, "whose": "person" if lead else "company"})
        return [
            {"id": r[0], "name": r[1], "domain": r[2], "website_url": r[3], "linkedin_url": r[4], "instagram_url": r[5], "facebook_url": r[6],
             "city": r[7], "state": r[8], "score": r[9], "status": r[10], "brief": r[11], "employees": int(r[12]) if (r[12] or "").isdigit() else None,
             "industry": r[13], "posts": recent.get(r[0], [])}
            for r in rows
        ]  # fmt: skip

    async def post_counts(self, lead_ids: list[UUID]) -> dict[UUID, int]:
        if not lead_ids:
            return {}
        rows = await self.session.execute(select(Post.lead_id, func.count()).where(Post.lead_id.in_(lead_ids)).group_by(Post.lead_id))
        return {lead_id: n for lead_id, n in rows}


VERIFIED_EMAIL = ("verified", "valid")  # Apollo's word, and the LinkedIn finder's


def _members(kind: str) -> Any:
    """The members person rows stand for in a list: their company, or them. Never null, so a "not" filter works."""
    col = Stakeholder.company_id if kind == "company" else Stakeholder.id
    return select(col).where(col.is_not(None))


class SmartListRepo:
    """Smart lists (`outreach.eo_smart_lists`) and who's in them (`eo_smart_list_members`). Companies and people
    themselves are only read here."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lists(self, workspace_id: UUID, kind: str) -> list[tuple[SmartList, int]]:
        count = (
            select(SmartListMember.list_id, func.count().label("n"))
            .where(SmartListMember.workspace_id == workspace_id, SmartListMember.kind == kind)
            .group_by(SmartListMember.list_id)
            .subquery()
        )
        rows = await self.session.execute(
            select(SmartList, func.coalesce(count.c.n, 0))
            .outerjoin(count, count.c.list_id == SmartList.id)
            .where(SmartList.workspace_id == workspace_id, SmartList.kind == kind)
            .order_by(func.coalesce(count.c.n, 0).desc(), SmartList.name)
        )
        return [(row[0], int(row[1])) for row in rows]

    async def get(self, workspace_id: UUID, list_id: UUID) -> SmartList | None:
        return await self.session.scalar(select(SmartList).where(SmartList.workspace_id == workspace_id, SmartList.id == list_id))

    def _unsorted(self, workspace_id: UUID, kind: str):
        model = Company if kind == "company" else Stakeholder
        sorted_ids = select(SmartListMember.member_id).where(SmartListMember.workspace_id == workspace_id, SmartListMember.kind == kind)
        return model, select(model).where(model.workspace_id == workspace_id, model.id.not_in(sorted_ids))

    async def unsorted(self, workspace_id: UUID, kind: str, limit: int) -> list[Any]:
        model, query = self._unsorted(workspace_id, kind)
        order = model.created_at if kind == "company" else model.found_at
        return list(await self.session.scalars(query.order_by(order).limit(limit)))

    async def unsorted_among(self, workspace_id: UUID, kind: str, ids: list[UUID]) -> list[Any]:
        """Which of these companies or people aren't in a list yet (those an import just brought in, say)."""
        if not ids:
            return []
        model, query = self._unsorted(workspace_id, kind)
        return list(await self.session.scalars(query.where(model.id.in_(ids))))

    async def count_unsorted(self, workspace_id: UUID, kind: str) -> int:
        _, query = self._unsorted(workspace_id, kind)
        return int(await self.session.scalar(select(func.count()).select_from(query.subquery())) or 0)

    async def sorted_keys(self, workspace_id: UUID, kind: str) -> dict[str, UUID]:
        rows = await self.session.execute(
            select(SmartListMember.dedupe_key, SmartListMember.list_id).where(
                SmartListMember.workspace_id == workspace_id, SmartListMember.kind == kind
            )
        )
        return {key: list_id for key, list_id in rows}

    async def delete(self, lst: SmartList) -> None:
        """Delete a list. Its companies or people stay, unsorted (their memberships go with it)."""
        await self.session.delete(lst)
        await self.session.commit()

    async def get_or_create(self, workspace_id: UUID, kind: str, name: str, key: str) -> SmartList:
        statement = insert(SmartList).values(workspace_id=workspace_id, kind=kind, name=name, name_key=key)
        await self.session.execute(statement.on_conflict_do_nothing(index_elements=["workspace_id", "kind", "name_key"]))
        return await self.session.scalar(
            select(SmartList).where(SmartList.workspace_id == workspace_id, SmartList.kind == kind, SmartList.name_key == key)
        )

    async def add_members(self, workspace_id: UUID, kind: str, rows: list[tuple[UUID, UUID, str, str]]) -> None:
        """(member_id, list_id, dedupe_key, reason); a member already sorted keeps its list. Written 500 to a
        statement: one at a time, 1,614 companies took minutes."""
        values = [
            {"workspace_id": workspace_id, "kind": kind, "member_id": member_id, "list_id": list_id, "dedupe_key": key, "reason": reason}
            for member_id, list_id, key, reason in rows
        ]
        for start in range(0, len(values), 500):
            await self.session.execute(insert(SmartListMember).values(values[start : start + 500]).on_conflict_do_nothing())
        await self.session.commit()

    async def members(self, workspace_id: UUID, lst: SmartList) -> list[tuple[Any, SmartListMember]]:
        model = Company if lst.kind == "company" else Stakeholder
        rows = await self.session.execute(
            select(model, SmartListMember)
            .join(SmartListMember, (SmartListMember.member_id == model.id) & (SmartListMember.kind == lst.kind))
            .where(SmartListMember.workspace_id == workspace_id, SmartListMember.list_id == lst.id)
            .order_by(model.name if lst.kind == "company" else model.first_name)
        )
        return [(row[0], row[1]) for row in rows]

    def _written(self, workspace_id: UUID, kind: str) -> Any:
        """Members someone has already had an email written to (what Quick Start counts as worked on): for a company,
        anyone at it; for a person, them."""
        written = _members(kind).join(Email, Email.lead_id == Stakeholder.lead_id)
        return written.where(Stakeholder.workspace_id == workspace_id)

    def _with_email(self, workspace_id: UUID, kind: str) -> Any:
        """Members with an email address: for a company, anyone at it who has one."""
        return _members(kind).where(Stakeholder.workspace_id == workspace_id, Stakeholder.email.is_not(None), Stakeholder.email != "")

    def _contacted(self, workspace_id: UUID, kind: str) -> Any:
        """Members already sent to or queued (what the send queue won't take again): an email of theirs queued or
        sent, or their address ever queued. For a company, anyone at it."""
        return _members(kind).where(
            Stakeholder.workspace_id == workspace_id,
            or_(Stakeholder.lead_id.in_(select(Email.lead_id).where(Email.status.in_(("queued", "sent")))),
                func.lower(Stakeholder.email).in_(select(QueuedAddress.email))),
        )  # fmt: skip

    def _verified(self, workspace_id: UUID, kind: str) -> Any:
        """Members with a verified email: for a company, anyone at it."""
        return _members(kind).where(
            Stakeholder.workspace_id == workspace_id, Stakeholder.email.is_not(None), Stakeholder.email != "",
            Stakeholder.email_status.in_(VERIFIED_EMAIL),
        )  # fmt: skip

    def _facets(self, workspace_id: UUID, kind: str) -> dict[str, Any]:
        """The yes/no filters on list members, each the ids it holds for."""
        return {"processed": self._written(workspace_id, kind), "contacted": self._contacted(workspace_id, kind),
                "verified": self._verified(workspace_id, kind), "emails": self._with_email(workspace_id, kind)}  # fmt: skip

    async def facts(self, workspace_id: UUID, kind: str) -> dict[str, list[UUID]]:
        """For pickers: every company (or person) in the workspace written to, sent or queued, with a verified email,
        and with any email."""
        return {key: [i for i in set(await self.session.scalars(query)) if i] for key, query in self._facets(workspace_id, kind).items()}
    async def member_facts(self, workspace_id: UUID, kind: str, ids: list[UUID]) -> tuple[set[UUID], dict[UUID, int]]:
        """Which of these members were written to, and (companies) how many people at each have an email."""
        if not ids:
            return set(), {}
        model = Company if kind == "company" else Stakeholder
        written = set(await self.session.scalars(select(model.id).where(model.id.in_(ids), model.id.in_(self._written(workspace_id, kind)))))
        if kind != "company":
            return written, {}
        counts = await self.session.execute(
            select(Stakeholder.company_id, func.count()).where(
                Stakeholder.workspace_id == workspace_id, Stakeholder.company_id.in_(ids), Stakeholder.email.is_not(None), Stakeholder.email != ""
            ).group_by(Stakeholder.company_id)
        )  # fmt: skip
        return written, {company: int(n) for company, n in counts.all()}

    async def members_page(
        self, workspace_id: UUID, lst: SmartList, offset: int, limit: int, q: str = "", filters: dict[str, str] | None = None
    ) -> tuple[list[tuple[Any, SmartListMember]], int]:
        """One page of a list's members, optionally filtered by name (company name, or person's name, title, company,
        email) and by any of the yes/no filters together (_facets: written to, sent or queued, a verified email, any
        email; for a company, anyone at it), and how many match in all."""
        model = Company if lst.kind == "company" else Stakeholder
        query = (
            select(model, SmartListMember)
            .join(SmartListMember, (SmartListMember.member_id == model.id) & (SmartListMember.kind == lst.kind))
            .where(SmartListMember.workspace_id == workspace_id, SmartListMember.list_id == lst.id)
        )
        if q.strip():
            like = f"%{q.strip()}%"
            if lst.kind == "company":
                query = query.where(or_(Company.name.ilike(like), Company.domain.ilike(like)))
            else:
                full = func.concat(Stakeholder.first_name, " ", Stakeholder.last_name)
                query = query.where(or_(full.ilike(like), Stakeholder.title.ilike(like), Stakeholder.organization_name.ilike(like),
                                        Stakeholder.email.ilike(like)))  # fmt: skip
        for key, ids in self._facets(workspace_id, lst.kind).items():
            value = (filters or {}).get(key)
            if value in ("yes", "no"):
                holds = model.id.in_(ids)
                query = query.where(holds if value == "yes" else ~holds)
        total = int(await self.session.scalar(select(func.count()).select_from(query.subquery())) or 0)
        order = (model.name,) if lst.kind == "company" else (model.first_name, model.last_name)
        rows = await self.session.execute(query.order_by(*order, model.id).offset(offset).limit(limit))
        return [(row[0], row[1]) for row in rows], total

    async def move(self, workspace_id: UUID, kind: str, member_ids: list[UUID], list_id: UUID) -> int:
        """Put these companies or people in another list (one they're already in is left alone); those in no list
        yet are added to it. Returns how many moved or were added."""
        result = await self.session.execute(
            update(SmartListMember)
            .where(SmartListMember.workspace_id == workspace_id, SmartListMember.kind == kind,
                   SmartListMember.member_id.in_(member_ids), SmartListMember.list_id != list_id)
            .values(list_id=list_id, reason="Moved by hand")
        )  # fmt: skip
        moved = result.rowcount or 0
        unsorted = await self.unsorted_among(workspace_id, kind, member_ids)
        if unsorted:
            key = company_key if kind == "company" else person_key
            await self.add_members(workspace_id, kind, [(m.id, list_id, key(m), "Added by hand") for m in unsorted])
        await self.session.commit()
        return moved + len(unsorted)

    def _candidates(self, workspace_id: UUID, kind: str, list_id: UUID, icp: str | None, in_list: str | None,
                    facets: dict[str, str], q: str | None) -> tuple[Any, Any, Any, Any, tuple[Any, ...]]:  # fmt: skip
        """Who can be added to a list, as the Add dialog filtered them: not already in it; by ICP, by the list they're in
        now and by the yes/no facts; and the search inside the name or the detail line it shows."""
        company = kind == "company"
        model = Company if company else Stakeholder
        current = (
            select(SmartListMember.list_id)
            .where(SmartListMember.workspace_id == workspace_id, SmartListMember.kind == kind, SmartListMember.member_id == model.id)
            .limit(1).scalar_subquery()
        )  # fmt: skip
        icp_col = Company.icp_id if company else Stakeholder.lead_intel_icp_id
        if company:
            name = Company.name
            place = func.nullif(func.concat_ws(", ", func.nullif(Company.city, ""), func.nullif(Company.state, "")), "")
            detail = func.concat_ws(" · ", func.nullif(Company.domain, ""), place)
            order = (Company.score.desc().nulls_last(), Company.created_at.desc(), Company.id)
        else:
            name = func.coalesce(func.nullif(func.trim(func.concat(Stakeholder.first_name, " ", Stakeholder.last_name)), ""),
                                 func.nullif(Stakeholder.email, ""), "Unnamed")  # fmt: skip
            detail = func.concat_ws(" · ", func.nullif(Stakeholder.title, ""), func.nullif(Stakeholder.organization_name, ""),
                                    func.nullif(Stakeholder.email, ""))  # fmt: skip
            order = (Stakeholder.found_at.desc(), Stakeholder.id)
        where: list[Any] = [model.workspace_id == workspace_id, or_(current.is_(None), current != list_id)]
        if icp and icp != "all":
            where.append(icp_col.is_(None) if icp == "none" else icp_col == UUID(icp))
        if in_list and in_list != "all":
            where.append(current.is_(None) if in_list == "none" else current == UUID(in_list))
        for key, ids in self._facets(workspace_id, kind).items():
            if facets.get(key) in ("yes", "no"):
                sub = ids.subquery()
                held = select(sub.c[0]).where(sub.c[0].is_not(None))  # a null here would make NOT IN match nobody
                where.append(model.id.in_(held) if facets[key] == "yes" else ~model.id.in_(held))
        text = listing.any_field(q, name, detail)
        if text is not None:
            where.append(text)
        return model, name, detail, current, (where, icp_col, order)

    async def candidates(self, workspace_id: UUID, kind: str, list_id: UUID, icp: str | None, in_list: str | None, facets: dict[str, str],
                         q: str | None, limit: int, offset: int) -> tuple[list[Any], int]:  # fmt: skip
        model, name, detail, current, (where, icp_col, order) = self._candidates(workspace_id, kind, list_id, icp, in_list, facets, q)
        total = (await self.session.execute(select(func.count()).select_from(model).where(*where))).scalar_one()
        query = select(model.id, name, detail, icp_col, current).where(*where).order_by(*order).limit(limit).offset(offset)
        return list((await self.session.execute(query)).all()), total

    async def candidate_ids(self, workspace_id: UUID, kind: str, list_id: UUID, icp: str | None, in_list: str | None,
                            facets: dict[str, str], q: str | None, cap: int) -> list[Any]:  # fmt: skip
        model, _, _, current, (where, _, order) = self._candidates(workspace_id, kind, list_id, icp, in_list, facets, q)
        return list((await self.session.execute(select(model.id, current).where(*where).order_by(*order).limit(cap))).all())

    async def memberships(self, workspace_id: UUID, kind: str) -> dict[UUID, UUID]:
        """Every company's (or person's) list: member id to list id. Those in no list are left out."""
        rows = await self.session.execute(
            select(SmartListMember.member_id, SmartListMember.list_id).where(
                SmartListMember.workspace_id == workspace_id, SmartListMember.kind == kind
            )
        )
        return {member_id: list_id for member_id, list_id in rows}

    async def list_names(self, workspace_id: UUID, kind: str, member_ids: list[UUID]) -> dict[UUID, str]:
        if not member_ids:
            return {}
        rows = await self.session.execute(
            select(SmartListMember.member_id, SmartList.name)
            .join(SmartList, SmartList.id == SmartListMember.list_id)
            .where(SmartListMember.workspace_id == workspace_id, SmartListMember.kind == kind, SmartListMember.member_id.in_(member_ids))
        )
        return {member_id: name for member_id, name in rows}

    async def people_at(self, workspace_id: UUID, company_ids: list[UUID]) -> list[Stakeholder]:
        if not company_ids:
            return []
        return list(await self.session.scalars(
            select(Stakeholder).where(Stakeholder.workspace_id == workspace_id, Stakeholder.company_id.in_(company_ids))
        ))  # fmt: skip

    async def all_people(self, workspace_id: UUID) -> list[Stakeholder]:
        query = select(Stakeholder).where(Stakeholder.workspace_id == workspace_id).order_by(Stakeholder.found_at)
        return list(await self.session.scalars(query))

    async def companies(self, workspace_id: UUID, ids: list[UUID]) -> dict[UUID, Company]:
        if not ids:
            return {}
        rows = await self.session.scalars(select(Company).where(Company.workspace_id == workspace_id, Company.id.in_(ids)))
        return {c.id: c for c in rows}

    async def save(self, lst: SmartList) -> None:
        self.session.add(lst)
        await self.session.commit()

    async def assign_icp(self, workspace_id: UUID, icp_id: UUID, company_ids: list[UUID]) -> None:
        """Put companies under an ICP, with their people and those people's leads, wherever none is set yet:
        an ICP chosen earlier (by a search for it, or on import) is never replaced."""
        if not company_ids:
            return
        await self.session.execute(
            update(Company).where(Company.workspace_id == workspace_id, Company.id.in_(company_ids), Company.icp_id.is_(None))
            .values(icp_id=icp_id)
        )  # fmt: skip
        people = select(Stakeholder.id).where(Stakeholder.workspace_id == workspace_id, Stakeholder.company_id.in_(company_ids))
        await self.session.execute(
            update(Stakeholder).where(Stakeholder.id.in_(people), Stakeholder.lead_intel_icp_id.is_(None)).values(lead_intel_icp_id=icp_id)
        )
        leads = select(Stakeholder.lead_id).where(Stakeholder.id.in_(people), Stakeholder.lead_id.is_not(None))
        await self.session.execute(
            update(Lead).where(Lead.workspace_id == workspace_id, Lead.id.in_(leads), Lead.lead_intel_icp_id.is_(None))
            .values(lead_intel_icp_id=icp_id)
        )  # fmt: skip
        await self.session.commit()

