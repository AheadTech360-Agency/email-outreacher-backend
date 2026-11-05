"""What the workspace sells. Each ICP picks one product on its outreach settings, and the product's
details fill the {{product.*}} placeholders in its verification, hook and email prompts."""

from uuid import UUID

import anthropic

from fastapi import APIRouter, Depends, HTTPException, Response

from crud import ProductRepo, PromptRepo, ProviderCallRepo
from dependencies import get_product_repo, get_prompt_repo, get_provider_call_repo, get_optional_anthropic, require_manager, require_workspace_id
from schemas import ProductIn, ProductOut
from services.product_suggestions import suggest_product
from services.prompts import PLACEHOLDER

router = APIRouter(prefix="/products", tags=["products"])


def _clean(body: ProductIn) -> dict:
    values = body.model_dump(mode="json")
    return {k: v.strip() if isinstance(v, str) else v for k, v in values.items()}


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


@router.post("/suggest", dependencies=[Depends(require_manager)])
async def product_suggestions(
    body: ProductIn, workspace_id: UUID = Depends(require_workspace_id),
    products: ProductRepo = Depends(get_product_repo), prompts: PromptRepo = Depends(get_prompt_repo),
    calls: ProviderCallRepo = Depends(get_provider_call_repo),
    client: anthropic.AsyncAnthropic | None = Depends(get_optional_anthropic),
):
    if client is None:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not set")
    if not await products.workspace_exists(workspace_id):
        raise HTTPException(404, "Workspace not found")
    version = await prompts.for_icp(workspace_id, "product", None)
    if version is None:
        raise HTTPException(404, "Set a default Product suggestions prompt in Prompts first")
    templates, versions = await prompts.list(workspace_id)
    current = {p.current_version_id: p.name for p in templates}
    usage = {}
    for prompt_version in versions:
        if prompt_version.id not in current:
            continue
        for match in PLACEHOLDER.finditer(prompt_version.system_prompt + "\n" + prompt_version.user_prompt):
            key = match.group(1)
            if key.startswith("product."):
                usage.setdefault(key, set()).add(current[prompt_version.id])
    proposal, error, cost, inputs, outputs = await suggest_product(client, version, body,
        {key: sorted(names) for key, names in usage.items()})
    await calls.record(workspace_id, "claude", "messages", "failed" if error else "succeeded", cost_usd=cost,
        action="Product suggestions", model=version.model, input_tokens=inputs, output_tokens=outputs)
    if error:
        raise HTTPException(502, error)
    # This endpoint never writes to the product. Approval happens in the editor before its normal save.
    return {**proposal.model_dump(mode="json", by_alias=True), "costUsd":cost,
            "promptId":str(version.prompt_id), "promptVersion":version.version}


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
