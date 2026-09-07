"""Measured usage and explicitly provisional values for the Harness dashboard."""

from __future__ import annotations

import math
from dataclasses import dataclass


def valid_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def reported_total(usage: dict) -> int | None:
    total = usage.get("total_tokens")
    if type(total) is int and total >= 0:
        return total
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if all(type(value) is int and value >= 0 for value in (prompt, completion)):
        # Reasoning and cached tokens are already included in these two totals.
        return prompt + completion
    return None


@dataclass(frozen=True)
class Measurement:
    value: float | None
    estimated: bool = False
    incomplete: bool = False

    def __add__(self, other: Measurement) -> Measurement:
        values = [value for value in (self.value, other.value) if value is not None]
        return Measurement(
            sum(values) if values else None,
            self.estimated or other.estimated,
            self.incomplete or other.incomplete or len(values) < 2,
        )

    @property
    def prefix(self) -> str:
        return "~" if self.estimated else ("≥" if self.incomplete else "")

    @property
    def suffix(self) -> str:
        return "+" if self.estimated and self.incomplete else ""


def settled(usage: dict, key: str, turns: int) -> Measurement:
    if not turns:
        return Measurement(0)
    value = usage.get(key)
    if valid_number(value):
        return Measurement(value)
    known = usage.get(f"known_{key}")
    return Measurement(known if valid_number(known) else None, incomplete=True)


def estimate_cost(prompt: int, completion: int, pricing: dict) -> Measurement:
    """Use catalog rates provisionally; provider billing replaces this after the call."""
    try:
        rates = [float(pricing[key]) for key in ("prompt", "completion")]
        request = float(pricing.get("request") or 0)
    except (KeyError, TypeError, ValueError):
        return Measurement(None, incomplete=True)
    if not all(valid_number(value) for value in [*rates, request]):
        return Measurement(None, incomplete=True)
    return Measurement(prompt * rates[0] + completion * rates[1] + request, estimated=True)
