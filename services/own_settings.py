"""A workspace's sending limits and warm-up settings (eo_own_sending_settings; the dashboard's Settings page). A
workspace that never saved any uses DEFAULTS. Live sending is a server switch (OWN_SENDING_LIVE, OWN_WARMUP_LIVE):
nothing here can make anything send, only hold it back."""

from dataclasses import asdict, dataclass, field, fields
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import OwnSendingSettings


@dataclass
class Settings:
    # Sending (campaigns)
    sending_paused: bool = False
    default_daily_limit: int = 30
    workspace_daily_cap: int | None = None
    min_gap_minutes: int = 5
    max_gap_minutes: int = 15
    # Warming
    warmup_enabled: bool = True
    warmup_timezone: str = "America/New_York"
    warmup_start: str = "09:00"
    warmup_end: str = "19:00"
    warmup_days: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7])
    warmup_start_per_day: int = 3
    warmup_ramp_per_day: int = 2
    warmup_default_target: int = 20
    warmup_default_reply_percent: int = 30
    warmup_star_percent: int = 25

    @property
    def warmup_window(self) -> dict:
        """The warm-up window as a campaign-style schedule (services/own_mail.in_window)."""
        return {"timezone": self.warmup_timezone, "start": self.warmup_start, "end": self.warmup_end, "days": list(self.warmup_days)}

    def as_dict(self) -> dict:
        return asdict(self)


NAMES = [f.name for f in fields(Settings)]
DEFAULTS = Settings()


def from_row(row: OwnSendingSettings | None) -> Settings:
    return Settings(**{n: getattr(row, n) for n in NAMES}) if row is not None else Settings()


async def load(session: AsyncSession, workspace_id: UUID) -> Settings:
    return from_row(await session.get(OwnSendingSettings, workspace_id))


async def load_all(session: AsyncSession) -> dict[UUID, Settings]:
    """Every workspace that saved settings; any other uses DEFAULTS (get with .get(ws, DEFAULTS))."""
    return {r.workspace_id: from_row(r) for r in await session.scalars(select(OwnSendingSettings))}


async def save(session: AsyncSession, workspace_id: UUID, values: Settings) -> Settings:
    row = await session.get(OwnSendingSettings, workspace_id)
    if row is None:
        row = OwnSendingSettings(workspace_id=workspace_id)
        session.add(row)
    for name in NAMES:
        setattr(row, name, getattr(values, name))
    await session.commit()
    return values
