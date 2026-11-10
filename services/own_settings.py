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
    # LinkedIn and Instagram: per account per day, unless the account has its own limit
    linkedin_daily_invites: int = 20
    linkedin_weekly_invites: int = 100
    linkedin_daily_messages: int = 50
    instagram_daily_messages: int = 30
    social_min_gap_minutes: int = 3
    social_max_gap_minutes: int = 10

    def social_limits(self, provider: str, daily_invite_limit: int | None = None, daily_message_limit: int | None = None) -> dict:
        """What one account may do: its own limits where it has them, this workspace's for its network otherwise.
        Instagram has no connection requests (invites 0)."""
        if provider == "linkedin":
            return {"invites_per_day": min(daily_invite_limit or self.linkedin_daily_invites, self.linkedin_weekly_invites),
                    "invites_per_week": self.linkedin_weekly_invites, "messages_per_day": daily_message_limit or self.linkedin_daily_messages}  # fmt: skip
        return {"invites_per_day": 0, "invites_per_week": 0, "messages_per_day": daily_message_limit or self.instagram_daily_messages}

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
