"""Celery tasks. Each runs async Coldbrew code on its own event loop."""

import asyncio
import logging
from dataclasses import asdict
from uuid import UUID

import background
import jobs
from services import quickstart
from celery_app import app
from config import get_settings

log = logging.getLogger("coldbrew.tasks")

LOCK_KEY = "coldbrew:follow-ups"


def _lock():
    """A Redis lock so two checks never overlap (a slow run when beat fires again, or several workers).
    None when the broker isn't Redis, as in tests and the in-memory broker."""
    settings = get_settings()
    if not settings.broker_url.startswith(("redis://", "rediss://")):
        return None
    import redis

    client = redis.Redis.from_url(settings.broker_url)
    # Released when the check ends; expires on its own if a worker dies mid-run.
    return client.lock(LOCK_KEY, timeout=settings.followup_check_seconds, blocking=False)


@app.task(name="coldbrew.check_follow_ups")
def check_follow_ups() -> dict:
    """Record sends and replies, and write the follow-ups that are nearly due, for every workspace."""
    lock = _lock()
    if lock is not None and not lock.acquire():
        log.info("follow-up check skipped: another one is still running")
        return {"skipped": True}
    try:
        summary = asdict(asyncio.run(jobs.check_follow_ups()))
        log.info("follow-up check: %s", summary)
        return summary
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:  # noqa: BLE001 - it expired already
                pass


@app.task(name="coldbrew.settle_apify_costs")
def settle_apify_costs() -> dict:
    """Correct recent Apify runs' logged cost to what Apify finally charged (jobs.settle_apify_costs)."""
    summary = asyncio.run(jobs.settle_apify_costs())
    log.info("Apify costs: %s", summary)
    return summary


_limit = get_settings().job_time_limit_seconds


# Never acknowledged late, so a worker that dies mid-job doesn't hand it to another: running it twice could
# pay twice. The job shows as failed instead (routers/jobs.py) and is started again by hand.
@app.task(name="coldbrew.run_job", acks_late=False, soft_time_limit=_limit, time_limit=_limit + 60)
def run_job(job_id: str) -> None:
    """One background job: a long-running endpoint, run as the person who started it (background.py)."""
    asyncio.run(background.run_job(UUID(job_id)))


# One batch of a Quick Start, then the next is queued behind whatever else is waiting (services/quickstart.py).
@app.task(name="coldbrew.quickstart_step", acks_late=False, soft_time_limit=_limit + 600, time_limit=_limit + 660)
def quickstart_step(run_id: str) -> None:
    if asyncio.run(quickstart.advance(UUID(run_id))):
        quickstart_step.apply_async(args=[run_id], retry=False)


def _own_lock(name: str):
    """One own-sending tick of each kind at a time: a slow one never overlaps the next (two would send twice)."""
    settings = get_settings()
    if not settings.broker_url.startswith(("redis://", "rediss://")):
        return None
    import redis

    return redis.Redis.from_url(settings.broker_url).lock(f"coldbrew:own:{name}", timeout=settings.own_sending_tick_seconds * 3, blocking=False)


def _locked(name: str, run) -> dict:
    lock = _own_lock(name)
    if lock is not None and not lock.acquire():
        log.info("own %s skipped: another one is still running", name)
        return {"skipped": True}
    try:
        summary = asyncio.run(run())
        log.info("own %s: %s", name, summary)
        return summary
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:  # noqa: BLE001 - it expired already
                pass


@app.task(name="coldbrew.own_send", acks_late=False)
def own_send() -> dict:
    """Coldbrew's own sending: the next due step per mailbox (jobs.own_send). Dry run unless OWN_SENDING_LIVE."""
    return _locked("send", jobs.own_send)


@app.task(name="coldbrew.own_read")
def own_read() -> dict:
    """Coldbrew's own sending: replies, bounces and unsubscribes in each mailbox (jobs.own_read)."""
    return _locked("read", jobs.own_read)


@app.task(name="coldbrew.own_warm", acks_late=False)
def own_warm() -> dict:
    """The email warmer (jobs.own_warm). Dry run unless OWN_WARMUP_LIVE."""
    return _locked("warm", jobs.own_warm)
