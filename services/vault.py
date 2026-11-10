"""Sender-account passwords in Supabase Vault: the only code that writes or reads vault.secrets.

Vault encrypts each secret at rest with a key Supabase holds outside the database; our eo_sender_accounts row keeps
only the secret's id. A password is read back only here, server-side, at the moment a mailbox is signed in to, and it
is never returned by any API. Approved by the user on 2026-11-10 ("Supabase Vault"), for these credentials only.

Every call runs in the caller's session, so a password and the account it belongs to are saved, or rolled back,
together.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PREFIX = "coldbrew/sender"


def secret_name(account_id: UUID, kind: str) -> str:
    """Vault names are unique: one per account and kind (smtp | imap)."""
    return f"{PREFIX}/{account_id}/{kind}"


class Vault:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, secret: str, name: str, description: str = "") -> UUID:
        return await self.session.scalar(
            text("select vault.create_secret(:secret, :name, :description)"),
            {"secret": secret, "name": name, "description": description},
        )

    async def update(self, secret_id: UUID, secret: str) -> None:
        await self.session.execute(text("select vault.update_secret(:id, :secret)"), {"id": secret_id, "secret": secret})

    async def read(self, secret_id: UUID | None) -> str | None:
        if secret_id is None:
            return None
        return await self.session.scalar(text("select decrypted_secret from vault.decrypted_secrets where id = :id"), {"id": secret_id})

    async def delete(self, secret_id: UUID | None) -> None:
        """Only a Coldbrew sender secret, by its id and name prefix: never anyone else's."""
        if secret_id is not None:
            await self.session.execute(
                text("delete from vault.secrets where id = :id and name like :prefix"), {"id": secret_id, "prefix": f"{PREFIX}/%"}
            )

    async def put(self, current: UUID | None, secret: str, name: str, description: str = "") -> UUID:
        """Save a password: a new secret, or the account's existing one replaced."""
        if current is None:
            return await self.create(secret, name, description)
        await self.update(current, secret)
        return current
