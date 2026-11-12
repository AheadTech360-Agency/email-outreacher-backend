import hmac
import time
from collections.abc import AsyncIterator, Callable, Mapping

import anthropic
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from background import current_job, long_running
from config import get_settings, origin_of
from crud import (
    CompanyRepo,
    EmailRepo,
    ExampleRepo,
    ExclusionRepo,
    IcpCampaignRepo,
    FeedbackRepo,
    IcpProfileRepo,
    JobRepo,
    QuickstartRepo,
    SmartListRepo,
    LeadRepo,
    PostRepo,
    ProductRepo,
    PromptRepo,
    SendingRepo,
    SettingsRepo,
    ProviderCallRepo,
    StakeholderRepo,
    UserRepo,
    WorkspaceCompanyRepo,
    WorkspaceRepo,
)
from database import get_lead_intel_icp_conn, get_readonly_conn, get_session
from lead_intel import LeadIntelRepo
from lead_intel_writes import LeadIntelIcpWriter
from services.apify import ApifyClient
from services.supabase_admin import SupabaseAdmin
from services.apollo import ApolloClient
from services.access import AccessRepo, Scope
from services.auth import AuthError, CurrentUser, verify
from services.generation import EmailWriter
from services.google_places import PlacesClient
from services.jina import JinaClient
from services.briefs import BriefWriter
from services.scoring import CompanyScorer
from services.websearch import BraveSearch
from services.sending import InboxesClient, SequencerClient
from services.website import WebsiteReader


def get_lead_intel_repo(conn: AsyncConnection = Depends(get_readonly_conn)) -> LeadIntelRepo:
    return LeadIntelRepo(conn)


def get_sending_repo(session: AsyncSession = Depends(get_session)) -> SendingRepo:
    return SendingRepo(session)


def get_exclusion_repo(session: AsyncSession = Depends(get_session)) -> ExclusionRepo:
    return ExclusionRepo(session)


def get_user_repo(session: AsyncSession = Depends(get_session)) -> UserRepo:
    return UserRepo(session)


SECRET_HEADER = "x-coldbrew-secret"


def secret_refusal(headers: Mapping[str, str]) -> HTTPException | None:
    """Why a request didn't come from Coldbrew's own frontend server, or None when it did: it must send
    API_SECRET in X-Coldbrew-Secret, and an Origin or Referer, when there is one, must be in AUTHORIZED_URLS.
    Shared with the response cache (response_cache.py), which answers before any dependency runs."""
    settings = get_settings()
    if not settings.api_secret:
        return HTTPException(status_code=503, detail="The API isn't configured: API_SECRET is not set.")
    sent = headers.get(SECRET_HEADER, "")
    if not hmac.compare_digest(sent.encode(), settings.api_secret.encode()):
        return HTTPException(status_code=401, detail="Not authorized.")
    for header in ("origin", "referer"):
        value = headers.get(header)
        if value and origin_of(value) not in settings.authorized_url_list:
            return HTTPException(status_code=403, detail="Not authorized.")
    return None


async def require_api_secret(request: Request) -> None:
    """Only Coldbrew's own frontend server may call the API (see secret_refusal). Runs before sign-in, so a
    stranger's request is refused without touching Supabase or the database."""
    if current_job.get() is not None:
        return  # a background job, run in-process by the worker (background.py): no browser involved
    refusal = secret_refusal(request.headers)
    if refusal is not None:
        raise refusal


# Who a signed-in account is (role, workspaces), kept briefly: looking it up costs two or three database
# round trips, on every request. The token itself is still checked every time. Cleared whenever an admin
# changes users or workspace members; another API process picks the change up within SIGN_IN_CACHE_SECONDS.
SIGN_IN_CACHE_SECONDS = 60
_sign_ins: dict[UUID, tuple[float, CurrentUser]] = {}


def forget_sign_ins() -> None:
    _sign_ins.clear()


NOT_ADDED = "This account hasn't been added to Coldbrew. Ask an admin to add your email on the Users page."


async def get_current_user(
    authorization: str | None = Header(default=None), users: UserRepo = Depends(get_user_repo)
) -> CurrentUser:
    """The signed-in person, from the Supabase access token in `Authorization: Bearer`. Every API
    route except health depends on this. A background job runs as the person who started it."""
    job = current_job.get()
    if job is not None:
        return job.user
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    settings = get_settings()
    try:
        claims = await verify(token, settings.jwks_url, settings.auth_issuer)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc
    cached = _sign_ins.get(claims.auth_user_id)
    if cached and time.monotonic() - cached[0] < SIGN_IN_CACHE_SECONDS:
        return cached[1]
    user = await users.for_sign_in(claims.auth_user_id, claims.email)
    if user is None:
        raise HTTPException(status_code=403, detail=NOT_ADDED)
    if user.status == "deactivated":
        raise HTTPException(status_code=403, detail="Your account is deactivated. Ask an admin to turn it back on.")
    current = CurrentUser(user.id, user.email, user.name, user.role, await users.workspace_ids(user.id))
    if len(_sign_ins) > 1000:
        _sign_ins.clear()
    _sign_ins[claims.auth_user_id] = (time.monotonic(), current)
    return current


async def refuse_inline(request: Request) -> None:
    """Long-running endpoints (background.LONG_RUNNING) only run in the worker: a request holding them open
    for minutes is what made pages hang. Start them with POST /jobs instead."""
    path = request.url.path.removeprefix("/api/v1")
    if request.method == "POST" and (label := long_running(path)) and current_job.get() is None:
        raise HTTPException(status_code=409, detail=f"{label} runs in the background: start it with POST /api/v1/jobs.")


def get_job_queue() -> Callable[[UUID], None]:
    """Hands a job to the Celery worker. Raises when the broker (Redis) can't be reached."""
    from tasks import run_job

    return lambda job_id: run_job.apply_async(args=[str(job_id)], retry=False)


def get_quickstart_queue() -> Callable[[UUID], None]:
    """Hands a Quick Start's next step to the Celery worker. Raises when the broker (Redis) can't be reached."""
    from tasks import quickstart_step

    return lambda run_id: quickstart_step.apply_async(args=[str(run_id)], retry=False)


def get_quickstart_repo(session: AsyncSession = Depends(get_session)) -> QuickstartRepo:
    return QuickstartRepo(session)


def get_smart_list_repo(session: AsyncSession = Depends(get_session)) -> SmartListRepo:
    return SmartListRepo(session)


def get_job_repo(session: AsyncSession = Depends(get_session)) -> JobRepo:
    return JobRepo(session)


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can do that.")
    return user


MANAGERS_ONLY = "Only Management and admins can do that: ICPs, prompts, products, example emails and anything that spends budget."


def require_manager(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Admin or Management (the Roles tab's "Campaign setup"): ICPs, prompts, products, example emails, the
    workspace's writing and research settings, and every run that spends budget. Sales reviews, approves,
    queues and adds leads. A Prospector's own Quick Start run gets through too, but only inside the worker and
    only on its LinkedIn steps (limit_prospectors refuses everything else)."""
    if user.role in ("admin", "management") or (user.role == "prospector" and current_job.get() is not None):
        return user
    raise HTTPException(status_code=403, detail=MANAGERS_ONLY)


def require_runner(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Management and admins, and Prospectors where the endpoint checks what's theirs (services/access.Scope): their
    own Quick Start runs (LinkedIn, under the workspace's cap), and the ICPs, products and smart lists they own or
    may edit."""
    if user.role in ("admin", "management", "prospector"):
        return user
    raise HTTPException(status_code=403, detail=MANAGERS_ONLY)


# What a Prospector may call from the browser. Each of these checks the item against what's theirs
# (services/access.Scope): their own Quick Start runs; the ICPs, products and smart lists they own or were shared; the
# companies their runs found or that are in their lists, and the people there. Inside their own run (the worker):
# the LinkedIn option's steps. Nothing else: no email writing, no Claude besides their runs, no jobs of their own.
PROSPECTOR_PAGES = {
    ("GET", "/me"), ("GET", "/workspaces"),
    ("GET", "/quickstart/limits"), ("GET", "/quickstart/runs"), ("POST", "/quickstart/runs"), ("GET", "/quickstart/runs/{run_id}"),
    ("GET", "/quickstart/runs/{run_id}/briefs"), ("POST", "/quickstart/runs/{run_id}/stop"),
    ("POST", "/quickstart/runs/{run_id}/resume"),
    ("GET", "/icps"), ("POST", "/icps"), ("GET", "/icps/{icp_id}"), ("PUT", "/icps/{icp_id}"), ("PUT", "/icps/{icp_id}/profile"),
    ("POST", "/icps/{icp_id}/archive"), ("POST", "/icps/{icp_id}/restore"),
    ("GET", "/products"), ("POST", "/products"), ("PUT", "/products/{product_id}"), ("DELETE", "/products/{product_id}"),
    ("GET", "/smart-lists"), ("POST", "/smart-lists"), ("GET", "/smart-lists/{list_id}"), ("PATCH", "/smart-lists/{list_id}"),
    ("DELETE", "/smart-lists/{list_id}"), ("GET", "/smart-lists/candidates"), ("GET", "/smart-lists/candidates/ids"),
    ("POST", "/smart-lists/move"),
    ("GET", "/pipeline/companies/page"), ("GET", "/pipeline/companies/matching"), ("GET", "/pipeline/companies/{company_id}"),
    ("GET", "/pipeline/companies/{company_id}/content"),
    ("GET", "/stakeholders/page"), ("GET", "/stakeholders/matching"), ("GET", "/stakeholders/{stakeholder_id}"),
    ("GET", "/posts"),
    # Their own saved filters (a workspace-wide one is only Management's to save or delete: routers/saved_views.py).
    ("GET", "/saved-views"), ("POST", "/saved-views"), ("DELETE", "/saved-views/{view_id}"),
    # The background activity menu: only their own jobs (routers/jobs.py shows non-admins only what they started).
    ("GET", "/jobs"), ("GET", "/jobs/{job_id}"),
    # What research and analysis can read, and its prices: the same for everyone (the ICP form lists them).
    ("GET", "/sources"),
}  # fmt: skip
PROSPECTOR_STEPS = {
    "/pipeline/find-linkedin", "/pipeline/web-search", "/pipeline/research", "/pipeline/stakeholders",
    "/stakeholders/add-as-leads", "/posts/fetch", "/pipeline/people-web", "/pipeline/briefs",
}  # fmt: skip
PROSPECTORS_ONLY = "Your account can only use Quick Start's LinkedIn search and the companies and people you find with it."


async def limit_prospectors(request: Request, user: CurrentUser = Depends(get_current_user)) -> None:
    """Every protected request: a Prospector gets only PROSPECTOR_PAGES, or PROSPECTOR_STEPS inside their run."""
    if user.role != "prospector":
        return
    route = request.scope.get("route")
    template = str(getattr(route, "path", request.url.path)).removeprefix("/api/v1")
    if current_job.get() is not None:
        if request.method == "POST" and template in PROSPECTOR_STEPS:
            return
    elif (request.method, template) in PROSPECTOR_PAGES:
        return
    raise HTTPException(status_code=403, detail=PROSPECTORS_ONLY)


def get_workspace_id(
    x_workspace_id: UUID | None = Header(default=None), user: CurrentUser = Depends(get_current_user)
) -> UUID | None:
    """The workspace the request is for, sent by the frontend as a header. Refused when the signed-in
    person can't open it; admins open every workspace."""
    if x_workspace_id is not None and not user.can_open(x_workspace_id):
        raise HTTPException(status_code=403, detail="You don't have access to this workspace. Ask an admin to add you.")
    return x_workspace_id


def require_workspace_id(workspace_id: UUID | None = Depends(get_workspace_id)) -> UUID:
    if workspace_id is None:
        raise HTTPException(status_code=400, detail="X-Workspace-Id header is required")
    return workspace_id


def get_access_repo(session: AsyncSession = Depends(get_session)) -> AccessRepo:
    return AccessRepo(session)


def get_scope(
    user: CurrentUser = Depends(get_current_user),
    workspace_id: UUID | None = Depends(get_workspace_id),
    repo: AccessRepo = Depends(get_access_repo),
) -> Scope:
    """What the signed-in person may see (services/access.py). A Prospector's every request names its workspace."""
    if user.role == "prospector" and workspace_id is None:
        raise HTTPException(status_code=400, detail="X-Workspace-Id header is required")
    return Scope(user, workspace_id, repo)  # type: ignore[arg-type]


def get_icp_campaign_repo(session: AsyncSession = Depends(get_session)) -> IcpCampaignRepo:
    return IcpCampaignRepo(session)


def get_icp_profile_repo(session: AsyncSession = Depends(get_session)) -> IcpProfileRepo:
    return IcpProfileRepo(session)


def get_lead_repo(session: AsyncSession = Depends(get_session)) -> LeadRepo:
    return LeadRepo(session)


def get_workspace_company_repo(session: AsyncSession = Depends(get_session)) -> WorkspaceCompanyRepo:
    return WorkspaceCompanyRepo(session)


def get_workspace_repo(session: AsyncSession = Depends(get_session)) -> WorkspaceRepo:
    return WorkspaceRepo(session)


def get_stakeholder_repo(session: AsyncSession = Depends(get_session)) -> StakeholderRepo:
    return StakeholderRepo(session)


def get_email_repo(session: AsyncSession = Depends(get_session)) -> EmailRepo:
    return EmailRepo(session)


async def get_optional_writer() -> AsyncIterator[EmailWriter | None]:
    """The email writer, or None when ANTHROPIC_API_KEY isn't set."""
    key = get_settings().anthropic_api_key
    if not key:
        yield None
        return
    writer = EmailWriter(key, base_url=get_settings().anthropic_base_url)
    try:
        yield writer
    finally:
        await writer.aclose()


def get_feedback_repo(session: AsyncSession = Depends(get_session)) -> FeedbackRepo:
    return FeedbackRepo(session)


async def get_optional_anthropic() -> AsyncIterator[anthropic.AsyncAnthropic | None]:
    key = get_settings().anthropic_api_key
    if not key:
        yield None
        return
    client = anthropic.AsyncAnthropic(api_key=key, base_url=get_settings().anthropic_base_url, max_retries=4, timeout=300.0)
    try:
        yield client
    finally:
        await client.close()


def get_settings_repo(session: AsyncSession = Depends(get_session)) -> SettingsRepo:
    return SettingsRepo(session)


def get_product_repo(session: AsyncSession = Depends(get_session)) -> ProductRepo:
    return ProductRepo(session)


def get_example_repo(session: AsyncSession = Depends(get_session)) -> ExampleRepo:
    return ExampleRepo(session)


def get_prompt_repo(session: AsyncSession = Depends(get_session)) -> PromptRepo:
    return PromptRepo(session)


def get_provider_call_repo(session: AsyncSession = Depends(get_session)) -> ProviderCallRepo:
    return ProviderCallRepo(session)


async def get_apollo() -> AsyncIterator[ApolloClient]:
    key = get_settings().apollo_api_key
    if not key:
        raise HTTPException(status_code=503, detail="APOLLO_API_KEY is not set")
    client = ApolloClient(key, base_url=get_settings().apollo_api_url)
    try:
        yield client
    finally:
        await client.aclose()


def get_lead_intel_icp_writer(conn: AsyncConnection = Depends(get_lead_intel_icp_conn)) -> LeadIntelIcpWriter:
    return LeadIntelIcpWriter(conn)


def get_post_repo(session: AsyncSession = Depends(get_session)) -> PostRepo:
    return PostRepo(session)


async def get_apify() -> AsyncIterator[ApifyClient]:
    token = get_settings().apify_token
    if not token:
        raise HTTPException(status_code=503, detail="APIFY_TOKEN is not set")
    client = ApifyClient(token, base_url=get_settings().apify_api_url)
    try:
        yield client
    finally:
        await client.aclose()


def get_company_repo(session: AsyncSession = Depends(get_session)) -> CompanyRepo:
    return CompanyRepo(session)


async def get_optional_apollo() -> AsyncIterator[ApolloClient | None]:
    key = get_settings().apollo_api_key
    client = ApolloClient(key, base_url=get_settings().apollo_api_url) if key else None
    try:
        yield client
    finally:
        if client:
            await client.aclose()


async def get_optional_apify() -> AsyncIterator[ApifyClient | None]:
    token = get_settings().apify_token
    client = ApifyClient(token, base_url=get_settings().apify_api_url) if token else None
    try:
        yield client
    finally:
        if client:
            await client.aclose()


async def get_optional_places() -> AsyncIterator[PlacesClient | None]:
    key = get_settings().google_places_api_key
    client = PlacesClient(key, endpoint=get_settings().google_places_url) if key else None
    try:
        yield client
    finally:
        if client:
            await client.aclose()


async def get_supabase_admin() -> AsyncIterator[SupabaseAdmin | None]:
    settings = get_settings()
    ready = settings.supabase_url and settings.supabase_service_role_key
    client = SupabaseAdmin(settings.supabase_url, settings.supabase_service_role_key) if ready else None  # type: ignore[arg-type]
    try:
        yield client
    finally:
        if client:
            await client.aclose()


async def get_optional_sequencer() -> AsyncIterator[SequencerClient | None]:
    key = get_settings().cheapsequencer_api_key
    client = SequencerClient(key, base_url=get_settings().cheapsequencer_api_url) if key else None
    try:
        yield client
    finally:
        if client:
            await client.aclose()


async def get_optional_inboxes() -> AsyncIterator[InboxesClient | None]:
    key = get_settings().cheapinboxes_api_key
    client = InboxesClient(key, base_url=get_settings().cheapinboxes_api_url) if key else None
    try:
        yield client
    finally:
        if client:
            await client.aclose()


async def get_website_reader() -> AsyncIterator[WebsiteReader]:
    settings = get_settings()
    jina = JinaClient(settings.jina_api_key, settings.jina_rpm, reader_url=settings.jina_reader_url)
    reader = WebsiteReader(jina=jina, bot_info_url=settings.bot_info_url)
    try:
        yield reader
    finally:
        await reader.aclose()
        await jina.aclose()


async def get_optional_brave() -> AsyncIterator[BraveSearch | None]:
    """Web search through Brave's Search API (BRAVE_API_KEY), or None when it isn't set."""
    key = get_settings().brave_api_key
    client = BraveSearch(key) if key else None
    try:
        yield client
    finally:
        if client:
            await client.aclose()


async def get_optional_brief_writer() -> AsyncIterator[BriefWriter | None]:
    settings = get_settings()
    writer = BriefWriter(settings.anthropic_api_key, settings.claude_brief_model, base_url=settings.anthropic_base_url) if settings.anthropic_api_key else None
    try:
        yield writer
    finally:
        if writer:
            await writer.aclose()


async def get_optional_scorer() -> AsyncIterator[CompanyScorer | None]:
    settings = get_settings()
    scorer = (
        CompanyScorer(settings.anthropic_api_key, settings.claude_scoring_model, base_url=settings.anthropic_base_url)
        if settings.anthropic_api_key
        else None
    )
    try:
        yield scorer
    finally:
        if scorer:
            await scorer.aclose()
