"""Provider-aware prompt caching through OpenRouter's Chat Completions API.

Only request copies receive cache metadata. Conversation messages, signatures,
tool definitions and ordering remain unchanged in the controller's transcript.
"""

from __future__ import annotations

import copy
import hashlib
import re
import time
import uuid

from wavebench.tokens import prompt_tokens


def _family(model: str) -> str:
    model = model.lower().lstrip("~")
    if model.startswith("anthropic/"):
        return "anthropic"
    if model.startswith("openai/"):
        version = re.search(r"gpt-(\d+)(?:\.(\d+))?", model)
        if version and tuple(int(v or 0) for v in version.groups()) >= (5, 6):
            return "openai_hybrid"
        return "openai_implicit"
    if model.startswith("google/gemini-"):
        version = re.search(r"gemini-(\d+)(?:\.(\d+))?", model)
        if version and tuple(int(v or 0) for v in version.groups()) >= (2, 5):
            return "google"
    return "automatic"


def _text_block(message: dict) -> dict | None:
    # Thinking blocks and assistant tool calls must never be rewritten or marked.
    if message.get("role") not in {"system", "developer", "user", "tool"}:
        return None
    content = message.get("content")
    if isinstance(content, str) and content:
        message["content"] = [{"type": "text", "text": content}]
        return message["content"][0]
    if isinstance(content, list):
        return next(
            (
                block
                for block in reversed(content)
                if block.get("type") == "text" and block.get("text")
            ),
            None,
        )
    return None


class CachePolicy:
    """One policy per model conversation; reuse it across build, repair and retries."""

    def __init__(self, model_id: str, session_id: str | None = None):
        self.family = _family(model_id)
        identity = f"{model_id}:{session_id or uuid.uuid4().hex}"
        self.key = "wb-" + hashlib.sha256(identity.encode()).hexdigest()[:48]
        self.boundaries: list[int] = []
        self.checkpoint: int | None = None
        self.checkpoint_at = 0.0
        self.previous_request: float | None = None
        self.anthropic_ttl = "5m"

    def reset(self) -> None:
        """Compaction changes message positions; retain affinity, discard checkpoints."""
        self.boundaries.clear()
        self.checkpoint = None
        self.checkpoint_at = 0.0

    def prepare(self, messages: list[dict], tools: list[dict], *, now=None) -> tuple[dict, dict]:
        now = time.monotonic() if now is None else now
        wire = copy.deepcopy(messages)
        payload = {"messages": wire, "session_id": self.key}
        record = {"policy": self.family, "session_id": self.key, "breakpoints": []}
        if self.family.startswith("openai"):
            payload["prompt_cache_key"] = self.key
        if self.family in {"automatic", "openai_implicit"}:
            return payload, record

        if self.family == "openai_hybrid":
            # OpenRouter's Chat Completions conversion drops explicit markers on
            # tool results. Explicit-only mode therefore disables caching when a
            # short user prompt grows through tool calls. Let OpenAI cache the
            # latest eligible message, retaining explicit anchors on user text.
            payload["prompt_cache_options"] = {"mode": "implicit", "ttl": "30m"}
            record.update(mode="implicit", ttl="30m")
        blocks = {
            i: block
            for i, message in enumerate(wire)
            if not (self.family == "openai_hybrid" and message.get("role") == "tool")
            and (block := _text_block(message))
        }
        if not blocks:
            return payload, record
        latest = max(blocks)

        if self.family == "google":
            # Google uses ONLY the last marker, charges storage and does not refresh
            # its 5m TTL on reads. Moving the marker every turn rewrites the prefix.
            if (self.checkpoint is None or now - self.checkpoint_at >= 300) and prompt_tokens(
                messages[: latest + 1], tools
            ) >= 4096:
                self.checkpoint, self.checkpoint_at = latest, now
            positions = [self.checkpoint] if self.checkpoint in blocks else []
            marker = {"type": "ephemeral"}
        else:
            if latest not in self.boundaries:
                self.boundaries.append(latest)
            # Anthropic needs prior boundaries beyond its 20-block lookback.
            # OpenAI's implicit breakpoint consumes one of the four write slots.
            first = next((i for i, m in enumerate(messages) if m.get("role") == "user"), latest)
            recent = 2 if self.family == "openai_hybrid" else 3
            positions = sorted({first, *self.boundaries[-recent:]}.intersection(blocks))
            if self.family == "anthropic":
                if self.previous_request is not None and now - self.previous_request >= 240:
                    self.anthropic_ttl = "1h"
                marker = {"type": "ephemeral", "ttl": self.anthropic_ttl}
            else:
                marker = {"mode": "explicit"}
        field = "prompt_cache_breakpoint" if self.family == "openai_hybrid" else "cache_control"
        for position in positions:
            blocks[position][field] = dict(marker)
        self.previous_request = now
        record.update(
            breakpoints=positions, ttl=marker.get("ttl", "5m" if self.family == "google" else "30m")
        )
        return payload, record


def affinity(model_id: str, prompt: str) -> dict:
    """Single-shot calls can reuse automatic caches on repeated benchmark prompts.

    Avoid provisioning explicit storage when there is no known next turn;
    each provider's default implicit caching and pricing still apply.
    """
    key = "wb-" + hashlib.sha256(f"{model_id}:{prompt}".encode()).hexdigest()[:48]
    result = {"session_id": key}
    if _family(model_id).startswith("openai"):
        result["prompt_cache_key"] = key
    return result
