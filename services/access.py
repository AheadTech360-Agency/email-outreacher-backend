"""Who may see what, for Prospectors. Everyone else (Sales, Management, admins) sees the whole workspace, as before.

A Prospector sees:
  - the ICPs, products and smart lists they created (they own those), and those an admin shared with them, editable
    or not (`outreach.eo_access`, one row per person and item);
  - the companies their own Quick Start runs found, and the companies in the smart lists they can see;
  - the people at those companies, and the people in the person lists they can see.

Every endpoint a Prospector may call (dependencies.PROSPECTOR_PAGES) checks the item through `Scope` before reading or
changing it. Something they can't see answers 404, as if it weren't there; something they can see but not edit, 403.
"""

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import Access, Company, QuickstartRun, SmartListMember, Stakeholder
from services.auth import CurrentUser

Kind = Literal["icp", "product", "smart_list"]
NAMES = {"icp": "ICP", "product": "Product", "smart_list": "Smart list"}
READ_ONLY = "It was shared with you to use, not to change. Ask an admin to let you edit it."
NOT_YOURS = "Only the person who created it can delete it."


class AccessRepo:
    """`outreach.eo_access`: which ICPs, products and smart lists each Prospector may see, and edit."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def rows(self, user_id: UUID, kind: str | None = None, workspace_id: UUID | None = None) -> list[Access]:
        """Their rows; with workspace_id, only that workspace's (what they may use there)."""
        query = select(Access).where(Access.user_id == user_id)
        if kind:
            query = query.where(Access.kind == kind)
        if workspace_id is not None:
            query = query.where(Access.workspace_id == workspace_id)
        return list(await self.session.scalars(query.order_by(Access.kind, Access.created_at)))

    async def get(self, user_id: UUID, kind: str, resource_id: UUID, workspace_id: UUID) -> Access | None:
        """Their access to this item in this workspace (sharing in one workspace says nothing about another)."""
        return await self.session.scalar(
            select(Access).where(Access.user_id == user_id, Access.kind == kind, Access.resource_id == resource_id,
                                 Access.workspace_id == workspace_id)
        )  # fmt: skip

    async def own(self, user_id: UUID, workspace_id: UUID, kind: str, resource_id: UUID) -> None:
        """The person who just created this item owns it: they see it, edit it and may delete it."""
        await self.session.execute(
            insert(Access)
            .values(user_id=user_id, workspace_id=workspace_id, kind=kind, resource_id=resource_id, can_edit=True, is_owner=True,
                    granted_by=user_id)
            .on_conflict_do_update(index_elements=[Access.user_id, Access.kind, Access.resource_id],
                                   set_={"can_edit": True, "is_owner": True, "workspace_id": workspace_id})
        )  # fmt: skip
        await self.session.commit()

    async def replace_shared(self, user_id: UUID, grants: list[dict[str, Any]], granted_by: UUID) -> None:
        """What an admin shared with this person, all at once: the shared rows are replaced, the ones they own kept
        (an owner can't be made read-only by leaving their item out)."""
        await self.session.execute(delete(Access).where(Access.user_id == user_id, Access.is_owner.is_(False)))
        owned = {(a.kind, a.resource_id) for a in await self.rows(user_id)}
        for g in grants:
            if (g["kind"], g["resource_id"]) in owned:
                continue
            self.session.add(Access(user_id=user_id, workspace_id=g["workspace_id"], kind=g["kind"], resource_id=g["resource_id"],
                                    can_edit=bool(g["can_edit"]), is_owner=False, granted_by=granted_by))  # fmt: skip
        await self.session.commit()

    async def forget(self, kind: str, resource_id: UUID) -> None:
        """The item is gone: nobody's access to it is kept."""
        await self.session.execute(delete(Access).where(Access.kind == kind, Access.resource_id == resource_id))
        await self.session.commit()


@dataclass
class Scope:
    """What the signed-in person may see in this workspace. `limited` is False for everyone but Prospectors, and then
    every check passes and every filter is None (the whole workspace)."""

    user: CurrentUser
    workspace_id: UUID
    repo: AccessRepo

    @property
    def limited(self) -> bool:
        return self.user.role == "prospector"

    # ---------- ICPs, products, smart lists ----------

    async def ids(self, kind: Kind) -> set[UUID] | None:
        """The items of this kind they may see; None: all of them."""
        if not self.limited:
            return None
        return {a.resource_id for a in await self.repo.rows(self.user.id, kind, self.workspace_id)}

    async def check(self, kind: Kind, resource_id: UUID | None, *, edit: bool = False, owner: bool = False) -> None:
        """404 when they can't see it, 403 when they may see it but not change (or delete) it."""
        if not self.limited or resource_id is None:
            return
        row = await self.repo.get(self.user.id, kind, resource_id, self.workspace_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"{NAMES[kind]} not found")
        if owner and not row.is_owner:
            raise HTTPException(status_code=403, detail=NOT_YOURS)
        if edit and not (row.can_edit or row.is_owner):
            raise HTTPException(status_code=403, detail=READ_ONLY)

    async def can_edit(self, kind: Kind, resource_id: UUID | None) -> bool:
        if not self.limited:
            return True
        if resource_id is None:
            return True
        row = await self.repo.get(self.user.id, kind, resource_id, self.workspace_id)
        return bool(row and (row.can_edit or row.is_owner))

    async def is_owner(self, kind: Kind, resource_id: UUID) -> bool:
        """Whether they may delete (or archive) it: everyone but a Prospector, and a Prospector for what they created."""
        if not self.limited:
            return True
        row = await self.repo.get(self.user.id, kind, resource_id, self.workspace_id)
        return bool(row and row.is_owner)

    async def flags(self, kind: Kind, resource_id: UUID) -> dict[str, bool]:
        """can_edit and is_owner, as the pages show them."""
        return {"can_edit": await self.can_edit(kind, resource_id), "is_owner": await self.is_owner(kind, resource_id)}

    async def created(self, kind: Kind, resource_id: UUID) -> None:
        """A Prospector made this: it's theirs. (Others see everything already.)"""
        if self.limited:
            await self.repo.own(self.user.id, self.workspace_id, kind, resource_id)

    # ---------- Companies and people ----------

    def _found(self) -> Any:
        """The companies their own Quick Start runs found."""
        return select(func.unnest(QuickstartRun.company_ids)).where(
            QuickstartRun.workspace_id == self.workspace_id, QuickstartRun.created_by == self.user.id
        )

    def _listed(self, kind: str) -> Any:
        """The companies (or people) in the smart lists they can see."""
        lists = select(Access.resource_id).where(Access.user_id == self.user.id, Access.kind == "smart_list",
                                                 Access.workspace_id == self.workspace_id)  # fmt: skip
        return select(SmartListMember.member_id).where(
            SmartListMember.workspace_id == self.workspace_id, SmartListMember.kind == kind, SmartListMember.list_id.in_(lists)
        )

    def companies(self, column: Any = Company.id) -> Any | None:
        """A condition on a company id column: only the companies they may see. None: no limit."""
        if not self.limited:
            return None
        return or_(column.in_(self._found()), column.in_(self._listed("company")))

    def people(self) -> Any | None:
        """A condition on people: those at the companies they may see, and those in the person lists they can see."""
        if not self.limited:
            return None
        return or_(self.companies(Stakeholder.company_id), Stakeholder.id.in_(self._listed("person")))

    async def check_company(self, session: AsyncSession, company_id: UUID) -> None:
        if (clause := self.companies()) is None:
            return
        if not await session.scalar(select(Company.id).where(Company.id == company_id, Company.workspace_id == self.workspace_id, clause)):
            raise HTTPException(status_code=404, detail="Company not found")

    async def check_person(self, session: AsyncSession, person_id: UUID) -> None:
        if (clause := self.people()) is None:
            return
        found = await session.scalar(select(Stakeholder.id).where(Stakeholder.id == person_id, Stakeholder.workspace_id == self.workspace_id, clause))
        if not found:
            raise HTTPException(status_code=404, detail="Person not found")

    async def visible_members(self, session: AsyncSession, kind: str, ids: list[UUID]) -> list[UUID]:
        """Which of these companies or people they may see (all of them for everyone else)."""
        clause = self.companies() if kind == "company" else self.people()
        if clause is None or not ids:
            return ids
        model = Company if kind == "company" else Stakeholder
        found = set(await session.scalars(select(model.id).where(model.id.in_(ids), model.workspace_id == self.workspace_id, clause)))
        return [i for i in ids if i in found]
