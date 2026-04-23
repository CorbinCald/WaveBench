#!/usr/bin/env python3
"""Effort-level audit for Claude Opus 4.7 via OpenRouter.

Sends the same complex prompt at three effort levels (low / high / xhigh)
and reports the wire payload, elapsed time, completion length, and token
usage — so we can verify both that the effort parameter is being
transmitted *and* that the upstream model is visibly adjusting its
behavior based on it.
"""
import asyncio
import json
import os
import sys
import time

# Import from the repo we're auditing.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import aiohttp
from wavebench.api import (
    load_api_key, call_model_streaming,
    _reasoning_attempts, _supported_efforts, _map_effort,
)

MODEL_ID = "anthropic/claude-opus-4.7"
EFFORTS = ["low", "high", "xhigh"]
MAX_TOKENS = 4000
PER_CALL_TIMEOUT_S = 900  # 15 minutes hard cap per call

PROMPT = (
    "Design and implement a Python function that computes the nth "
    "Fibonacci number in O(log n) time using matrix exponentiation. "
    "Include:\n"
    "1. A rigorous mathematical derivation of why repeated squaring of "
    "the 2x2 transformation matrix yields the nth Fibonacci number.\n"
    "2. A correct, idiomatic Python implementation handling n = 0, "
    "negative n (either via raising an exception or via the extension "
    "F(-n) = (-1)^(n+1) F(n) — justify your choice), and very large n.\n"
    "3. A short time and space complexity analysis.\n"
    "4. Verification table for n = 0, 1, 10, 50, 100.\n"
    "Be thorough and rigorous."
)


async def run_one_call(
    session: aiohttp.ClientSession,
    api_key: str,
    effort: str,
) -> dict:
    print(f"\n{'═' * 68}")
    print(f"BATCH: effort={effort!r}")
    print(f"{'═' * 68}")

    # Static inspection: the exact payload that `_reasoning_attempts` will
    # try first (we're verifying the variable is placed on the wire correctly).
    attempts = _reasoning_attempts(MODEL_ID, effort, MAX_TOKENS)
    print(f"  primary wire payload:  {attempts[0]}")
    print(f"  fallback payloads:     {len(attempts) - 1}")

    t0 = time.monotonic()
    progress_peak = [0]

    def on_progress(n: int) -> None:
        progress_peak[0] = n

    try:
        content, usage = await asyncio.wait_for(
            call_model_streaming(
                session, api_key, MODEL_ID, PROMPT,
                reasoning_effort=effort,
                on_progress=on_progress,
                max_tokens=MAX_TOKENS,
            ),
            timeout=PER_CALL_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - t0
        print(f"  TIMEOUT after {elapsed:.1f}s (cap {PER_CALL_TIMEOUT_S}s)")
        return {"effort": effort, "status": "timeout", "elapsed": elapsed}
    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"  FAILED after {elapsed:.1f}s: {exc!r}")
        return {"effort": effort, "status": "failed", "elapsed": elapsed,
                "error": repr(exc)}

    elapsed = time.monotonic() - t0
    print(f"  elapsed:              {elapsed:.1f}s")
    print(f"  content length:       {len(content):,} chars")
    print(f"  progress chars peak:  {progress_peak[0]:,}")
    print(f"  usage (raw):          {json.dumps(usage, indent=2)}")

    # First 200 chars of response as a sanity check
    snippet = content[:200].replace("\n", "\\n")
    print(f"  response preview:     {snippet}…")

    return {
        "effort": effort,
        "status": "ok",
        "elapsed": elapsed,
        "chars": len(content),
        "progress_peak": progress_peak[0],
        "usage": usage,
    }


async def main() -> None:
    api_key = load_api_key()
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY not available")
        sys.exit(1)

    # Static verification: the effort-mapping functions agree with our
    # expectations for Opus 4.7 (no clamping — it supports all 5 levels).
    print("=" * 68)
    print("STATIC INSPECTION — no network calls yet")
    print("=" * 68)
    supported = _supported_efforts(MODEL_ID)
    print(f"  _supported_efforts({MODEL_ID!r})")
    print(f"    = {supported}")
    print(f"  effort-clamp map (what the user's choice becomes on the wire):")
    for e in ["low", "medium", "high", "xhigh", "max"]:
        mapped = _map_effort(e, supported)
        tag = "" if mapped == e else f"  (clamped ← would NOT be 1:1)"
        print(f"    {e:>6}  →  {mapped:<6}{tag}")

    results = []
    async with aiohttp.ClientSession() as session:
        for effort in EFFORTS:
            r = await run_one_call(session, api_key, effort)
            results.append(r)

    # Summary table
    print("\n" + "=" * 68)
    print("SUMMARY")
    print("=" * 68)
    header = (f"{'effort':<8} {'status':<8} {'elapsed':>10} {'chars':>10} "
              f"{'prompt':>8} {'compl':>8} {'reason':>8} {'total':>8}")
    print(header)
    print("-" * len(header))
    for r in results:
        if r["status"] != "ok":
            print(f"{r['effort']:<8} {r['status']:<8} "
                  f"{r.get('elapsed', 0):>9.1f}s")
            continue
        u = r.get("usage") or {}
        pt = u.get("prompt_tokens", "?")
        ct = u.get("completion_tokens", "?")
        tt = u.get("total_tokens", "?")
        # Reasoning tokens may appear in completion_tokens_details (OpenAI-style)
        # or as a top-level "reasoning_tokens" field.
        details = u.get("completion_tokens_details") or {}
        rt = details.get("reasoning_tokens")
        if rt is None:
            rt = u.get("reasoning_tokens", "-")
        print(f"{r['effort']:<8} {r['status']:<8} "
              f"{r['elapsed']:>9.1f}s {r['chars']:>10,} "
              f"{str(pt):>8} {str(ct):>8} {str(rt):>8} {str(tt):>8}")

    print("\nDifferentiation check — we expect reasoning / completion tokens "
          "and elapsed time to grow monotonically from low → high → xhigh.")


if __name__ == "__main__":
    asyncio.run(main())
