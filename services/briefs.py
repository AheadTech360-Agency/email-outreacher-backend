"""A company brief: everything Coldbrew gathered about one company (LinkedIn, website, socials, posts, the people found
there, its verdict) turned by Claude into a short analysis for outreach, ending with the hooks most likely to work.
Quick Start's LinkedIn option ends with one per company. Stored in the company's research JSON under "brief"."""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import anthropic
from pydantic import BaseModel, Field

from services.claude import error_message

SYSTEM = """You prepare a short outreach brief on one company for a salesperson who is about to email it.
You get everything gathered about the company: its LinkedIn listing, its website text, its social profiles, its recent
posts, the people found there, and how well it fits the ideal customer profile (ICP) and the product being sold.

Rules:
- Use only the evidence given. Never invent facts, numbers, names or events. When something isn't known, say so.
- Be specific and plain. No hype, no filler, no em dashes.
- Hooks are openings for a cold email: each ties something real and recent about the company (a post, a launch, a page
  on their site, their positioning) to the problem the product solves. Name the evidence each hook rests on.
- Rank hooks best first. Only include hooks the evidence supports; fewer good hooks beat more weak ones."""


class Hook(BaseModel):
    hook: str = Field(description="The opening idea in one sentence, as it could start an email.")
    why: str = Field(description="Why it should land with this company, tied to the product's value.")
    evidence: str = Field(description="The exact thing it rests on: which post, page or fact.")


class CompanyBrief(BaseModel):
    summary: str = Field(description="Two or three sentences: who they are, what they sell, to whom, and where.")
    fit: str = Field(description="One or two sentences on why they fit (or don't) the ICP and the product.")
    signals: list[str] = Field(description="Up to 5 concrete, recent signals worth knowing (growth, launches, hiring, focus).")
    online: str = Field(description="One or two sentences on their website and social presence and how active they are.")
    posting_themes: list[str] = Field(description="Up to 4 themes their recent posts are about; empty if no posts were found.")
    hooks: list[Hook] = Field(description="Up to 4 hooks, best first.")
    best_contacts: list[str] = Field(description="Up to 3 of the people found to email first, as 'Name (title): why'.")
    cautions: list[str] = Field(description="Up to 3 things to be careful about (weak evidence, poor fit signs, sensitive topics).")


@dataclass
class BriefResult:
    brief: CompanyBrief | None
    model: str
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def evidence_text(evidence: dict[str, Any], limit: int = 24000) -> str:
    """The gathered evidence as tagged sections, trimmed so one company never sends a huge prompt."""
    parts = []
    for key, value in evidence.items():
        if value in (None, "", [], {}):
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        parts.append(f"<{key}>\n{text[:8000]}\n</{key}>")
    out = "\n\n".join(parts)
    return out[:limit]


class BriefWriter:
    def __init__(self, api_key: str, model: str, client: anthropic.AsyncAnthropic | None = None, concurrency: int = 4,
                 base_url: str | None = None) -> None:  # fmt: skip
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=4, timeout=120.0)
        self._model = model
        self._gate = asyncio.Semaphore(concurrency)

    async def aclose(self) -> None:
        await self._client.close()

    async def write(self, evidence: dict[str, Any]) -> BriefResult:
        async with self._gate:
            try:
                response = await self._client.messages.parse(
                    model=self._model,
                    max_tokens=2000,
                    system=SYSTEM,
                    output_config={"effort": "medium"},
                    output_format=CompanyBrief,
                    messages=[{"role": "user", "content": "Write the brief for this company.\n\n" + evidence_text(evidence)}],
                )
            except anthropic.APIError as exc:
                return BriefResult(None, self._model, error_message(exc))
        tokens = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
        if response.stop_reason == "refusal":
            return BriefResult(None, self._model, "Claude declined to write this brief.", **tokens)
        if response.parsed_output is None:
            return BriefResult(None, self._model, "Claude's answer couldn't be read.", **tokens)
        return BriefResult(response.parsed_output, self._model, None, **tokens)
