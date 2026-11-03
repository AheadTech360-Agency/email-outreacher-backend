"""What the workspace sells. Each ICP picks one product on its outreach settings, and the product's
details fill the {{product.*}} placeholders in its verification, hook and email prompts."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from crud import ProductRepo
from dependencies import get_product_repo, require_manager, require_workspace_id
from schemas import ProductIn, ProductOut

router = APIRouter(prefix="/products", tags=["products"])


def _clean(body: ProductIn) -> dict[str, str]:
    return {k: v.strip() for k, v in body.model_dump().items()}


async def _out(products: ProductRepo, workspace_id: UUID, product) -> ProductOut:
    return ProductOut.model_validate(product).model_copy(update={"icp_ids": await products.used_by(workspace_id, product.id)})


async def _get(products: ProductRepo, workspace_id: UUID, product_id: UUID):
    product = await products.get(workspace_id, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.get("", response_model=list[ProductOut])
async def list_products(
    workspace_id: UUID = Depends(require_workspace_id), products: ProductRepo = Depends(get_product_repo)
) -> list[ProductOut]:
    return [await _out(products, workspace_id, p) for p in await products.list(workspace_id)]


@router.post("", response_model=ProductOut, status_code=201, dependencies=[Depends(require_manager)])
async def create_product(
    body: ProductIn, workspace_id: UUID = Depends(require_workspace_id), products: ProductRepo = Depends(get_product_repo)
) -> ProductOut:
    if not await products.workspace_exists(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    values = _clean(body)
    if not values["name"]:
        raise HTTPException(status_code=422, detail="Give the product a name")
    if await products.name_taken(workspace_id, values["name"]):
        raise HTTPException(status_code=409, detail="A product with this name already exists")
    return await _out(products, workspace_id, await products.create(workspace_id, values))


@router.put("/{product_id}", response_model=ProductOut, dependencies=[Depends(require_manager)])
async def update_product(
    product_id: UUID,
    body: ProductIn,
    workspace_id: UUID = Depends(require_workspace_id),
    products: ProductRepo = Depends(get_product_repo),
) -> ProductOut:
    product = await _get(products, workspace_id, product_id)
    values = _clean(body)
    if not values["name"]:
        raise HTTPException(status_code=422, detail="Give the product a name")
    if await products.name_taken(workspace_id, values["name"], except_id=product_id):
        raise HTTPException(status_code=409, detail="A product with this name already exists")
    return await _out(products, workspace_id, await products.update(product, values))


@router.delete("/{product_id}", status_code=204, dependencies=[Depends(require_manager)])
async def delete_product(
    product_id: UUID, workspace_id: UUID = Depends(require_workspace_id), products: ProductRepo = Depends(get_product_repo)
) -> Response:
    """Delete a product. ICPs that sold it are left with no product (the foreign key clears it) until another
    is picked; their own offer, if they have one, still lets emails be written."""
    product = await _get(products, workspace_id, product_id)
    await products.delete(product)
    return Response(status_code=204)
