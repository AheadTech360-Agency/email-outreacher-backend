"""Lead Intel's tables, described for reading only.

These tables belong to Lead Intel and must never be written, altered or migrated from here.
Everything that touches Lead Intel is in this file, and nothing in it writes. The tables live in
their own MetaData, which Alembic never sees, and every query against them runs in a READ ONLY
transaction (see get_readonly_conn in database.py). Only the columns this service reads are listed.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
    and_,
    cast,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncConnection


lead_intel_metadata = MetaData(schema="public")

icps = Table(
    "icps",
    lead_intel_metadata,
    # Lead Intel generates ids itself (gen_random_uuid()); described here, not set by us.
    Column("id", PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    # Lead Intel user who created it. Only written when this service adds an ICP (lead_intel_writes.py).
    Column("created_by", PG_UUID(as_uuid=True)),
    Column("name", Text),
    Column("industry", Text),
    Column("priority", Integer),
    Column("enabled", Boolean),
    # The matching rule, exactly as Lead Intel stores it.
    Column("sources", ARRAY(Text)),
    Column("any_of", ARRAY(Text)),
    Column("none_of", ARRAY(Text)),
    Column("website_any_of", ARRAY(Text)),
    Column("instagram_any_of", ARRAY(Text)),
    Column("require_all", Boolean),
    Column("min_followers", Integer),
    Column("max_followers", Integer),
    Column("search_terms", ARRAY(Text)),
    Column("requires_website", Boolean),
    Column("min_reviews", Integer),
    Column("places", ARRAY(Text)),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)

businesses = Table(
    "businesses",
    lead_intel_metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("status", Text),
    Column("display_name", Text),
    Column("website_status", Text),
    Column("city", Text),
    Column("state", Text),
    Column("country", Text),
    Column("exclusion_flag", Boolean),
    Column("problem_score", Float),
    Column("tier", SmallInteger),
    Column("icp_id", PG_UUID(as_uuid=True)),
    Column("icp_name", Text),
    Column("created_at", DateTime(timezone=True)),
)

field_metadata = Table(
    "field_metadata",
    lead_intel_metadata,
    Column("business_id", PG_UUID(as_uuid=True)),
    Column("field_name", Text),
    Column("value", Text),
    Column("source_name", Text),
    Column("last_verified_at", DateTime(timezone=True)),
)

identity_keys = Table(
    "identity_keys",
    lead_intel_metadata,
    Column("business_id", PG_UUID(as_uuid=True)),
    Column("key_type", Text),
    Column("key_value", Text),
)


# The fields shown on every company row. Everything else is only returned on the detail view.
LIST_FIELDS = (
    "website_url",
    "instagram_handle",
    "instagram_url",
    "instagram_followers",
    "instagram_category",
    "instagram_bio",
)

# Lead Intel's status columns are Postgres enums; comparing them as text avoids enum-vs-varchar
# operator errors without having to declare the enum types here.
_contactable = and_(
    cast(businesses.c.status, Text) == "active",
    businesses.c.exclusion_flag.is_(False),
)


class LeadIntelRepo:
    def __init__(self, conn: AsyncConnection) -> None:
        self.conn = conn

    async def icp_names(self) -> list[dict[str, Any]]:
        """Every ICP's id, name and whether it's enabled, without the company counts list_icps adds."""
        query = select(icps.c.id, icps.c.name, icps.c.enabled).order_by(icps.c.priority, icps.c.created_at)
        return [dict(row) for row in (await self.conn.execute(query)).mappings()]

    async def list_icps(self) -> list[dict[str, Any]]:
        counts = (
            select(businesses.c.icp_id, func.count().label("company_count"))
            .where(_contactable, businesses.c.icp_id.is_not(None))
            .group_by(businesses.c.icp_id)
            .subquery()
        )
        query = (
            select(icps, func.coalesce(counts.c.company_count, 0).label("company_count"))
            .outerjoin(counts, counts.c.icp_id == icps.c.id)
            .order_by(icps.c.priority, icps.c.created_at)
        )
        return [dict(row) for row in (await self.conn.execute(query)).mappings()]

    async def get_icp(self, icp_id: UUID) -> dict[str, Any] | None:
        count = (
            select(func.count())
            .where(_contactable, businesses.c.icp_id == icp_id)
            .scalar_subquery()
        )
        query = select(icps, count.label("company_count")).where(icps.c.id == icp_id)
        row = (await self.conn.execute(query)).mappings().first()
        return dict(row) if row else None

    async def list_companies(
        self,
        icp_id: UUID | None,
        q: str | None,
        limit: int,
        offset: int,
        *,
        no_icp: bool = False,
        only_ids: list[UUID] | None = None,
        exclude_ids: list[UUID] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        where = [_contactable]
        if icp_id:
            where.append(businesses.c.icp_id == icp_id)
        if no_icp:
            where.append(businesses.c.icp_id.is_(None))
        if only_ids is not None:
            where.append(businesses.c.id.in_(only_ids))
        if exclude_ids:
            where.append(businesses.c.id.not_in(exclude_ids))
        if q:
            where.append(businesses.c.display_name.ilike(f"%{q}%"))

        # The total rides along on every row (a window count), saving a round trip to the database.
        rows = (
            await self.conn.execute(
                select(businesses, func.count().over().label("_total"))
                .where(*where)
                .order_by(businesses.c.tier.asc().nulls_last(), businesses.c.problem_score.desc().nulls_last(), businesses.c.id)
                .limit(limit)
                .offset(offset)
            )
        ).mappings().all()
        if rows:
            total = rows[0]["_total"]
        elif offset:  # past the last page: no row to carry the total
            total = (await self.conn.execute(select(func.count()).select_from(businesses).where(*where))).scalar_one()
        else:
            total = 0

        companies = [{k: v for k, v in row.items() if k != "_total"} for row in rows]
        await self._attach_fields(companies, LIST_FIELDS)
        return companies, total

    async def contactable_ids(self, ids: list[UUID] | None = None, icp_id: UUID | None = None) -> list[UUID]:
        """Which of these ids (or every company in this ICP) exist and may be contacted."""
        if ids is None and icp_id is None:
            raise ValueError("Pass ids or an ICP")
        query = select(businesses.c.id).where(_contactable)
        if ids is not None:
            query = query.where(businesses.c.id.in_(ids))
        if icp_id is not None:
            query = query.where(businesses.c.icp_id == icp_id)
        return list((await self.conn.execute(query)).scalars())

    async def get_company(self, company_id: UUID) -> dict[str, Any] | None:
        row = (
            await self.conn.execute(select(businesses).where(businesses.c.id == company_id, _contactable))
        ).mappings().first()
        if not row:
            return None
        company = dict(row)
        await self._attach_fields([company], None)
        return company

    async def _attach_fields(self, companies: list[dict[str, Any]], names: tuple[str, ...] | None) -> None:
        """Add each company's stored fields and its domain, in two queries for the whole page."""
        ids = [c["id"] for c in companies]
        for c in companies:
            c["fields"] = {}
            c["domain"] = None
        if not ids:
            return
        by_id = {c["id"]: c for c in companies}

        fields_query = select(field_metadata.c.business_id, field_metadata.c.field_name, field_metadata.c.value).where(
            field_metadata.c.business_id.in_(ids)
        )
        if names is not None:
            fields_query = fields_query.where(field_metadata.c.field_name.in_(names))
        for business_id, name, value in await self.conn.execute(fields_query):
            by_id[business_id]["fields"][name] = value

        domains = select(identity_keys.c.business_id, identity_keys.c.key_value).where(
            identity_keys.c.business_id.in_(ids), cast(identity_keys.c.key_type, Text) == "domain"
        )
        for business_id, domain in await self.conn.execute(domains):
            by_id[business_id]["domain"] = domain
