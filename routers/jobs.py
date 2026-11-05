"""Background jobs: start a long-running endpoint in the Celery worker, then check on it (see background.py)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

import response_cache
from background import SALES_JOBS, long_running
from config import get_settings
from crud import JobRepo
from dependencies import MANAGERS_ONLY, get_current_user, get_job_queue, get_job_repo, require_workspace_id
from models import Job
from schemas import JobIn, JobOut
from services.auth import CurrentUser

router = APIRouter(prefix="/jobs", tags=["jobs"])

# A queued job nobody has picked up after this long most likely has no worker to run it.
NO_WORKER_AFTER = timedelta(seconds=20)
NO_WORKER = "Waiting for a worker. If this doesn't start soon, the Celery worker isn't running."
STOPPED = "It stopped without finishing: the worker restarted or it ran out of time. Start it again."
NO_QUEUE = "Coldbrew's job queue isn't reachable. Start Redis and the Celery worker, then try again."


def _out(job: Job) -> JobOut:
    now = datetime.now(UTC)
    status, step, detail = job.status, job.step, job.detail
    if status == "queued" and now - job.created_at > NO_WORKER_AFTER:
        step = NO_WORKER
    # The worker stops a job at the time limit; one still "running" well after that died with its worker.
    limit = timedelta(seconds=get_settings().job_time_limit_seconds + 120)
    if status == "running" and job.started_at and now - job.started_at > limit:
        status, detail = "failed", STOPPED
    result = job.result or {}
    return JobOut(
        id=job.id, kind=job.kind, label=job.label, status=status, step=step, detail=detail,
        done=max(0, getattr(job, "done", 0) or 0), total=getattr(job, "total", None),
        status_code=result.get("statusCode"), result=result.get("body"),
        created_at=job.created_at, started_at=job.started_at, finished_at=job.finished_at,
    )  # fmt: skip


@router.post("", response_model=JobOut, status_code=202)
async def start_job(
    body: JobIn,
    workspace_id: UUID = Depends(require_workspace_id),
    user: CurrentUser = Depends(get_current_user),
    jobs: JobRepo = Depends(get_job_repo),
    queue: Callable[[UUID], None] = Depends(get_job_queue),
) -> JobOut:
    """Start one of the long-running endpoints in the background. Who may run it is checked when it runs,
    as the person starting it, exactly as a request would be."""
    label = long_running(body.path)
    if label is None:
        raise HTTPException(status_code=422, detail="That doesn't run as a background job.")
    # Refused here, before it's queued (the endpoint checks again when the worker runs it as this person).
    if body.path not in SALES_JOBS and user.role not in ("admin", "management"):
        raise HTTPException(status_code=403, detail=MANAGERS_ONLY)
    job = await jobs.create(workspace_id, user.id, body.path, label, {"path": body.path, "body": body.body})
    try:
        queue(job.id)
    except Exception as exc:  # noqa: BLE001 - Redis down or unreachable: say so instead of waiting forever
        await jobs.finish(job.id, "failed", None, NO_QUEUE)
        raise HTTPException(status_code=503, detail=NO_QUEUE) from exc
    return _out(job)


@router.get("", response_model=list[JobOut])
async def recent_jobs(
    workspace_id: UUID = Depends(require_workspace_id),
    user: CurrentUser = Depends(get_current_user),
    jobs: JobRepo = Depends(get_job_repo),
) -> list[JobOut]:
    """Recent jobs, active first. Same visibility as the individual job endpoint; omit large results."""
    rows = await jobs.recent(workspace_id, None if user.is_admin else user.id)
    return [_out(job).model_copy(update={"result": None}) for job in rows]


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: UUID,
    workspace_id: UUID = Depends(require_workspace_id),
    user: CurrentUser = Depends(get_current_user),
    jobs: JobRepo = Depends(get_job_repo),
) -> JobOut:
    """A job and, once finished, its result. Only its starter (or an admin) sees it."""
    job = await jobs.get(job_id)
    if job is None or job.workspace_id != workspace_id or (job.created_by != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="Job not found")
    out = _out(job)
    if out.status not in ("queued", "running"):
        # The worker changed things in another process: reads cached before it finished are out of date.
        response_cache.clear()
    return out
