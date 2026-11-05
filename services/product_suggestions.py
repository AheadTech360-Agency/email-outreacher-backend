"""Read-only product proposals. Only the existing product save endpoint persists approved edits."""
import json

import anthropic
from pydantic import Field, ValidationError

from schemas import ApiModel, ProductIn
from services.claude import cost_usd, error_message
from services.prompts import PLACEHOLDER, product_variables, render



class ProductChangeReason(ApiModel):
    target: str = Field(max_length=100)
    reason: str = Field(max_length=1000)


class ProductProposal(ApiModel):
    summary: str = Field(max_length=3000)
    product: ProductIn
    reasons: list[ProductChangeReason] = Field(default_factory=list, max_length=100)


async def suggest_product(client, version, product, field_usage):
    variables = product_variables(product.model_dump(mode="json"))
    variables["product.data"] = json.dumps({"product":product.model_dump(mode="json", by_alias=True), "currentPromptFieldUsage":field_usage}, ensure_ascii=False, indent=2)
    user = render(version.user_prompt, variables)
    if "product.data" not in {m.group(1) for m in PLACEHOLDER.finditer(version.user_prompt)}:
        user += "\n\nComplete product data:\n" + variables["product.data"]
    try:
        response = await client.messages.parse(model=version.model, max_tokens=version.max_tokens,
            system=render(version.system_prompt, variables), output_config={"effort":"low"},
            output_format=ProductProposal, messages=[{"role":"user", "content":user}])
    except anthropic.APIError as exc:
        return None, error_message(exc), 0, 0, 0
    except (ValidationError, ValueError):
        return None, "The AI returned invalid or redundant product fields. No changes were applied; try again or adjust the prompt.", 0, 0, 0
    inputs, outputs = response.usage.input_tokens, response.usage.output_tokens
    cost = cost_usd(version.model, inputs, outputs)
    if response.stop_reason == "refusal":
        return None, "The AI declined to suggest changes. No changes were applied.", cost, inputs, outputs
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        return None, "The suggestion was incomplete. Increase the Product suggestions prompt's max tokens or shorten the product data.", cost, inputs, outputs
    return response.parsed_output, None, cost, inputs, outputs
