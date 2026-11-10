"""The follow-up check, as plain async code that a Celery task runs (see tasks.py and celery_app.py).

Each run looks at every workspace with emails queued in a CheapSequencer campaign: it records sends,
replies and stops, and writes the follow-ups that are nearly due. Nothing is kept between runs: each
one works out what's due from CheapSequencer and the database, so a late or skipped run loses nothing.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select

from config import get_settings
from crud import (
    ExampleRepo, ExclusionRepo, IcpCampaignRepo, IcpProfileRepo, ProductRepo, PromptRepo, ProviderCallRepo, SendingRepo, SettingsRepo,
)  # fmt: skip
from database import get_engine, get_sessionmaker
from lead_intel import LeadIntelRepo
from models import Email
from services import outreach
from services.apify import ApifyClient, ApifyError
from services.generation import EmailWriter
from services.sending import SequencerClient

log = logging.getLogger("coldbrew.jobs")


async def workspaces_in_sequence() -> list[UUID]:
    async with get_sessionmaker()() as session:
        query = select(Email.workspace_id).where(Email.sequence_status.in_(("queued", "active"))).distinct()
        return list(await session.scalars(query))


@asynccontextmanager
async def deps_for(workspace_id: UUID) -> AsyncIterator[outreach.Deps]:
    settings = get_settings()
    sequencer = SequencerClient(settings.cheapsequencer_api_key, base_url=settings.cheapsequencer_api_url)
    writer = EmailWriter(settings.anthropic_api_key, base_url=settings.anthropic_base_url) if settings.anthropic_api_key else None
    try:
        async with get_sessionmaker()() as session, get_engine().connect() as conn:
            await conn.execution_options(postgresql_readonly=True)  # Lead Intel is only ever read
            try:
                yield outreach.Deps(
                    workspace_id, SendingRepo(session), SettingsRepo(session), IcpProfileRepo(session), PromptRepo(session),
                    ProductRepo(session), ExampleRepo(session), ExclusionRepo(session), ProviderCallRepo(session),
                    LeadIntelRepo(conn), sequencer, writer, IcpCampaignRepo(session),
                )  # fmt: skip
            finally:
                await conn.rollback()
    finally:
        await sequencer.aclose()
        if writer:
            await writer.aclose()


@dataclass
class CheckSummary:
    workspaces: int = 0
    checked: int = 0
    written: int = 0
    ended: int = 0
    cost_usd: float = 0.0
    failed: list[str] = field(default_factory=list)


async def check_follow_ups() -> CheckSummary:
    """One follow-up check across every workspace. One failing workspace never stops the others.
    Called on a fresh event loop each time, so the database pool is closed again before returning."""
    summary = CheckSummary()
    try:
        if not get_settings().cheapsequencer_api_key:
            return summary
        for workspace_id in await workspaces_in_sequence():
            summary.workspaces += 1
            try:
                async with deps_for(workspace_id) as deps:
                    run = await outreach.run_follow_ups(deps)
                summary.checked += run.checked
                summary.written += run.written
                summary.ended += run.ended
                summary.cost_usd += run.cost_usd
                if run.notes:
                    log.info("follow-ups for %s: %s", workspace_id, run.notes)
            except Exception:  # noqa: BLE001
                log.exception("follow-ups failed for workspace %s", workspace_id)
                summary.failed.append(str(workspace_id))
    finally:
        # Pooled connections belong to this run's event loop; the next run gets a new loop.
        await get_engine().dispose()
    return summary


# How far back Apify runs are re-read. Apify settles a run's cost within minutes; this is ample.
SETTLE_WINDOW = timedelta(hours=6)


async def settle_apify_costs() -> dict[str, int]:
    """Correct the logged cost of recent Apify runs to what Apify finally charged. A run's cost is read when
    it finishes, but some charges (a posts run's per-post and "no result" events) land a little later, so
    the first figure can be too low. Reading a run is free. Called on a fresh event loop each time."""
    settings = get_settings()
    out = {"checked": 0, "corrected": 0}
    if not settings.apify_token:
        return out
    apify = ApifyClient(settings.apify_token, base_url=settings.apify_api_url)
    try:
        async with get_sessionmaker()() as session:
            calls = ProviderCallRepo(session)
            for row in await calls.apify_runs_since(datetime.now(UTC) - SETTLE_WINDOW):
                run_id = row.idempotency_key.removeprefix("apify:run:")
                try:
                    cost = (await apify.get(f"/actor-runs/{run_id}")).get("data", {}).get("usageTotalUsd")
                except ApifyError as exc:
                    log.warning("couldn't read Apify run %s: %s", run_id, exc.message)
                    continue
                out["checked"] += 1
                if cost is not None and Decimal(str(round(float(cost), 4))) != row.cost_usd:
                    row.cost_usd = Decimal(str(round(float(cost), 4)))
                    out["corrected"] += 1
            await session.commit()
    finally:
        await apify.aclose()
        await get_engine().dispose()
    return out


# ---------- Coldbrew's own sending (services/own_sending.py) ----------


async def own_send() -> dict:
    """One scheduler tick across every workspace: dry run unless OWN_SENDING_LIVE is on. Fresh event loop each time."""
    from services.own_sending import coldbrew_writer, send_due

    settings = get_settings()
    writer = EmailWriter(settings.anthropic_api_key, base_url=settings.anthropic_base_url) if settings.anthropic_api_key else None
    try:
        async with get_sessionmaker()() as session, get_engine().connect() as conn:
            await conn.execution_options(postgresql_readonly=True)  # Lead Intel is only ever read
            try:
                write = coldbrew_writer(writer, LeadIntelRepo(conn)) if writer else None
                summary = await send_due(session, live=settings.own_sending_live, write_follow_up=write)
            finally:
                await conn.rollback()
        return {"live": settings.own_sending_live, "sent": summary.sent, "dry_run": summary.dry_run,
                "failed": summary.failed, "skipped": summary.skipped}  # fmt: skip
    finally:
        if writer:
            await writer.aclose()
        await get_engine().dispose()


async def own_read() -> dict:
    """One inbox tick: replies, bounces and unsubscribes in every mailbox with IMAP. Fresh event loop each time."""
    from services.own_sending import read_inboxes

    try:
        async with get_sessionmaker()() as session:
            summary = await read_inboxes(session, warmup_markers=get_settings().warmup_marker_list)
        return {"read": summary.read, "recorded": summary.recorded, "failed": summary.failed}
    finally:
        await get_engine().dispose()
