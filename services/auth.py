"""Signing in: Supabase Auth issues the session, this checks it.

The frontend signs in with Supabase (email and password; there is no sign-up page, accounts are
made in the Supabase dashboard) and sends the session's access token as `Authorization: Bearer`.
Here the token's signature is checked against the project's public keys (ES256, fetched from its
JWKS endpoint and cached), along with who issued it, who it's for, and that it hasn't expired.

Supabase Auth is shared with Lead Intel, so any account there can sign in. What it may do here is
decided by our own `eo_users` row: its role and workspaces. That's separate from Lead Intel's roles.
"""

import asyncio
from dataclasses import dataclass, field
from functools import lru_cache
from uuid import UUID

import jwt

AUDIENCE = "authenticated"
CLOCK_LEEWAY = 60  # seconds


class AuthError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class TokenClaims:
    auth_user_id: UUID
    email: str


@dataclass
class CurrentUser:
    """The signed-in person, as this app sees them."""

    id: UUID
    email: str
    name: str
    role: str  # sales | management | admin | prospector
    workspace_ids: list[UUID] = field(default_factory=list)
    # A Prospector's: what an admin lets them do (schemas.ProspectorPermissions, as stored).
    permissions: dict = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_prospector(self) -> bool:
        return self.role == "prospector"

    def can_open(self, workspace_id: UUID) -> bool:
        return self.is_admin or workspace_id in self.workspace_ids


@lru_cache
def _jwks(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)


async def verify(token: str, jwks_url: str, issuer: str) -> TokenClaims:
    """The claims of a valid access token. Raises AuthError with a reason a person can act on."""
    try:
        # Fetching the keys is a blocking HTTP call on a cache miss, so it runs off the event loop.
        key = await asyncio.to_thread(_jwks(jwks_url).get_signing_key_from_jwt, token)
        # Leeway: a server clock a second behind Supabase's would otherwise refuse a token issued just now.
        claims = jwt.decode(token, key.key, algorithms=["ES256", "RS256"], audience=AUDIENCE, issuer=issuer, leeway=CLOCK_LEEWAY)
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Your session has expired. Sign in again.") from exc
    except jwt.PyJWKClientError as exc:
        raise AuthError("Couldn't check the sign-in: Supabase's signing keys weren't available.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("That sign-in isn't valid. Sign in again.") from exc
    email = (claims.get("email") or "").strip().lower()
    if not claims.get("sub") or not email:
        raise AuthError("That sign-in has no account email.")
    return TokenClaims(UUID(claims["sub"]), email)
