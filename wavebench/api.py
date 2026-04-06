import os
import json
import time
import urllib.request
import urllib.error
import asyncio
import re
import aiohttp
from typing import Optional, Tuple, Any, Dict, List, Callable

from wavebench.tui.styles import _tri, S
from wavebench.models import _model_score, is_stealth

API_URL = "https://openrouter.ai/api/v1"
_MODEL_CONTEXT_CACHE: Dict[str, int] = {}
_MODEL_CONTEXTS_ATTEMPTED = False
_MODEL_CONTEXT_LOCK = asyncio.Lock()

async def _load_model_context_lengths(
    session: aiohttp.ClientSession, api_key: str,
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
    session: aiohttp.ClientSession, api_key: str, model_id: str,
    prompt: str, fallback: int,
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

def _context_limit_from_error_text(err_text: str) -> Optional[int]:
    """Extract context limit from OpenRouter 400 text when present."""
    m = re.search(r"maximum context length is (\d+) tokens", err_text)
    if not m:
        return None
    try:
        limit = int(m.group(1))
    except (TypeError, ValueError):
        return None
    return limit if limit > 0 else None

def _credit_token_limit_from_error(err_text: str) -> Optional[int]:
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

def load_api_key() -> Optional[str]:
    """Load API key from environment or .env file."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key

    if os.path.exists(".env"):
        try:
            with open(".env", "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() == "OPENROUTER_API_KEY":
                            return v.strip().strip('"').strip("'")
        except Exception as exc:
            print(f"    {_tri} {S.DIM}could not read .env: {exc}{S.RST}")

    return None


def _reasoning_attempts(
    model_id: str, effort: str, max_tokens: int,
) -> List[Dict[str, Any]]:
    """Build an ordered list of reasoning payload overrides to try.

    Each entry is a dict to merge into the base request data.  When the
    provider returns 400 the caller moves to the next entry.  Returns an
    empty list for models known to never support reasoning.

    The order is:
      1. Provider-specific primary format (Anthropic enabled flag,
         standard OpenRouter effort for everything else).
      2. max_tokens-based reasoning (Anthropic / Gemini / Qwen style).
      3. Simple ``enabled: true`` flag.
      4. Top-level ``reasoning_effort`` (native provider APIs).
    Duplicates are suppressed automatically.
    """
    lower = model_id.lower()

    if "glm-4.7" in lower:
        return []

    is_anthropic = "anthropic/" in lower or "claude" in lower
    is_mercury = "inception/" in lower or "mercury" in lower

    seen: List[Dict[str, Any]] = []

    def _add(extra: Dict[str, Any]) -> None:
        if extra not in seen:
            seen.append(extra)

    if is_mercury:
        # Mercury-2 accepts OpenRouter's reasoning.effort (low/medium/high)
        # and Inception's native reasoning_summary param which gets passed
        # through to the provider, producing more detailed chain-of-thought
        # inline in the content.  Don't try max_tokens or enabled — they're
        # no-ops for Mercury, and combining effort + max_tokens causes a 400.
        _add({
            "reasoning": {"effort": effort},
            "reasoning_summary": True,
        })
        return seen

    if is_anthropic:
        _add({"reasoning": {"enabled": True}})
    else:
        _add({"reasoning": {"effort": effort}})

    budget = max(1024, int(max_tokens * 0.8))
    _add({"reasoning": {"max_tokens": budget}})
    _add({"reasoning": {"enabled": True}})
    _add({"reasoning_effort": effort})

    return seen


async def call_model_async(session: aiohttp.ClientSession, api_key: str, model_id: str, prompt: str,
                           reasoning_effort: Optional[str] = "high", return_usage: bool = False,
                           temperature: Optional[float] = None,
                           max_tokens: Optional[int] = None) -> Any:
    """Call the OpenRouter API for a specific model."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "Benchmark Script",
    }

    is_anthropic = "anthropic/" in model_id.lower() or "claude" in model_id.lower()

    fallback_max_tokens = 128000 if is_anthropic else 200000
    resolved_max_tokens = await _resolve_max_tokens(
        session, api_key, model_id, prompt, fallback=fallback_max_tokens)
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
        print(f"    {_tri} {S.DIM}{model_id} 402 — retrying with "
              f"max_tokens={retry_data['max_tokens']}{S.RST}")
        async with session.post(
            f"{API_URL}/chat/completions", headers=headers, json=retry_data,
        ) as r:
            if r.status == 200:
                body = await r.json()
                content = body['choices'][0]['message']['content']
                usage = body.get('usage', {})
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
                    headers=headers, json=data,
                ) as resp:
                    if resp.status == 200:
                        try:
                            body = await resp.json()
                            content = body['choices'][0]['message']['content']
                            usage = body.get('usage', {})
                            return (content, usage) if return_usage else content
                        except (KeyError, IndexError, json.JSONDecodeError) as e:
                            print(f"    {_tri} {S.DIM}parse error "
                                  f"({model_id}): {e}{S.RST}")
                    elif resp.status == 400:
                        remaining = len(attempts) - attempt_idx - 1
                        if remaining > 0:
                            print(f"    {_tri} {S.DIM}{model_id} 400 w/ reasoning"
                                  f" — trying next format ({remaining} left)…{S.RST}")
                            continue
                        print(f"    {_tri} {S.DIM}{model_id} 400 w/ reasoning"
                              f" — retrying without…{S.RST}")
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
                        print(f"    {_tri} {S.DIM}{model_id}: {resp.status}"
                              f" — {text[:120].strip()}{S.RST}")
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
                print(f"    {_tri} {S.DIM}{model_id} reasoning err: "
                      f"{exc_str}{S.RST}")
            break  # non-400 failures skip remaining reasoning formats

    # Final attempt — without reasoning
    async with session.post(
        f"{API_URL}/chat/completions",
        headers=headers, json=base_data,
    ) as resp:
        if resp.status == 200:
            try:
                body = await resp.json()
                content = body['choices'][0]['message']['content']
                usage = body.get('usage', {})
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
                        headers=headers, json=retry_data,
                    ) as retry_resp:
                        if retry_resp.status == 200:
                            body = await retry_resp.json()
                            content = body['choices'][0]['message']['content']
                            usage = body.get('usage', {})
                            return (content, usage) if return_usage else content
                        retry_text = await retry_resp.text()
                        raise RuntimeError(
                            f"HTTP {retry_resp.status}: {retry_text[:120].strip()}")
        raise RuntimeError(f"HTTP {resp.status}: {text[:120].strip()}")


async def call_model_streaming(
    session: aiohttp.ClientSession, api_key: str, model_id: str, prompt: str,
    reasoning_effort: Optional[str] = "high",
    on_progress: Optional[Callable[[int], None]] = None,
    max_tokens: Optional[int] = None,
) -> Tuple[str, dict]:
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
        session, api_key, model_id, prompt, fallback=fallback_max_tokens)
    model_max_tokens = (
        max(1, min(max_tokens, resolved_max_tokens))
        if max_tokens is not None
        else resolved_max_tokens
    )

    base_data: Dict[str, Any] = {
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

    async def _do_stream(data: dict) -> Tuple[Optional[str], dict, int, str]:
        """Execute one streaming request.  Returns (content, usage, status, err)."""
        nonlocal _got_first_token
        parts: list[str] = []
        usage: dict = {}
        total_chars = 0

        def _stall_remaining() -> float:
            return _stall_deadline - time.monotonic()

        def _raise_stall() -> None:
            elapsed_m = int(REASONING_STALL_TIMEOUT / 60)
            raise RuntimeError(
                f"no tokens after {elapsed_m}m (reasoning stall)")

        if not _got_first_token and _stall_remaining() <= 0:
            _raise_stall()

        post_timeout = (
            _stall_remaining() if not _got_first_token else None)
        resp_ctx = session.post(
            f"{API_URL}/chat/completions", headers=headers, json=data)
        try:
            resp = await asyncio.wait_for(
                resp_ctx.__aenter__(), timeout=post_timeout)
        except asyncio.TimeoutError:
            try:
                await resp_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            _raise_stall()

        try:
            if resp.status != 200:
                err = await resp.text()
                return None, {}, resp.status, err

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
                            if txt is None: txt = ""
                            if reasoning is None: reasoning = ""
                            
                            if txt or reasoning:
                                if txt:
                                    parts.append(txt)
                                total_chars += len(txt) + len(reasoning)
                                _got_first_token = True
                                if on_progress:
                                    on_progress(total_chars)
                    except json.JSONDecodeError:
                        pass
        finally:
            await resp_ctx.__aexit__(None, None, None)

        return "".join(parts), usage, 200, ""

    async def _stream_retry_402(data: dict, err_text: str) -> Optional[Tuple[str, dict]]:
        """On 402, parse affordable token limit and retry with reduced max_tokens."""
        affordable = _credit_token_limit_from_error(err_text)
        if not affordable or affordable >= data.get("max_tokens", 0):
            return None
        retry_data = {**data, "max_tokens": max(1, affordable)}
        print(f"    {_tri} {S.DIM}{model_id} 402 — retrying with "
              f"max_tokens={retry_data['max_tokens']}{S.RST}")
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
                        print(f"    {_tri} {S.DIM}{model_id} 400 w/ reasoning"
                              f" — trying next format ({remaining} left)…{S.RST}")
                        continue
                    print(f"    {_tri} {S.DIM}{model_id} 400 w/ reasoning"
                          f" — retrying without…{S.RST}")
                elif status == 402:
                    result = await _stream_retry_402(data, err)
                    if result is not None:
                        return result
                    raise RuntimeError(f"HTTP 402: {err[:120].strip()}")
                else:
                    print(f"    {_tri} {S.DIM}{model_id}: {status}"
                          f" — {err[:120].strip()}{S.RST}")
                    if status not in (429, 500, 502, 503, 504):
                        raise RuntimeError(f"HTTP {status}: {err[:120].strip()}")
            except (asyncio.CancelledError, RuntimeError):
                raise
            except asyncio.TimeoutError:
                print(f"    {_tri} {S.DIM}{model_id} reasoning err: timeout{S.RST}")
            except aiohttp.ClientError as exc:
                print(f"    {_tri} {S.DIM}{model_id} reasoning err: API err ({exc}){S.RST}")
            except Exception as exc:
                exc_str = str(exc) or exc.__class__.__name__
                print(f"    {_tri} {S.DIM}{model_id} reasoning err: "
                      f"{exc_str}{S.RST}")
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


def fetch_top_models(api_key: str, count: int = 12) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch models from OpenRouter, returning (top_for_menu, pricing_lookup).

    *top_for_menu* is a list of model dicts sorted by popularity score.
    *pricing_lookup* maps **every** model ID to its pricing dict so the
    caller can look up pricing for any model (including ones already in
    the user's config).
    """
    req = urllib.request.Request(
        f"{API_URL}/models",
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

    # Build pricing lookup for every model
    pricing_lookup = {}
    for m in all_models:
        mid = m.get("id", "")
        if mid:
            pricing_lookup[mid] = m.get("pricing", {})

    # Filter candidates for the menu
    seen_slugs = set()
    filtered = []
    for m in all_models:
        mid = m.get("id", "")
        arch = m.get("architecture", {})
        out_mods = arch.get("output_modalities") or []
        in_mods = arch.get("input_modalities") or []

        # Must accept text input and produce text output
        if "text" not in in_mods or "text" not in out_mods:
            continue
        # Skip audio / image output models
        if "audio" in out_mods or "image" in out_mods:
            continue
        # Skip :free duplicate variants (keep the paid original)
        if ":free" in mid:
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

        filtered.append(m)

    # Sort by popularity score descending
    filtered.sort(key=_model_score, reverse=True)
    return filtered[:count], pricing_lookup
