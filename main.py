"""Entry point. Run with: uvicorn main:app --reload"""

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from response_cache import ResponseCache
from dependencies import get_current_user, refuse_inline, require_api_secret
from routers import (
    analysis, companies, emails, examples, exclusions, health, home, icps, improve, jobs, leads, mailboxes, pipeline, posts, products, prompts,
    quickstart, records, saved_views, sending, settings, smart_lists, sources, stakeholders, usage, users, webhooks, workspaces,
)  # fmt: skip


# Every feature router is included here, under /api/v1. Only health and the webhooks are open. The rest need, in order,
# the shared API secret from the frontend's server and an allowed origin (dependencies.require_api_secret),
# then a signed-in Supabase session (dependencies.get_current_user). Long-running endpoints only run as
# background jobs (background.py, dependencies.refuse_inline).
api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
# CheapSequencer's webhooks can't sign in: each delivery is checked against its signature instead (routers/webhooks.py).
api_router.include_router(webhooks.router)
protected = APIRouter(dependencies=[Depends(require_api_secret), Depends(get_current_user), Depends(refuse_inline)])
protected.include_router(analysis.router)
protected.include_router(users.router)
protected.include_router(icps.router)
protected.include_router(companies.router)
protected.include_router(leads.router)
protected.include_router(workspaces.router)
protected.include_router(stakeholders.router)
protected.include_router(posts.router)
protected.include_router(pipeline.router)
protected.include_router(usage.router)
protected.include_router(prompts.router)
protected.include_router(emails.router)
protected.include_router(improve.router)
protected.include_router(settings.router)
protected.include_router(products.router)
protected.include_router(examples.router)
protected.include_router(exclusions.router)
protected.include_router(mailboxes.router)
protected.include_router(sending.router)
protected.include_router(sources.router)
protected.include_router(jobs.router)
protected.include_router(quickstart.router)
protected.include_router(smart_lists.router)
protected.include_router(home.router)
protected.include_router(records.router)
protected.include_router(saved_views.router)
api_router.include_router(protected)


def create_app() -> FastAPI:
    settings = get_settings()
    # Background work (the follow-up check) runs in Celery workers, not here: see celery_app.py.
    app = FastAPI(title=settings.app_name)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.authorized_url_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Repeat reads come from memory until anything changes (response_cache.py). Inside CORS, so cached
    # answers get the same CORS headers.
    app.add_middleware(ResponseCache)

    app.include_router(api_router)

    # Unversioned liveness check for Railway's healthcheck.
    @app.get("/health", include_in_schema=False)
    def root_health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
