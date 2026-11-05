"""Scoring a company against an ICP with Claude: how well it fits, 0 to 100, and why.

Only for companies found in Apollo (Lead Intel's are already verified). Claude reads what we
gathered (the website's text, the Instagram profile, Apollo's description) next to the ICP, using
the ICP's Company Verification prompt (or the workspace's default one), and answers in a fixed
JSON shape whatever the prompt says.

The prompt's model is used; Claude Haiku 5.5 (CLAUDE_SCORING_MODEL) when no prompt is given. Low
effort: this is classification. Temperature isn't sent (Haiku 5.5 rejects non-default sampling).
The SDK retries rate limits and server errors itself.
"""

import asyncio
from dataclasses import dataclass
from typing import Any

import anthropic
from pydantic import BaseModel, Field

from services.claude import error_message
from services.prompts import DEFAULTS, render_product as render, render as render_template, verify_variables

class CompanyScore(BaseModel):
    """Company Verification's verdict on one company."""

    score: int = Field(description="How good a prospect the company is, from 0 to 100, combining ICP fit and product fit.")
    summary: str = Field(description="One sentence naming the evidence that decided the score.")
    reasons: list[str] = Field(
        description='Up to 4 reasons it fits, most important first, each starting "ICP fit:" or "Product fit:" and pointing to the evidence.'
    )
    concerns: list[str] = Field(description="Up to 4 concerns or gaps in the evidence, most important first.")


class CompanyScoreOrRead(CompanyScore):
    """The score so far, and the one page to read next if the evidence isn't enough yet."""

    next_page: str | None = Field(
        description="The one URL from the list of unread pages to read next, or null when the evidence is enough to decide."
    )
    next_reason: str = Field(description="What reading next_page should settle. Empty when next_page is null.")


@dataclass(frozen=True)
class MoreReading:
    """Pages of the site Claude may ask to read next, and how many it has read of how many allowed."""

    candidates: list[dict]  # [{url, text}]
    read: int
    limit: int


def more_reading_note(more: MoreReading) -> str:
    links = "\n".join(f'{i}. {c["url"]}' + (f' ("{c["text"]}")' if c.get("text") else "") for i, c in enumerate(more.candidates, 1))
    return (
        f"<more_pages>\nYou have read {more.read} of at most {more.limit} pages of this company's website. "
        f"Pages of the site you haven't read:\n{links}\n\n"
        "If what you've read isn't enough to judge this company confidently, set next_page to the one URL above "
        "most likely to settle it, and next_reason to what you expect to learn there. If you can judge it now, "
        "set next_page to null. Don't read more just to be thorough.\n</more_pages>"
    )


@dataclass
class ScoreResult:
    score: int | None
    summary: str
    reasons: list[str]
    concerns: list[str]
    model: str
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    # Set when Claude wants one more page read before it decides.
    next_page: str | None = None
    next_reason: str = ""


@dataclass(frozen=True)
class PromptSpec:
    """A Company Verification prompt version: what Claude is told, and with which model."""

    system: str
    user: str
    model: str
    max_tokens: int
    version_id: str | None = None  # the eo_prompt_versions row, None for the built-in default


def default_spec(model: str) -> PromptSpec:
    d = DEFAULTS["verify"]
    return PromptSpec(d.system_prompt, d.user_prompt, model, d.max_tokens)


class CompanyScorer:
    def __init__(
        self, api_key: str, model: str, client: anthropic.AsyncAnthropic | None = None, concurrency: int = 4,
        base_url: str | None = None,
    ) -> None:
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=4, timeout=120.0)
        self._model = model
        self._gate = asyncio.Semaphore(concurrency)

    async def aclose(self) -> None:
        await self._client.close()

    async def score(
        self,
        icp: dict[str, Any],
        profile: dict[str, Any] | None,
        evidence: dict[str, Any],
        prompt: PromptSpec | None = None,
        more: MoreReading | None = None,
    ) -> ScoreResult:
        """Score the company. With `more`, Claude may instead ask for one more page (next_page)."""
        spec = prompt or default_spec(self._model)
        model = spec.model
        user = render(spec.user, verify_variables(icp, profile, evidence))
        if more is not None:
            user += "\n\n" + more_reading_note(more)
        async with self._gate:
            try:
                response = await self._client.messages.parse(
                    model=model,
                    max_tokens=spec.max_tokens,
                    system=render_template(spec.system, verify_variables(icp, profile, evidence)),
                    output_config={"effort": "low"},
                    output_format=CompanyScore if more is None else CompanyScoreOrRead,
                    messages=[{"role": "user", "content": user}],
                )
            except anthropic.APIError as exc:
                return ScoreResult(None, "", [], [], model, error_message(exc))

        tokens = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
        if response.stop_reason == "refusal":
            return ScoreResult(None, "", [], [], model, "Claude declined to score this company.", **tokens)
        parsed = response.parsed_output
        if parsed is None:
            return ScoreResult(None, "", [], [], model, "Claude's answer couldn't be read.", **tokens)
        return ScoreResult(
            score=max(0, min(100, parsed.score)),
            summary=parsed.summary.strip(),
            reasons=[r.strip() for r in parsed.reasons if r.strip()][:6],
            concerns=[c.strip() for c in parsed.concerns if c.strip()][:6],
            model=model,
            **tokens,
            next_page=(getattr(parsed, "next_page", None) or "").strip() or None,
            next_reason=(getattr(parsed, "next_reason", "") or "").strip(),
        )
