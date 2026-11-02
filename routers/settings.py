"""Workspace settings: the defaults, review and auto-send values, how many rated emails Claude learns from, how
much of a website research reads, and the cost estimator. All kept in the database for the whole workspace."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from config import get_settings
from crud import PromptRepo, ProviderCallRepo, SettingsRepo
from dependencies import (
    get_optional_apify, get_prompt_repo, get_provider_call_repo, get_settings_repo, require_manager, require_workspace_id,
)  # fmt: skip
from schemas import (
    ClaudePrice, CompanyApprovalSettings, CostEstimate, CostReference, FeedbackSettings, GeneralSettings, MeasuredUse, ResearchSettings, ScoringSettings,
)  # fmt: skip
from services.auth import CurrentUser
from services.apify import ApifyClient, ApifyError
from services.claude import PRICES
from services.cost_estimate import refresh
from services.sources import apify_prices

router = APIRouter(prefix="/settings", tags=["settings"])


async def _workspace(settings: SettingsRepo, workspace_id: UUID) -> None:
    if not await settings.workspace_exists(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")


@router.get("/feedback", response_model=FeedbackSettings)
async def get_feedback_settings(
    workspace_id: UUID = Depends(require_workspace_id), settings: SettingsRepo = Depends(get_settings_repo)
) -> FeedbackSettings:
    await _workspace(settings, workspace_id)
    return FeedbackSettings(**await settings.feedback(workspace_id))


@router.put("/feedback", response_model=FeedbackSettings, dependencies=[Depends(require_manager)])
async def save_feedback_settings(
    body: FeedbackSettings,
    workspace_id: UUID = Depends(require_workspace_id),
    settings: SettingsRepo = Depends(get_settings_repo),
) -> FeedbackSettings:
    await _workspace(settings, workspace_id)
    return FeedbackSettings(**await settings.save_feedback(workspace_id, body.model_dump()))


ADMINS_ONLY = "Only admins can change the monthly budget, auto-send and email scoring."


def _general_out(values: dict) -> GeneralSettings:
    return GeneralSettings(
        default_model=values["default_model"], posts_per_lead=values["posts_per_lead"], max_post_age_days=values["max_post_age_days"],
        monthly_budget_usd=values["monthly_budget_usd"],
        scoring=ScoringSettings(prompt_id=values["scoring_prompt_id"], threshold=values["score_threshold"],
                                auto_send=values["auto_send"], daily_auto_send_cap=values["daily_auto_send_cap"]),
        company_approval=CompanyApprovalSettings(threshold=values["company_threshold"], auto_approve=values["company_auto_approve"]),
    )  # fmt: skip


@router.get("/general", response_model=GeneralSettings)
async def get_general_settings(
    workspace_id: UUID = Depends(require_workspace_id), settings: SettingsRepo = Depends(get_settings_repo)
) -> GeneralSettings:
    await _workspace(settings, workspace_id)
    return _general_out(await settings.general(workspace_id))


@router.put("/general", response_model=GeneralSettings)
async def save_general_settings(
    body: GeneralSettings,
    workspace_id: UUID = Depends(require_workspace_id),
    user: CurrentUser = Depends(require_manager),
    settings: SettingsRepo = Depends(get_settings_repo),
    prompts: PromptRepo = Depends(get_prompt_repo),
) -> GeneralSettings:
    """Save them for everyone in the workspace. Management changes the model and posts; only admins change the
    budget, auto-send and scoring."""
    await _workspace(settings, workspace_id)
    now = await settings.general(workspace_id)
    values = {
        "default_model": body.default_model, "posts_per_lead": body.posts_per_lead, "max_post_age_days": body.max_post_age_days,
        "monthly_budget_usd": body.monthly_budget_usd, "score_threshold": body.scoring.threshold, "auto_send": body.scoring.auto_send,
        "daily_auto_send_cap": body.scoring.daily_auto_send_cap, "scoring_prompt_id": body.scoring.prompt_id,
        "company_threshold": body.company_approval.threshold if body.company_approval else now["company_threshold"],
        "company_auto_approve": body.company_approval.auto_approve if body.company_approval else now["company_auto_approve"],
    }  # fmt: skip
    admin_fields = ("monthly_budget_usd", "score_threshold", "auto_send", "daily_auto_send_cap", "scoring_prompt_id")
    if not user.is_admin and any(values[k] != now[k] for k in admin_fields):
        raise HTTPException(status_code=403, detail=ADMINS_ONLY)
    if body.scoring.prompt_id is not None:
        prompt = await prompts.get(workspace_id, body.scoring.prompt_id)
        if prompt is None or prompt.kind != "score":
            raise HTTPException(status_code=422, detail="Pick one of this workspace's email scoring prompts.")
    return _general_out(await settings.save_general(workspace_id, values))


@router.get("/research", response_model=ResearchSettings)
async def get_research_settings(
    workspace_id: UUID = Depends(require_workspace_id), settings: SettingsRepo = Depends(get_settings_repo)
) -> ResearchSettings:
    await _workspace(settings, workspace_id)
    return ResearchSettings(**await settings.research(workspace_id))


@router.put("/research", response_model=ResearchSettings, dependencies=[Depends(require_manager)])
async def save_research_settings(
    body: ResearchSettings,
    workspace_id: UUID = Depends(require_workspace_id),
    settings: SettingsRepo = Depends(get_settings_repo),
) -> ResearchSettings:
    await _workspace(settings, workspace_id)
    return ResearchSettings(**await settings.save_research(workspace_id, body.model_dump()))


async def _priced(estimate: dict, apify: ApifyClient | None) -> dict:
    config = get_settings()
    prices = None
    if apify is not None:
        try:
            prices = (await apify_prices(apify))[1]
        except ApifyError:
            prices = None  # Apify unreachable: the starting prices stand in
    return refresh(estimate, config.claude_scoring_model, config.cost_price_overrides, prices)


@router.get("/cost-estimate", response_model=CostEstimate)
async def get_cost_estimate(
    workspace_id: UUID = Depends(require_workspace_id),
    settings: SettingsRepo = Depends(get_settings_repo),
    apify: ApifyClient | None = Depends(get_optional_apify),
) -> CostEstimate:
    """The cost estimator's inputs and cost items: the workspace's saved ones, or the starting ones."""
    await _workspace(settings, workspace_id)
    return CostEstimate(**await _priced(await settings.cost_estimate(workspace_id), apify))


@router.put("/cost-estimate", response_model=CostEstimate)
async def save_cost_estimate(
    body: CostEstimate,
    workspace_id: UUID = Depends(require_workspace_id),
    settings: SettingsRepo = Depends(get_settings_repo),
    apify: ApifyClient | None = Depends(get_optional_apify),
    _: CurrentUser = Depends(require_manager),
) -> CostEstimate:
    await _workspace(settings, workspace_id)
    if len({item.id for item in body.items}) != len(body.items):
        raise HTTPException(status_code=422, detail="Each cost item needs its own id")
    # Prices are read-only: what's saved is today's price, whatever the browser sent.
    priced = await _priced(body.model_dump(), apify)
    return CostEstimate(**await _priced(await settings.save_cost_estimate(workspace_id, priced), apify))


MEASURED_DAYS = 90


@router.get("/cost-estimate/reference", response_model=CostReference)
async def cost_reference(
    workspace_id: UUID = Depends(require_workspace_id), calls: ProviderCallRepo = Depends(get_provider_call_repo)
) -> CostReference:
    """What the estimator prices Claude with: each model's price per token, and the workspace's measured
    tokens per use of each Claude operation over the last 90 days."""
    since = datetime.now(UTC) - timedelta(days=MEASURED_DAYS)
    return CostReference(
        claude_prices=[ClaudePrice(model=m, input_per_mtok=p_in, output_per_mtok=p_out) for m, (p_in, p_out) in PRICES.items()],
        scoring_model=get_settings().claude_scoring_model,
        measured=[MeasuredUse(**m) for m in await calls.claude_per_use(workspace_id, since)],
        since=since,
    )
