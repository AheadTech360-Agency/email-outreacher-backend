"""Alembic environment, fenced to the `outreach` schema.

Lead Intel's tables share this database and must never be touched. Three things keep migrations
away from them:
  1. target_metadata is this service's models only; Lead Intel's tables are not in it.
  2. include_name / include_object drop every schema and object outside `outreach`, so
     autogenerate never even sees Lead Intel's tables (and so can never propose dropping them).
  3. Alembic's own version table lives in `outreach` too, as `eo_alembic_version`.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from config import get_settings
from database import to_async_url
from models import OUTREACH_SCHEMA, TABLE_SCHEMA, Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_name(name, type_, parent_names) -> bool:
    if type_ == "schema":
        # `public` is the default schema, which Alembic names None.
        return name == OUTREACH_SCHEMA or (name is None and OUTREACH_SCHEMA == "public")
    return True


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Only our eo_ tables. In Coldbrew's own database they share `public` with Lead Intel's (icps, businesses...),
    which Alembic must never see, let alone propose dropping."""
    table = obj if type_ == "table" else getattr(obj, "table", None)
    schema = getattr(table, "schema", None) if table is not None else None
    ours = schema == OUTREACH_SCHEMA or (schema is None and OUTREACH_SCHEMA == "public")
    return ours and (table is None or table.name.startswith("eo_"))


def _configure(connection=None, url=None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        include_object=include_object,
        version_table="eo_alembic_version",
        version_table_schema=TABLE_SCHEMA,
        literal_binds=connection is None,
    )


def run_migrations_offline() -> None:
    url, _ = to_async_url(_database_url())
    _configure(url=url)
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
        if OUTREACH_SCHEMA == "public":
            # Supabase's Data API publishes `public` to anyone with the publishable key: every eo_ table, a new one
            # included, gets row level security with no rules, so it reads nothing there (the backend owns the tables).
            exposed = connection.execute(
                text("select tablename from pg_tables where schemaname = 'public' and tablename like 'eo\\_%' and not rowsecurity")
            ).scalars().all()
            for table in exposed:
                connection.execute(text(f'alter table public."{table}" enable row level security'))


async def run_migrations_online() -> None:
    url, connect_args = to_async_url(_database_url())
    engine = create_async_engine(url, connect_args=connect_args | {"statement_cache_size": 0})
    async with engine.connect() as connection:
        # The version table goes in `outreach`, so the schema has to exist before Alembic looks.
        await connection.execute(text(f"create schema if not exists {OUTREACH_SCHEMA}"))
        await connection.commit()
        await connection.run_sync(_run_sync)
        await connection.commit()
    await engine.dispose()


def _database_url() -> str:
    url = get_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
