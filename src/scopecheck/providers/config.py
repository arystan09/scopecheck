"""Explicit owner-reviewed text-only pricing and generation configuration."""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from scopecheck.runs.service import RequestModel

Positive = Annotated[int, Field(strict=True, gt=0, le=10**12)]
Tokens = Annotated[int, Field(strict=True, gt=0, le=10**7)]
Identifier = Annotated[
    str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
]


class PriceSnapshot(RequestModel):
    version: Identifier
    model: Identifier
    currency: Literal["USD"] = "USD"
    # Uncached standard-tier prices, in micro-USD per million tokens.
    input_microusd_per_million: Positive
    output_microusd_per_million: Positive
    context_window_tokens: Tokens
    max_output_tokens: Tokens
    valid_until: datetime
    owner_verified: Annotated[bool, Field(strict=True)]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.owner_verified is not True:
            raise ValueError("Owner verification required")
        if self.valid_until.tzinfo is None:
            raise ValueError("Price expiry must include a timezone")
        if self.max_output_tokens > self.context_window_tokens:
            raise ValueError("Output ceiling exceeds context window")
        return self

    def require_current(self) -> None:
        if self.valid_until <= datetime.now(UTC):
            raise ValueError("Price snapshot expired")


class Generation(RequestModel):
    max_input_tokens: Tokens
    max_output_tokens: Tokens
    temperature: (
        Annotated[float, Field(strict=True, ge=0, le=2, allow_inf_nan=False)] | None
    ) = None
    top_p: (
        Annotated[float, Field(strict=True, gt=0, le=1, allow_inf_nan=False)] | None
    ) = None
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None
    ) = None


class LiveConfig(RequestModel):
    mode: Literal["live"] = "live"
    provider: Literal["openai"] = "openai"
    model: Identifier
    generation: Generation
    price: PriceSnapshot

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.model != self.price.model:
            raise ValueError("Price snapshot must name the exact model")
        # Reserve the entire documented input context ceiling, never a tokenizer
        # estimate. A smaller guessed prompt count cannot safely cap a charge.
        if self.generation.max_input_tokens != self.price.context_window_tokens:
            raise ValueError("Input reservation must cover the full context ceiling")
        if self.generation.max_output_tokens > self.price.max_output_tokens:
            raise ValueError("Output limit exceeds the priced model ceiling")
        return self

    def maximum_charge(self) -> int:
        p, g = self.price, self.generation
        numerator = (
            g.max_input_tokens * p.input_microusd_per_million
            + g.max_output_tokens * p.output_microusd_per_million
        )
        return (numerator + 999_999) // 1_000_000
