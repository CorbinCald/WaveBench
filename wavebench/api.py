"""OpenRouter API client.

Handles API-key loading, streaming and non-streaming chat completions,
reasoning-effort negotiation (including fallback formats for models that
reject ``reasoning.effort``), and catalog fetching. All OpenRouter-specific
knowledge lives here so the rest of the package never talks to HTTP directly.

Key entry points:
  - ``load_api_key()``  — env var or ``.env`` file lookup
  - ``call_model_async()``  — non-streaming completion
  - ``call_model_streaming()``  — SSE completion with progress callback
  - ``call_tts_speech()``  — raw audio generation via /audio/speech
  - ``call_image_generation()``  — non-streaming image-output chat completion
  - ``fetch_top_models()``  — sync catalog fetch for the config menu
"""

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import aiohttp

from wavebench.models import _model_score, is_image_model, is_stealth
from wavebench.tui.styles import S, _tri

API_URL = "https://openrouter.ai/api/v1"
_MODEL_CONTEXT_CACHE: dict[str, int] = {}
_MODEL_CONTEXTS_ATTEMPTED = False
_MODEL_CONTEXT_LOCK = asyncio.Lock()


async def _load_model_context_lengths(
    session: aiohttp.ClientSession,
    api_key: str,
) -> None:
    """Populate a cache of model_id -> context_length from OpenRouter."""
    global _MODEL_CONTEXTS_ATTEMPTED
    if _MODEL_CONTEXTS_ATTEMPTED:
        return

    async with _MODEL_CONTEXT_LOCK:
        if _MODEL_CONTEXTS_ATTEMPTED:
            return
        _MODEL_CONTEXTS_ATTEMPTED = True

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "Benchmark Script",
        }
        try:
            async with session.get(f"{API_URL}/models", headers=headers) as resp:
                if resp.status != 200:
                    return
                body = await resp.json()
                for m in body.get("data", []):
                    mid = m.get("id")
                    ctx = m.get("context_length")
                    if not mid:
                        continue
                    try:
                        ctx_i = int(ctx)
                    except (TypeError, ValueError):
                        continue
                    if ctx_i > 0:
                        _MODEL_CONTEXT_CACHE[mid] = ctx_i
        except Exception:
            # Fall back to legacy defaults if model metadata cannot be fetched.
            return


async def _resolve_max_tokens(
    session: aiohttp.ClientSession,
    api_key: str,
    model_id: str,
    prompt: str,
    fallback: int,
) -> int:
    """Use model context_length and budget around prompt size.

    The result is capped to MAX_OUTPUT_TOKENS_DEFAULT so we don't
    request an absurdly large completion that exceeds credit budgets
    (HTTP 402) or wastes context window.  Callers can still pass an
    explicit *max_tokens* to override this cap.
    """
    if model_id in _MODEL_CONTEXT_CACHE:
        context_limit = _MODEL_CONTEXT_CACHE[model_id]
    else:
        await _load_model_context_lengths(session, api_key)
        context_limit = _MODEL_CONTEXT_CACHE.get(model_id, fallback)

    prompt_tokens_est = max(1, len(prompt) // 4)
    safety_buffer = 512
    available = max(1, context_limit - prompt_tokens_est - safety_buffer)
    return min(available, MAX_OUTPUT_TOKENS_DEFAULT)


def _context_limit_from_error_text(err_text: str) -> int | None:
    """Extract context limit from OpenRouter 400 text when present."""
    m = re.search(r"maximum context length is (\d+) tokens", err_text)
    if not m:
        return None
    try:
        limit = int(m.group(1))
    except (TypeError, ValueError):
        return None
    return limit if limit > 0 else None


def _credit_token_limit_from_error(err_text: str) -> int | None:
    """Extract the affordable token count from an OpenRouter 402 error.

    Typical message: "You requested up to 128000 tokens, but can only
    afford 24576 tokens."  We grab the *affordable* number so we can
    retry with a lower max_tokens.
    """
    m = re.search(r"can(?:\s+only)?\s+(?:afford\s+)?(\d[\d,]*)\b", err_text)
    if not m:
        return None
    try:
        limit = int(m.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return limit if limit > 0 else None


MAX_OUTPUT_TOKENS_DEFAULT = 32_000
REASONING_STALL_TIMEOUT = 300  # 5 minutes with zero tokens → abort

# Statuses that indicate a transient upstream condition rather than a real
# request error. Each is retried with bounded exponential backoff.
_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})
_MAX_RETRIES: int = 3
_MAX_RETRY_WAIT_S: float = 30.0
# on_retry callback signature: (status, attempt, max_attempts, wait_seconds)
RetryCallback = Callable[[int, int, int, float], None]


def _retry_wait_seconds(retry_after_header: str | None, attempt: int) -> float:
    """Seconds to wait before retry *attempt* (1-based).

    Honors a numeric ``Retry-After`` (seconds) when present; otherwise
    falls back to exponential backoff (1s, 2s, 4s, …). Capped at
    ``_MAX_RETRY_WAIT_S`` so a stuck upstream can't park a benchmark
    indefinitely.
    """
    if retry_after_header:
        try:
            return min(_MAX_RETRY_WAIT_S, max(0.5, float(retry_after_header)))
        except ValueError:
            pass
    return min(_MAX_RETRY_WAIT_S, 2.0 ** (attempt - 1))


def load_api_key() -> str | None:
    """Load API key from environment or .env file."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key

    if os.path.exists(".env"):
        try:
            with open(".env", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() == "OPENROUTER_API_KEY":
                            return v.strip().strip('"').strip("'")
        except Exception as exc:
            print(f"    {_tri} {S.DIM}could not read .env: {exc}{S.RST}")

    return None


# Ordered low → max.  Only used for distance math when clamping an
# unsupported effort choice down to the closest level the model accepts.
_EFFORT_ORDER: list[str] = ["low", "medium", "high", "xhigh", "max"]

# Per-model effort capabilities (as of 2026-04-22).  Patterns match as
# substrings against the lower-cased OpenRouter model id; most-specific
# entries first so "opus-4.7" wins over a hypothetical generic "opus".
_CLAUDE_EFFORT_CAPABILITIES: list[tuple] = [
    ("opus-4-7", ["low", "medium", "high", "xhigh", "max"]),
    ("opus-4.7", ["low", "medium", "high", "xhigh", "max"]),
    ("mythos", ["low", "medium", "high", "xhigh", "max"]),
    ("opus-4-6", ["low", "medium", "high", "max"]),
    ("opus-4.6", ["low", "medium", "high", "max"]),
    ("sonnet-4-6", ["low", "medium", "high", "max"]),
    ("sonnet-4.6", ["low", "medium", "high", "max"]),
    ("opus-4-5", ["low", "medium", "high"]),
    ("opus-4.5", ["low", "medium", "high"]),
]


def _supported_efforts(model_id: str) -> list[str] | None:
    """Return the effort levels supported by *model_id*, or None if the
    model accepts no ``effort`` parameter at all (older Claude variants).
    """
    lower = model_id.lower()
    is_anthropic = "anthropic/" in lower or "claude" in lower
    if is_anthropic:
        for pat, levels in _CLAUDE_EFFORT_CAPABILITIES:
            if pat in lower:
                return levels
        return None  # legacy Claude — only reasoning.enabled toggles work
    if "deepseek-v4" in lower:
        # V4 *does* have a max-reasoning tier natively (DeepSeek exposes two
        # thinking levels — `high` and `max`, per api-docs.deepseek.com/
        # guides/thinking_mode). OpenRouter's normalization layer names that
        # ceiling `xhigh` and rejects the literal string `max` for V4 slugs
        # (verified 2026-04-24 via 400 validator: expected one of
        # xhigh|high|medium|low|minimal|none). `xhigh` → DeepSeek `max`
        # upstream, so clamping user-configured `max` to `xhigh` here
        # actually reaches V4's highest tier — it's a naming bridge, not a
        # downgrade.
        return ["low", "medium", "high", "xhigh"]
    if "gpt-5" in lower:
        # Same OpenRouter naming bridge as DeepSeek V4. Verified 2026-04-25
        # against gpt-5, gpt-5-pro, gpt-5-mini, gpt-5-nano, gpt-5-codex,
        # gpt-5.5, gpt-5.5-pro: every variant's validator accepts
        # xhigh|high|medium|low|minimal|none and 400s on literal `max`.
        # `xhigh` is the top reasoning tier across the family.
        return ["low", "medium", "high", "xhigh"]
    return ["low", "medium", "high"]


def _is_effort_naming_bridge(model_id: str, requested: str, mapped: str) -> bool:
    """True when ``requested → mapped`` is a cross-vendor naming bridge that
    routes to the same underlying tier, not a capability downgrade.

    Surfacing a "max → xhigh" notice for DeepSeek V4 is misleading because
    V4 natively names its max-reasoning tier ``max`` while OpenRouter's
    normalization layer names the identical tier ``xhigh``. Callers building
    user-facing effort-adjustment notices should skip entries for which this
    predicate returns True.
    """
    lower = model_id.lower()
    return requested == "max" and mapped == "xhigh" and ("deepseek-v4" in lower or "gpt-5" in lower)


def _map_effort(effort: str, supported: list[str]) -> str:
    """Clamp *effort* to the closest value in *supported*.  Ties (equal
    distance — e.g. xhigh on a 4.6 model between high and max) resolve
    upward, per the "highest effort closest to the set choice" rule.
    """
    if effort in supported:
        return effort
    if effort not in _EFFORT_ORDER:
        return effort
    target = _EFFORT_ORDER.index(effort)
    best: tuple | None = None
    for level in supported:
        if level not in _EFFORT_ORDER:
            continue
        idx = _EFFORT_ORDER.index(level)
        key = (abs(idx - target), -idx)  # min distance, then max ordinal
        if best is None or key < best[0]:
            best = (key, level)
    return best[1] if best else effort


def _reasoning_attempts(
    model_id: str,
    effort: str,
    max_tokens: int,
) -> list[dict[str, Any]]:
    """Build an ordered list of reasoning payload overrides to try.

    Each entry is a dict to merge into the base request data.  When the
    provider returns 400 the caller moves to the next entry.  Returns an
    empty list for models known to never support reasoning.

    Order:
      1. Native ``reasoning.effort`` with effort clamped to what the
         model supports (xhigh → high/max on 4.6, max → high on 4.5, …).
         Legacy Claude models fall back to ``reasoning.enabled: true``
         because they don't accept an effort parameter.
      2. ``reasoning.max_tokens`` budget (for Gemini / Qwen-style APIs
         that want an explicit thinking budget).  Deprecated on Claude
         4.6 and removed on 4.7 — kept here only as a last-resort alias.
      3. Simple ``enabled: true`` flag.
      4. Top-level ``reasoning_effort`` (native provider APIs).
    Duplicates are suppressed automatically.
    """
    lower = model_id.lower()

    if "glm-4.7" in lower:
        return []

    is_mercury = "inception/" in lower or "mercury" in lower

    seen: list[dict[str, Any]] = []

    def _add(extra: dict[str, Any]) -> None:
        if extra not in seen:
            seen.append(extra)

    if is_mercury:
        # Mercury-2 accepts OpenRouter's reasoning.effort (low/medium/high)
        # and Inception's native reasoning_summary param which gets passed
        # through to the provider, producing more detailed chain-of-thought
        # inline in the content.  Don't try max_tokens or enabled — they're
        # no-ops for Mercury, and combining effort + max_tokens causes a 400.
        mapped = _map_effort(effort, ["low", "medium", "high"])
        _add(
            {
                "reasoning": {"effort": mapped},
                "reasoning_summary": True,
            }
        )
        return seen

    supported = _supported_efforts(model_id)
    if supported is None:
        # Legacy Claude (Sonnet 4.5, Haiku series, Claude 3.x, …) — no
        # effort parameter; toggling reasoning on is the best we can do.
        _add({"reasoning": {"enabled": True}})
    else:
        _add({"reasoning": {"effort": _map_effort(effort, supported)}})

    budget = max(1024, int(max_tokens * 0.8))
    _add({"reasoning": {"max_tokens": budget}})
    _add({"reasoning": {"enabled": True}})
    _add({"reasoning_effort": effort})

    return seen


async def call_model_async(
    session: aiohttp.ClientSession,
    api_key: str,
    model_id: str,
    prompt: str,
    reasoning_effort: str | None = "high",
    return_usage: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> Any:
    """Call the OpenRouter API for a specific model."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "Benchmark Script",
    }

    is_anthropic = "anthropic/" in model_id.lower() or "claude" in model_id.lower()

    fallback_max_tokens = 128000 if is_anthropic else 200000
    resolved_max_tokens = await _resolve_max_tokens(
        session, api_key, model_id, prompt, fallback=fallback_max_tokens
    )
    model_max_tokens = (
        max(1, min(max_tokens, resolved_max_tokens))
        if max_tokens is not None
        else resolved_max_tokens
    )

    base_data = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1 if temperature is None else temperature,
        "max_tokens": model_max_tokens,
    }

    if "gemini-3" in model_id.lower():
        base_data["temperature"] = 1.0

    async def _retry_with_reduced_tokens(data: dict, affordable: int) -> Any:
        """Re-issue *data* with max_tokens clamped to *affordable* (402 recovery)."""
        if affordable >= data.get("max_tokens", 0):
            return None
        retry_data = {**data, "max_tokens": max(1, affordable)}
        print(
            f"    {_tri} {S.DIM}{model_id} 402 — retrying with "
            f"max_tokens={retry_data['max_tokens']}{S.RST}"
        )
        async with session.post(
            f"{API_URL}/chat/completions",
            headers=headers,
            json=retry_data,
        ) as r:
            if r.status == 200:
                body = await r.json()
                content = body["choices"][0]["message"]["content"]
                usage = body.get("usage", {})
                return (content, usage) if return_usage else content
            t = await r.text()
            raise RuntimeError(f"HTTP {r.status}: {t[:120].strip()}")

    # Try reasoning formats in priority order; on 400 try the next format
    if reasoning_effort:
        attempts = _reasoning_attempts(model_id, reasoning_effort, model_max_tokens)
        for attempt_idx, extra in enumerate(attempts):
            data = {**base_data, **extra}
            try:
                async with session.post(
                    f"{API_URL}/chat/completions",
                    headers=headers,
                    json=data,
                ) as resp:
                    if resp.status == 200:
                        try:
                            body = await resp.json()
                            content = body["choices"][0]["message"]["content"]
                            usage = body.get("usage", {})
                            return (content, usage) if return_usage else content
                        except (KeyError, IndexError, json.JSONDecodeError) as e:
                            print(f"    {_tri} {S.DIM}parse error ({model_id}): {e}{S.RST}")
                    elif resp.status == 400:
                        remaining = len(attempts) - attempt_idx - 1
                        if remaining > 0:
                            print(
                                f"    {_tri} {S.DIM}{model_id} 400 w/ reasoning"
                                f" — trying next format ({remaining} left)…{S.RST}"
                            )
                            continue
                        print(
                            f"    {_tri} {S.DIM}{model_id} 400 w/ reasoning"
                            f" — retrying without…{S.RST}"
                        )
                    elif resp.status == 402:
                        text = await resp.text()
                        affordable = _credit_token_limit_from_error(text)
                        if affordable:
                            result = await _retry_with_reduced_tokens(data, affordable)
                            if result is not None:
                                return result
                        raise RuntimeError(f"HTTP 402: {text[:120].strip()}")
                    else:
                        text = await resp.text()
                        print(
                            f"    {_tri} {S.DIM}{model_id}: {resp.status}"
                            f" — {text[:120].strip()}{S.RST}"
                        )
                        if resp.status not in (429, 500, 502, 503, 504):
                            raise RuntimeError(f"HTTP {resp.status}: {text[:120].strip()}")
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                print(f"    {_tri} {S.DIM}{model_id} reasoning err: timeout{S.RST}")
            except aiohttp.ClientError as exc:
                print(f"    {_tri} {S.DIM}{model_id} reasoning err: API err ({exc}){S.RST}")
            except Exception as exc:
                exc_str = str(exc) or exc.__class__.__name__
                print(f"    {_tri} {S.DIM}{model_id} reasoning err: {exc_str}{S.RST}")
            break  # non-400 failures skip remaining reasoning formats

    # Final attempt — without reasoning
    async with session.post(
        f"{API_URL}/chat/completions",
        headers=headers,
        json=base_data,
    ) as resp:
        if resp.status == 200:
            try:
                body = await resp.json()
                content = body["choices"][0]["message"]["content"]
                usage = body.get("usage", {})
                return (content, usage) if return_usage else content
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                raise RuntimeError(f"parse error: {e}")

        text = await resp.text()

        if resp.status == 402:
            affordable = _credit_token_limit_from_error(text)
            if affordable:
                result = await _retry_with_reduced_tokens(base_data, affordable)
                if result is not None:
                    return result
            raise RuntimeError(f"HTTP 402: {text[:120].strip()}")

        if resp.status == 400:
            limit = _context_limit_from_error_text(text)
            if limit is not None:
                prompt_tokens_est = max(1, len(prompt) // 4)
                retry_max = max(1, limit - prompt_tokens_est - 512)
                if retry_max < base_data["max_tokens"]:
                    retry_data = {**base_data, "max_tokens": retry_max}
                    async with session.post(
                        f"{API_URL}/chat/completions",
                        headers=headers,
                        json=retry_data,
                    ) as retry_resp:
                        if retry_resp.status == 200:
                            body = await retry_resp.json()
                            content = body["choices"][0]["message"]["content"]
                            usage = body.get("usage", {})
                            return (content, usage) if return_usage else content
                        retry_text = await retry_resp.text()
                        raise RuntimeError(f"HTTP {retry_resp.status}: {retry_text[:120].strip()}")
        raise RuntimeError(f"HTTP {resp.status}: {text[:120].strip()}")


async def call_model_streaming(
    session: aiohttp.ClientSession,
    api_key: str,
    model_id: str,
    prompt: str,
    reasoning_effort: str | None = "high",
    on_progress: Callable[[int], None] | None = None,
    max_tokens: int | None = None,
    on_retry: RetryCallback | None = None,
) -> tuple[str, dict]:
    """Stream a chat completion via SSE, calling *on_progress(total_chars)* for
    each content chunk so the caller can drive a live progress bar.

    Returns ``(content, usage)`` — same shape as ``call_model_async`` with
    ``return_usage=True``.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "Benchmark Script",
    }

    is_anthropic = "anthropic/" in model_id.lower() or "claude" in model_id.lower()

    fallback_max_tokens = 128000 if is_anthropic else 200000
    resolved_max_tokens = await _resolve_max_tokens(
        session, api_key, model_id, prompt, fallback=fallback_max_tokens
    )
    model_max_tokens = (
        max(1, min(max_tokens, resolved_max_tokens))
        if max_tokens is not None
        else resolved_max_tokens
    )

    base_data: dict[str, Any] = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": model_max_tokens,
        "stream": True,
    }

    if "gemini-3" in model_id.lower():
        base_data["temperature"] = 1.0

    _stall_deadline = time.monotonic() + REASONING_STALL_TIMEOUT
    _got_first_token = False

    async def _do_stream(data: dict) -> tuple[str | None, dict, int, str]:
        """Execute one streaming request.  Returns (content, usage, status, err).

        Transparently retries 429/5xx upstream throttles up to ``_MAX_RETRIES``
        with exponential backoff (or ``Retry-After`` when honored), notifying
        the caller via ``on_retry`` so the UI can surface throttle state.
        """
        nonlocal _got_first_token
        parts: list[str] = []
        usage: dict = {}
        total_chars = 0

        def _stall_remaining() -> float:
            return _stall_deadline - time.monotonic()

        def _raise_stall() -> None:
            elapsed_m = int(REASONING_STALL_TIMEOUT / 60)
            raise RuntimeError(f"no tokens after {elapsed_m}m (reasoning stall)")

        last_status = 0
        last_err = ""

        for attempt in range(1, _MAX_RETRIES + 2):
            if not _got_first_token and _stall_remaining() <= 0:
                _raise_stall()

            post_timeout = _stall_remaining() if not _got_first_token else None
            resp_ctx = session.post(f"{API_URL}/chat/completions", headers=headers, json=data)
            try:
                resp = await asyncio.wait_for(resp_ctx.__aenter__(), timeout=post_timeout)
            except asyncio.TimeoutError:
                try:
                    await resp_ctx.__aexit__(None, None, None)
                except Exception:
                    pass
                _raise_stall()

            wait_s = 0.0
            should_retry = False
            try:
                if resp.status in _RETRYABLE_STATUSES and attempt <= _MAX_RETRIES:
                    last_status = resp.status
                    last_err = await resp.text()
                    wait_s = _retry_wait_seconds(resp.headers.get("Retry-After"), attempt)
                    should_retry = True
                elif resp.status != 200:
                    err = await resp.text()
                    return None, {}, resp.status, err
                else:
                    buf = ""
                    content_stream = resp.content
                    while True:
                        if not _got_first_token:
                            remaining = _stall_remaining()
                            if remaining <= 0:
                                _raise_stall()
                        else:
                            remaining = None

                        try:
                            raw = await asyncio.wait_for(
                                content_stream.readany(),
                                timeout=remaining,
                            )
                        except asyncio.TimeoutError:
                            _raise_stall()

                        if not raw:
                            break

                        buf += raw.decode("utf-8", errors="replace")
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            line = line.strip()
                            if not line or line.startswith(":"):
                                continue
                            if not line.startswith("data: "):
                                continue
                            payload = line[6:].strip()
                            if payload == "[DONE]":
                                continue
                            try:
                                obj = json.loads(payload)
                                if "usage" in obj:
                                    usage = obj["usage"]
                                for ch in obj.get("choices", []):
                                    delta = ch.get("delta", {})
                                    txt = delta.get("content", "")
                                    reasoning = delta.get("reasoning", "")

                                    # Safely handle None values from OpenRouter
                                    if txt is None:
                                        txt = ""
                                    if reasoning is None:
                                        reasoning = ""

                                    if txt or reasoning:
                                        if txt:
                                            parts.append(txt)
                                        total_chars += len(txt) + len(reasoning)
                                        _got_first_token = True
                                        if on_progress:
                                            on_progress(total_chars)
                            except json.JSONDecodeError:
                                pass
                    return "".join(parts), usage, 200, ""
            finally:
                await resp_ctx.__aexit__(None, None, None)

            if not should_retry:
                # Defensive: control should have returned via one of the
                # branches above. Treat as non-retryable failure.
                return None, {}, last_status, last_err

            if on_retry:
                on_retry(last_status, attempt, _MAX_RETRIES, wait_s)
            await asyncio.sleep(wait_s)

        return None, {}, last_status, last_err

    async def _stream_retry_402(data: dict, err_text: str) -> tuple[str, dict] | None:
        """On 402, parse affordable token limit and retry with reduced max_tokens."""
        affordable = _credit_token_limit_from_error(err_text)
        if not affordable or affordable >= data.get("max_tokens", 0):
            return None
        retry_data = {**data, "max_tokens": max(1, affordable)}
        print(
            f"    {_tri} {S.DIM}{model_id} 402 — retrying with "
            f"max_tokens={retry_data['max_tokens']}{S.RST}"
        )
        content, usage, status, err = await _do_stream(retry_data)
        if status == 200 and content:
            return content, usage
        return None

    # Try reasoning formats in priority order; on 400 try the next format
    if reasoning_effort:
        attempts = _reasoning_attempts(model_id, reasoning_effort, model_max_tokens)
        for attempt_idx, extra in enumerate(attempts):
            data = {**base_data, **extra}
            try:
                content, usage, status, err = await _do_stream(data)
                if status == 200:
                    if content:
                        return content, usage
                    # empty content at 200 — stop trying reasoning formats
                elif status == 400:
                    remaining = len(attempts) - attempt_idx - 1
                    if remaining > 0:
                        print(
                            f"    {_tri} {S.DIM}{model_id} 400 w/ reasoning"
                            f" — trying next format ({remaining} left)…{S.RST}"
                        )
                        continue
                    print(
                        f"    {_tri} {S.DIM}{model_id} 400 w/ reasoning — retrying without…{S.RST}"
                    )
                elif status == 402:
                    result = await _stream_retry_402(data, err)
                    if result is not None:
                        return result
                    raise RuntimeError(f"HTTP 402: {err[:120].strip()}")
                else:
                    # 429/5xx are exhausted retries from _do_stream — raise so
                    # the runner records a clean failure rather than falling
                    # through to a no-reasoning attempt against the same
                    # throttled upstream (which would just throttle again).
                    print(f"    {_tri} {S.DIM}{model_id}: {status} — {err[:120].strip()}{S.RST}")
                    raise RuntimeError(f"HTTP {status}: {err[:120].strip()}")
            except (asyncio.CancelledError, RuntimeError):
                raise
            except asyncio.TimeoutError:
                print(f"    {_tri} {S.DIM}{model_id} reasoning err: timeout{S.RST}")
            except aiohttp.ClientError as exc:
                print(f"    {_tri} {S.DIM}{model_id} reasoning err: API err ({exc}){S.RST}")
            except Exception as exc:
                exc_str = str(exc) or exc.__class__.__name__
                print(f"    {_tri} {S.DIM}{model_id} reasoning err: {exc_str}{S.RST}")
            break  # non-400 failures skip remaining reasoning formats

    # Final attempt — without reasoning
    content, usage, status, err = await _do_stream(base_data)
    if status == 200 and content:
        return content, usage
    if status == 200:
        raise RuntimeError("empty response")
    if status == 402:
        result = await _stream_retry_402(base_data, err)
        if result is not None:
            return result
        raise RuntimeError(f"HTTP 402: {err[:120].strip()}")
    if status == 400:
        limit = _context_limit_from_error_text(err)
        if limit is not None:
            prompt_tokens_est = max(1, len(prompt) // 4)
            retry_max = max(1, limit - prompt_tokens_est - 512)
            if retry_max < base_data["max_tokens"]:
                retry_data = {**base_data, "max_tokens": retry_max}
                content, usage, status, err = await _do_stream(retry_data)
                if status == 200 and content:
                    return content, usage
                if status == 200:
                    raise RuntimeError("empty response")
    raise RuntimeError(f"HTTP {status}: {err[:120].strip()}")


async def call_tts_speech(
    session: aiohttp.ClientSession,
    api_key: str,
    model_id: str,
    input_text: str,
    voice: str = "alloy",
    response_format: str = "mp3",
    speed: float | None = 1.0,
    on_progress: Callable[[int], None] | None = None,
    on_retry: RetryCallback | None = None,
) -> tuple[bytes, dict]:
    """Generate speech audio via OpenRouter's OpenAI-compatible TTS endpoint.

    Returns ``(audio_bytes, usage)``. The endpoint returns raw audio rather
    than JSON, so usage is a lightweight local metadata dict containing input
    character count, output byte count, and the optional generation id header.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "Benchmark Script",
    }
    data: dict[str, Any] = {
        "model": model_id,
        "input": input_text,
        "voice": voice,
        "response_format": response_format,
    }
    if speed is not None:
        data["speed"] = speed

    last_status = 0
    last_err = ""
    for attempt in range(1, _MAX_RETRIES + 2):
        retry_wait_s: float | None = None
        async with session.post(f"{API_URL}/audio/speech", headers=headers, json=data) as resp:
            if resp.status in _RETRYABLE_STATUSES and attempt <= _MAX_RETRIES:
                last_status = resp.status
                last_err = await resp.text()
                retry_wait_s = _retry_wait_seconds(resp.headers.get("Retry-After"), attempt)
                if on_retry:
                    on_retry(last_status, attempt, _MAX_RETRIES, retry_wait_s)
            elif resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {text[:120].strip()}")
            else:
                chunks: list[bytes] = []
                total_bytes = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    total_bytes += len(chunk)
                    if on_progress:
                        on_progress(total_bytes)

                audio = b"".join(chunks)
                if not audio:
                    raise RuntimeError("empty audio response")

                usage: dict[str, Any] = {
                    "input_characters": len(input_text),
                    "audio_bytes": total_bytes,
                }
                generation_id = resp.headers.get("X-Generation-Id")
                if generation_id:
                    usage["generation_id"] = generation_id
                return audio, usage

        if retry_wait_s is not None:
            await asyncio.sleep(retry_wait_s)
            continue

    raise RuntimeError(f"HTTP {last_status}: {last_err[:120].strip()}")


async def call_image_generation(
    session: aiohttp.ClientSession,
    api_key: str,
    model_id: str,
    prompt: str,
    modalities: list[str] | None = None,
    image_config: dict[str, str] | None = None,
    on_retry: RetryCallback | None = None,
) -> tuple[dict[str, Any], dict]:
    """Generate images through non-streaming /chat/completions.

    Returns the assistant message and usage metadata. Image data URLs are
    decoded by the image mode/runner so assistant text can be ignored there.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "Benchmark Script",
    }
    data: dict[str, Any] = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": modalities or ["image", "text"],
        "stream": False,
    }
    if image_config:
        data["image_config"] = image_config

    last_status = 0
    last_err = ""
    for attempt in range(1, _MAX_RETRIES + 2):
        retry_wait_s: float | None = None
        async with session.post(f"{API_URL}/chat/completions", headers=headers, json=data) as resp:
            if resp.status in _RETRYABLE_STATUSES and attempt <= _MAX_RETRIES:
                last_status = resp.status
                last_err = await resp.text()
                retry_wait_s = _retry_wait_seconds(resp.headers.get("Retry-After"), attempt)
                if on_retry:
                    on_retry(last_status, attempt, _MAX_RETRIES, retry_wait_s)
            elif resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {text[:120].strip()}")
            else:
                try:
                    body = await resp.json()
                    message = body["choices"][0]["message"]
                except (KeyError, IndexError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f"parse error: {exc}")
                if not isinstance(message, dict):
                    raise RuntimeError("parse error: assistant message was not an object")
                return message, body.get("usage", {})

        if retry_wait_s is not None:
            await asyncio.sleep(retry_wait_s)
            continue

    raise RuntimeError(f"HTTP {last_status}: {last_err[:120].strip()}")


def fetch_top_models(api_key: str, count: int = 12) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch models from OpenRouter, returning (top_for_menu, pricing_lookup).

    *top_for_menu* is a list of model dicts sorted by popularity score.
    *pricing_lookup* maps **every** model ID to its pricing dict so the
    caller can look up pricing for any model (including ones already in
    the user's config).
    """
    req = urllib.request.Request(
        f"{API_URL}/models?output_modalities=all",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        exc_str = str(exc) or exc.__class__.__name__
        print(f"    {_tri} {S.DIM}could not fetch models: {exc_str}{S.RST}")
        return [], {}

    all_models = data.get("data", [])
    non_free_model_ids = {
        m.get("id", "") for m in all_models if ":free" not in (m.get("id", "") or "")
    }

    # Build pricing/metadata lookup for every model. The extra private keys are
    # ignored by cost computation but let image mode choose OpenRouter modalities
    # from catalog metadata when available.
    pricing_lookup = {}
    for m in all_models:
        mid = m.get("id", "")
        if mid:
            arch = m.get("architecture", {}) or {}
            pricing_lookup[mid] = {
                **(m.get("pricing", {}) or {}),
                "__output_modalities": arch.get("output_modalities") or [],
                "__input_modalities": arch.get("input_modalities") or [],
            }

    # Filter candidates for the menu. Include regular text-output models,
    # speech-output models for TTS, and image-output models for Image mode.
    seen_slugs = set()
    filtered = []
    speech_filtered = []
    image_filtered = []
    for m in all_models:
        mid = m.get("id", "")
        arch = m.get("architecture", {})
        out_mods = arch.get("output_modalities") or []
        in_mods = arch.get("input_modalities") or []
        has_text_output = "text" in out_mods
        has_speech_output = "speech" in out_mods
        has_image_output = "image" in out_mods or is_image_model(mid, m)

        # Must accept text input and produce a supported output modality.
        if "text" not in in_mods or not (has_text_output or has_speech_output or has_image_output):
            continue
        # Skip audio-output models that are not dedicated speech/TTS models.
        if "audio" in out_mods and not has_speech_output:
            continue
        # Skip :free duplicate variants only when a non-free counterpart exists.
        if mid.endswith(":free") and mid.removesuffix(":free") in non_free_model_ids:
            continue
        # Skip OpenRouter utility models (routers, etc.) but keep stealth models
        if mid.startswith("openrouter/") and not is_stealth(mid):
            continue
        # Skip roleplay / her-specific models
        name_lower = m.get("name", "").lower()
        if "-her" in mid or "roleplay" in name_lower:
            continue
        # Deduplicate by canonical slug
        slug = m.get("canonical_slug", mid)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        if has_image_output:
            image_filtered.append(m)
        elif has_speech_output:
            speech_filtered.append(m)
        else:
            filtered.append(m)

    # Sort by popularity score descending. Reserve slices for speech/image
    # models so each dedicated config tab has useful catalog rows.
    filtered.sort(key=_model_score, reverse=True)
    speech_filtered.sort(key=_model_score, reverse=True)
    image_filtered.sort(key=_model_score, reverse=True)
    image_count = min(len(image_filtered), count, 20)
    speech_count = min(len(speech_filtered), max(0, count - image_count), 20)
    text_count = max(0, count - image_count - speech_count)
    return (
        filtered[:text_count]
        + speech_filtered[:speech_count]
        + image_filtered[:image_count]
    ), pricing_lookup
