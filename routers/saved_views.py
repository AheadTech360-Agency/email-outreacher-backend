"""Account-private views and manager-owned workspace views."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field, field_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from dependencies import get_current_user, require_workspace_id
from models import SavedView, Workspace
from schemas import ApiModel
from services.auth import CurrentUser

router = APIRouter(prefix="/saved-views", tags=["saved views"])


class SaveViewIn(ApiModel):
    page: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=60)
    visibility: Literal["personal", "workspace"] = "personal"
    filters: dict[str, str] = Field(max_length=50)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value):
        if not value.strip():
            raise ValueError("Enter a view name")
        return value.strip()

    @field_validator("filters")
    @classmethod
    def bounded_filters(cls, values):
        if any(len(key) > 80 or len(value) > 2000 for key, value in values.items()):
            raise ValueError("Filter values are too long")
        return values


def visible_to(user):
    return or_(SavedView.visibility == "workspace", and_(SavedView.visibility == "personal", SavedView.user_id == user.id))


def output(row, user):
    return {"id": str(row.id), "name": row.name, "filters": row.filters, "visibility": row.visibility,
            "canManage": user.role in ("admin", "management") if row.visibility == "workspace" else row.user_id == user.id}


@router.get("")
async def list_views(page: str = Query(min_length=1, max_length=80), workspace_id: UUID = Depends(require_workspace_id),
                     user: CurrentUser = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(select(SavedView).where(SavedView.workspace_id == workspace_id,
        SavedView.page == page, visible_to(user)).order_by(SavedView.name, SavedView.id))
    return [output(row, user) for row in rows]


@router.post("")
async def save_view(body: SaveViewIn, workspace_id: UUID = Depends(require_workspace_id),
                    user: CurrentUser = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if body.visibility == "workspace" and user.role not in ("admin", "management"):
        raise HTTPException(403, "Only Management and admins can save workspace views.")
    # Serialize saves within a workspace so concurrent requests cannot exceed the limit or duplicate names.
    if await session.scalar(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()) is None:
        raise HTTPException(404, "Workspace not found")
    query = select(SavedView).where(SavedView.workspace_id == workspace_id, SavedView.page == body.page,
                                   SavedView.visibility == body.visibility)
    if body.visibility == "personal":
        query = query.where(SavedView.user_id == user.id)
    rows = list(await session.scalars(query))
    row = next((v for v in rows if v.name.casefold() == body.name.casefold()), None)
    if row is None:
        if len(rows) >= 20:
            raise HTTPException(409, "Up to 20 views per page and save location. Delete a view first.")
        row = SavedView(workspace_id=workspace_id, user_id=user.id, page=body.page,
                        visibility=body.visibility, name=body.name, filters=body.filters)
        session.add(row)
    else:
        row.name, row.filters = body.name, body.filters
    await session.commit()
    await session.refresh(row)
    return output(row, user)


@router.delete("/{view_id}", status_code=204)
async def delete_view(view_id: UUID, workspace_id: UUID = Depends(require_workspace_id),
                      user: CurrentUser = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    row = await session.scalar(select(SavedView).where(SavedView.workspace_id == workspace_id,
                               SavedView.id == view_id, visible_to(user)).with_for_update())
    if row is None:
        raise HTTPException(404, "Saved view not found")
    if row.visibility == "workspace" and user.role not in ("admin", "management"):
        raise HTTPException(403, "Only Management and admins can delete workspace views.")
    await session.delete(row)
    await session.commit()
