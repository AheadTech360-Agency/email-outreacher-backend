"""Long-running work runs in a Celery worker, never inside a web request.

Research, finding companies, fetching posts, writing emails, people searches and the like can take
minutes and call paid providers. The frontend starts them with POST /jobs (routers/jobs.py), which
records an eo_jobs row and queues `coldbrew.run_job`; the page then checks GET /jobs/{id} until it's
done. Calling one of these endpoints directly is refused (dependencies.refuse_inline).

The worker runs the very same endpoint, in-process, as the person who started the job: their role and
workspace access are checked exactly as in a request. `current_job` marks that call; it's a context
variable, so nothing outside the worker process can set it.

A job is never retried on its own (a retry could pay for the same work twice): one that dies with its
worker shows as failed and is started again by hand.
"""

import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from config import get_settings
from crud import JobRepo, UserRepo
from database import get_engine, get_sessionmaker
from services.auth import CurrentUser

log = logging.getLogger("coldbrew.background")

# POST endpoints (under /api/v1) that only run as background jobs, with what the person sees.
LONG_RUNNING: dict[str, str] = {
    "/pipeline/find": "Finding companies",
    "/pipeline/research": "Researching companies",
    "/pipeline/stakeholders": "Finding people",
    "/emails/generate": "Writing emails",
    "/posts/fetch": "Fetching posts",
    "/stakeholders/search": "Searching for people",
    "/stakeholders/search-icp": "Searching for people",
    "/stakeholders/search-linkedin": "Searching LinkedIn",
    "/stakeholders/reveal": "Revealing emails",
    "/improve/email-prompt": "Improving the email prompt",
    "/sending/queue": "Queueing emails",
    "/sending/follow-ups/run": "Writing follow-ups",
    "/sending/campaigns/sync": "Syncing campaigns",
    "/smart-lists/sort": "Sorting smart lists",
    "/stakeholders/import-run": "Importing people",
    "/pipeline/companies/{id}/reread": "Reading the website again",
}


def long_running(path: str) -> str | None:
    """The label of the background-only endpoint at this path, if it is one. "{id}" in a registered path stands for
    any one path segment (a company's id, say)."""
    if path in LONG_RUNNING:
        return LONG_RUNNING[path]
    parts = path.strip("/").split("/")
    for pattern, label in LONG_RUNNING.items():
        want = pattern.strip("/").split("/")
        if len(want) == len(parts) and all(w == "{id}" or w == p for w, p in zip(want, parts, strict=True)):
            return label
    return None


# The background jobs Sales may start: sending approved emails (the Roles tab's "Send approved emails"). Every
# other one spends budget or changes the campaign setup, so it's for Management and admins.
SALES_JOBS = {"/sending/queue", "/stakeholders/import-run"}  # an import sorts only for Management


@dataclass(frozen=True)
class JobContext:
    job_id: UUID
    user: CurrentUser


current_job: ContextVar[JobContext | None] = ContextVar("coldbrew_current_job", default=None)


async def report(step: str, done: int | None = None, total: int | None = None) -> None:
    """Show a running job's progress ("Saving people", 300 of 1,805). Its own session, so it's seen at once whatever
    the job's session is in the middle of. Outside a job (a direct call, a test) it does nothing."""
    job = current_job.get()
    if job is None:
        return
    async with get_sessionmaker()() as session:
        await JobRepo(session).progress(job.job_id, step, done, total)

GONE = "Its starter is no longer a user here, so it wasn't run."
CRASHED = "Something went wrong running it. The worker's log has the details."
TIMED_OUT = "It ran out of time (JOB_TIME_LIMIT_SECONDS) and was stopped. Try fewer at a time."


async def _user(users: UserRepo, user_id: UUID | None) -> CurrentUser | None:
    row = await users.get(user_id) if user_id else None
    if row is None or row.status == "deactivated":
        return None
    return CurrentUser(row.id, row.email, row.name, row.role, await users.workspace_ids(row.id))


async def run_job(job_id: UUID) -> None:
    """Run one queued job to the end and record its result. Called by the Celery task, on a fresh event
    loop each time, so the database pool is closed again before returning."""
    try:
        async with get_sessionmaker()() as session:
            job = await JobRepo(session).start(job_id)
            if job is None:
                return  # already run, or another worker has it
            user = await _user(UserRepo(session), job.created_by)
            if user is None:
                await JobRepo(session).finish(job_id, "failed", None, GONE)
                return
            path, body, workspace_id = job.params["path"], job.params.get("body"), job.workspace_id
        try:
            # Enforced here as well as by Celery: Celery's time limits do nothing with --pool=solo (Windows).
            async with asyncio.timeout(get_settings().job_time_limit_seconds):
                status_code, payload = await _call(JobContext(job_id, user), path, body, workspace_id)
        except TimeoutError:
            log.warning("job %s (%s) ran out of time", job_id, path)
            status_code, payload = 504, {"detail": TIMED_OUT}
        except Exception:  # noqa: BLE001 - recorded on the job, so the page stops waiting
            log.exception("job %s (%s) crashed", job_id, path)
            status_code, payload = 500, {"detail": CRASHED}
        ok = status_code < 400
        detail = None if ok else _detail(payload, status_code)
        async with get_sessionmaker()() as session:
            await JobRepo(session).finish(job_id, "done" if ok else "failed", {"statusCode": status_code, "body": payload}, detail)
    finally:
        await get_engine().dispose()


async def _call(context: JobContext, path: str, body: Any, workspace_id: UUID) -> tuple[int, Any]:
    from main import app  # here, not at import: main imports the routers, which import this module

    token = current_job.set(context)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://coldbrew", timeout=None) as client:
            response = await client.post(f"/api/v1{path}", json=body, headers={"X-Workspace-Id": str(workspace_id)})
    finally:
        current_job.reset(token)
    try:
        payload = response.json() if response.content else None
    except ValueError:
        payload = None
    return response.status_code, payload


def _detail(payload: Any, status_code: int) -> str:
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail:  # a validation error: the first problem, in words
        first = detail[0]
        where = ".".join(str(p) for p in first.get("loc", [])[1:])
        return f"{where}: {first.get('msg')}" if where else str(first.get("msg"))
    return f"It failed ({status_code})."
