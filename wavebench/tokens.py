"""Local token estimates, calibrated against actual provider prompt usage.

o200k is a proxy for other vendors, not their tokenizer. Provider measurements
replace the estimate for the unchanged prefix; only new content is estimated.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache

import tiktoken


@lru_cache(maxsize=1)
def encoder():
    return tiktoken.get_encoding("o200k_base")


def count_tokens(value) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(encoder().encode(text, disallowed_special=()))


def prompt_tokens(messages: list[dict], tools: list[dict]) -> int:
    # Counting JSON also reserves space for roles, call IDs, schemas and signatures.
    return count_tokens({"messages": messages, "tools": tools})


def context_usage(usage: dict, cache_policy: str) -> dict:
    """Separate Gemini cache-creation billing from the model's actual context.

    OpenRouter charges a cache-create input plus the generation input on a Google
    explicit-cache miss. Live usage includes both in prompt_tokens; cache writes
    aren't a second copy of the prefix in the model's context. Keep raw usage for
    budget/cost accounting and normalize only the context estimator's observation.
    """
    measured = usage.get("prompt_tokens")
    written = (usage.get("prompt_tokens_details") or {}).get("cache_write_tokens")
    if (
        cache_policy == "google"
        and type(measured) is int
        and type(written) is int
        and 0 < written < measured
    ):
        return {**usage, "prompt_tokens": measured - written}
    return usage


class PromptEstimate:
    def __init__(self):
        self.measured: int | None = None
        self.local = 0
        self.ratio = 1.0

    def observe(self, local: int, usage: dict) -> None:
        measured = usage.get("prompt_tokens")
        if type(measured) is int and measured >= 0:
            self.measured, self.local = measured, local
            self.ratio = max(1.0, measured / max(1, local))

    def estimate(self, local: int) -> int:
        if self.measured is None:
            return math.ceil(local * self.ratio)
        return self.measured + math.ceil(max(0, local - self.local) * self.ratio)

    def bound(self, local: int) -> int:
        # Ten percent on unmeasured content, never on the already measured prefix.
        delta = local if self.measured is None else max(0, local - self.local)
        return self.estimate(local) + math.ceil(delta * self.ratio * 0.1)

    def after_compaction(self) -> PromptEstimate:
        result = PromptEstimate()
        # Preserve the observed vendor/tokenizer ratio, but discard prefix counts
        # because the middle of that prefix has changed.
        result.ratio = self.ratio
        return result
