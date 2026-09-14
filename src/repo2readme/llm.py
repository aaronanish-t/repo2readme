"""Thin structured-output client. The pipeline depends on the `StructuredLLM` protocol so tests can fake it."""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models where server-side `fallbacks: "default"` applies; others run without it.
FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}
# Per-million-token list prices used only for the cost estimate in the report.
PRICES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-fable-5-1": (10.00, 50.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


class LLMError(Exception):
    pass


class RefusalError(LLMError):
    pass


class TruncatedError(LLMError):
    pass


@dataclass
class StageUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class Usage:
    model: str
    stages: dict[str, StageUsage] = field(default_factory=dict)

    def record(self, stage: str, u) -> None:
        s = self.stages.setdefault(stage, StageUsage())
        s.calls += 1
        s.input_tokens += u.input_tokens or 0
        s.output_tokens += u.output_tokens or 0
        s.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        s.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0

    def estimated_cost_usd(self) -> float | None:
        if self.model not in PRICES:
            return None
        pin, pout = PRICES[self.model]
        total = 0.0
        for s in self.stages.values():
            total += s.input_tokens * pin + s.output_tokens * pout
            total += s.cache_read_tokens * pin * 0.1 + s.cache_write_tokens * pin * 1.25
        return round(total / 1e6, 4)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "stages": {k: vars(v) for k, v in self.stages.items()},
            "estimated_cost_usd": self.estimated_cost_usd(),
        }


class StructuredLLM(Protocol):
    usage: Usage

    async def generate(
        self, *, stage: str, system: list[dict], user: str, schema: type[T], effort: str, max_tokens: int
    ) -> T: ...


class ClaudeLLM:
    def __init__(self, model: str, client: anthropic.AsyncAnthropic | None = None):
        self.model = model
        self.client = client or anthropic.AsyncAnthropic(max_retries=4)
        self.usage = Usage(model)
        self._lock = asyncio.Lock()

    def _request_options(self, effort: str, schema: type[BaseModel]) -> dict:
        output_config: dict = {"format": {"type": "json_schema", "schema": _json_schema(schema)}}
        opts: dict = {"output_config": output_config}
        if self.model.startswith("claude-haiku-4-5"):
            return opts  # no adaptive thinking or effort on Haiku 4.5
        opts["thinking"] = {"type": "adaptive"}
        output_config["effort"] = effort
        if self.model in FALLBACK_MODELS:
            opts["fallbacks"] = "default"
            opts["betas"] = [FALLBACK_BETA]
        return opts

    async def generate(
        self, *, stage: str, system: list[dict], user: str, schema: type[T], effort: str, max_tokens: int
    ) -> T:
        # The schema goes in output_config rather than `output_format=` so that parsing happens here,
        # after stop_reason is known: the SDK's streaming parser validates as soon as the text block
        # closes, which turns a max_tokens truncation into a ValidationError before we can tell.
        async with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            **self._request_options(effort, schema),
        ) as stream:
            message = await stream.get_final_message()

        async with self._lock:
            self.usage.record(stage, message.usage)

        if message.stop_reason == "refusal":
            category = getattr(message.stop_details, "category", None) if message.stop_details else None
            raise RefusalError(f"model declined the {stage} request (category: {category})")
        if message.stop_reason == "max_tokens":
            raise TruncatedError(f"{stage} output hit max_tokens={max_tokens}")
        text = "".join(b.text for b in message.content if b.type == "text")
        try:
            return schema.model_validate_json(text)
        except ValidationError as e:
            raise LLMError(f"{stage} returned output that doesn't match the schema: {e.error_count()} errors") from e


@functools.cache
def _json_schema(schema: type[BaseModel]) -> dict:
    return anthropic.transform_schema(schema.model_json_schema())
