"""The sending dashboard's Settings: each workspace's sending limits and warm-up settings (services/own_settings.py).
Anyone in the workspace can read them; Management and admins change them. Whether anything sends for real is the
server's switch (OWN_SENDING_LIVE, OWN_WARMUP_LIVE), shown here but not changed here."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from config import get_settings
from database import get_session
from dependencies import require_manager, require_workspace_id
from schemas import ApiModel
from services import own_settings
from services.auth import CurrentUser
from services.own_mail import clean_schedule

router = APIRouter(tags=["own sending"])

CLOCK = r"^([01]\d|2[0-3]):[0-5]\d$"


class SettingsIn(ApiModel):
    sending_paused: bool = False
    default_daily_limit: int = Field(default=30, ge=1, le=500)
    workspace_daily_cap: int | None = Field(default=None, ge=1, le=100000)
    min_gap_minutes: int = Field(default=5, ge=1, le=240)
    max_gap_minutes: int = Field(default=15, ge=1, le=240)
    warmup_enabled: bool = True
    warmup_timezone: str = Field(default="America/New_York", max_length=64)
    warmup_start: str = Field(default="09:00", pattern=CLOCK)
    warmup_end: str = Field(default="19:00", pattern=CLOCK)
    warmup_days: list[int] = Field(default=[1, 2, 3, 4, 5, 6, 7], min_length=1, max_length=7)
    warmup_start_per_day: int = Field(default=3, ge=1, le=50)
    warmup_ramp_per_day: int = Field(default=2, ge=0, le=20)
    warmup_default_target: int = Field(default=20, ge=1, le=100)
    warmup_default_reply_percent: int = Field(default=30, ge=0, le=100)
    warmup_star_percent: int = Field(default=25, ge=0, le=100)


class SettingsOut(SettingsIn):
    # The server's switches: off, everything is a dry run whatever these settings say.
    sending_live: bool
    warmup_live: bool


def _out(s: own_settings.Settings) -> SettingsOut:
    server = get_settings()
    return SettingsOut(**s.as_dict(), sending_live=server.own_sending_live, warmup_live=server.own_warmup_live)


@router.get("/own-sending/settings", response_model=SettingsOut)
async def get_own_settings(workspace_id: UUID = Depends(require_workspace_id), session=Depends(get_session)):
    return _out(await own_settings.load(session, workspace_id))


@router.put("/own-sending/settings", response_model=SettingsOut)
async def save_own_settings(body: SettingsIn, workspace_id: UUID = Depends(require_workspace_id),
                            _user: CurrentUser = Depends(require_manager), session=Depends(get_session)):  # fmt: skip
    if body.min_gap_minutes > body.max_gap_minutes:
        raise HTTPException(422, "The shortest gap between sends can't be longer than the longest.")
    try:
        window = clean_schedule({"timezone": body.warmup_timezone, "start": body.warmup_start, "end": body.warmup_end, "days": body.warmup_days})
    except ValueError as exc:
        raise HTTPException(422, f"Warm-up window: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - an unknown timezone
        raise HTTPException(422, "That timezone isn't recognised.") from exc
    values = own_settings.Settings(**{**body.model_dump(), "warmup_days": window["days"]})
    return _out(await own_settings.save(session, workspace_id, values))
