"""Who signs in and what they can open.

Accounts and passwords live in Supabase Auth, shared with Lead Intel, with public sign-up off. An admin
adds someone here with their role, workspaces and a first password: that makes their sign-in too
(services/supabase_admin.py), unless the email already has one, which they keep using. Only people added
here get in (dependencies.get_current_user); a Supabase account on its own isn't enough. Here: each
person's role and workspaces, in `eo_users` and `eo_workspace_members`.

Removing someone here only removes them from this app. Their Supabase account, which Lead Intel
shares, is left alone.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from crud import ProductRepo, SmartListRepo, UserRepo
from dependencies import (
    forget_sign_ins, get_access_repo, get_current_user, get_lead_intel_repo, get_product_repo, get_smart_list_repo, get_supabase_admin,
    get_user_repo, require_admin,
)  # fmt: skip
from lead_intel import LeadIntelRepo
from schemas import AccessIn, AccessOut, UserAdded, UserIn, UserOut, UserUpdate, permissions_of
from services.access import AccessRepo
from services.supabase_admin import SupabaseAdmin, SupabaseAdminError
from services.auth import CurrentUser

router = APIRouter(tags=["users"])


def _out(user, workspace_ids: list[UUID]) -> UserOut:
    return UserOut.model_validate({
        "id": user.id, "name": user.name, "email": user.email, "role": user.role, "status": user.status,
        "workspace_ids": workspace_ids, "last_active_at": user.last_active_at, "created_at": user.created_at,
        "permissions": permissions_of(user.permissions),
    })  # fmt: skip


async def _active_admins(users: UserRepo, except_id: UUID) -> int:
    return sum(1 for u in await users.list() if u.role == "admin" and u.status != "deactivated" and u.id != except_id)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser = Depends(get_current_user), users: UserRepo = Depends(get_user_repo)) -> UserOut:
    row = await users.get(user.id)
    return _out(row, user.workspace_ids)


@router.get("/users", response_model=list[UserOut])
async def list_users(_: CurrentUser = Depends(require_admin), users: UserRepo = Depends(get_user_repo)) -> list[UserOut]:
    members = await users.members()
    return [_out(u, members.get(u.id, [])) for u in await users.list()]


@router.post("/users", response_model=UserAdded, status_code=201)
async def add_user(
    body: UserIn,
    _: CurrentUser = Depends(require_admin),
    users: UserRepo = Depends(get_user_repo),
    supabase: SupabaseAdmin | None = Depends(get_supabase_admin),
) -> UserAdded:
    """Add someone with their role and workspaces, and make their sign-in with the password given. An email
    that already has a sign-in (from Lead Intel) keeps it, and its own password."""
    email = body.email.strip().lower()
    if await users.by_email(email):
        raise HTTPException(status_code=409, detail="Someone with this email is already a user.")
    if supabase is None:
        raise HTTPException(status_code=503, detail="SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set to make sign-ins.")
    try:
        auth_id = await supabase.create_user(email, body.password)
    except SupabaseAdminError as exc:
        raise HTTPException(status_code=502 if (exc.status or 500) >= 500 else 422, detail=exc.message) from exc
    try:
        user = await users.create(body.name.strip() or email.split("@")[0], email, body.role, body.workspace_ids, auth_user_id=auth_id,
                                  permissions=body.permissions.model_dump(mode="json") if body.permissions else None)
    except Exception:
        if auth_id:
            await supabase.delete_user(auth_id)  # no half-made person: the sign-in goes too
        raise
    out = _out(user, body.workspace_ids).model_dump()
    return UserAdded(**out, sign_in="created" if auth_id else "existing")


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: UUID, body: UserUpdate, admin: CurrentUser = Depends(require_admin), users: UserRepo = Depends(get_user_repo)
) -> UserOut:
    user = await users.get(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    values = body.model_dump(exclude_unset=True, exclude={"workspace_ids", "permissions"})
    if body.permissions is not None:
        values["permissions"] = body.permissions.model_dump(mode="json")
    if "name" in values:
        values["name"] = (values["name"] or "").strip() or user.name
    losing_admin = user.role == "admin" and (
        values.get("role", "admin") != "admin" or values.get("status") == "deactivated"
    )
    if user.id == admin.id and (values.get("role", user.role), values.get("status", user.status)) != (user.role, user.status):
        raise HTTPException(status_code=409, detail="You can't change your own role or deactivate yourself. Ask another admin.")
    if losing_admin and await _active_admins(users, user.id) == 0:
        raise HTTPException(status_code=409, detail="This is the last active admin. Make someone else an admin first.")
    workspace_ids = body.workspace_ids if "workspace_ids" in body.model_fields_set else None
    user = await users.update(user, values, workspace_ids)
    forget_sign_ins()
    return _out(user, await users.workspace_ids(user.id))


@router.get("/users/{user_id}/access", response_model=list[AccessOut])
async def get_access(
    user_id: UUID, _: CurrentUser = Depends(require_admin), users: UserRepo = Depends(get_user_repo),
    access: AccessRepo = Depends(get_access_repo),
) -> list[AccessOut]:
    """The ICPs, products and smart lists a Prospector may see: theirs, and those shared with them."""
    if await users.get(user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    return [AccessOut(kind=a.kind, resource_id=a.resource_id, workspace_id=a.workspace_id, can_edit=a.can_edit or a.is_owner,
                      is_owner=a.is_owner) for a in await access.rows(user_id)]  # fmt: skip


@router.put("/users/{user_id}/access", response_model=list[AccessOut])
async def set_access(
    user_id: UUID,
    body: AccessIn,
    admin: CurrentUser = Depends(require_admin),
    users: UserRepo = Depends(get_user_repo),
    access: AccessRepo = Depends(get_access_repo),
    lead_intel: LeadIntelRepo = Depends(get_lead_intel_repo),
    products: ProductRepo = Depends(get_product_repo),
    lists: SmartListRepo = Depends(get_smart_list_repo),
) -> list[AccessOut]:
    """Share ICPs, products and smart lists with a Prospector, each to use or to edit. Replaces what was shared
    before; what they created stays theirs. Each item must exist, in a workspace they're in."""
    user = await users.get(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role != "prospector":
        raise HTTPException(status_code=422, detail="Only Prospectors see just what's shared with them; everyone else sees the whole workspace.")
    workspaces = set(await users.workspace_ids(user_id))
    icp_ids = {i["id"] for i in await lead_intel.list_icps()}
    grants = []
    for g in {(g.kind, g.resource_id): g for g in body.grants}.values():
        if g.workspace_id not in workspaces:
            raise HTTPException(status_code=422, detail="Give them that workspace first: an item can only be shared in a workspace they're in.")
        found = (g.resource_id in icp_ids if g.kind == "icp" else await products.get(g.workspace_id, g.resource_id) is not None
                 if g.kind == "product" else await lists.get(g.workspace_id, g.resource_id) is not None)  # fmt: skip
        if not found:
            raise HTTPException(status_code=422, detail=f"That {g.kind.replace('_', ' ')} doesn't exist in the workspace chosen.")
        grants.append(g.model_dump())
    await access.replace_shared(user_id, grants, admin.id)
    forget_sign_ins()
    return await get_access(user_id, admin, users, access)


@router.delete("/users/{user_id}", status_code=204, response_class=Response)
async def remove_user(user_id: UUID, admin: CurrentUser = Depends(require_admin), users: UserRepo = Depends(get_user_repo)) -> Response:
    """Remove someone from this app. Their Supabase account stays; if they sign in again they come
    back as Sales with no workspaces."""
    user = await users.get(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=409, detail="You can't remove yourself.")
    if user.role == "admin" and await _active_admins(users, user.id) == 0:
        raise HTTPException(status_code=409, detail="This is the last active admin.")
    await users.delete(user)
    forget_sign_ins()
    return Response(status_code=204)
