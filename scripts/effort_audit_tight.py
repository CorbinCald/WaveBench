#!/usr/bin/env python3
"""Tighter effort-level audit for Claude Opus 4.7.

Runs n=3 samples at each of three effort levels (low / high / xhigh)
against a non-textbook concurrency-design prompt.  Parallel within an
effort phase, sequential across phases.  Reports per-call usage plus
per-effort aggregates so we can see differentiation emerge despite
temperature=0.1 sample variance.
"""
import asyncio
import json
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import aiohttp
from wavebench.api import (
    load_api_key, call_model_streaming,
    _reasoning_attempts, _supported_efforts, _map_effort,
)

MODEL_ID = "anthropic/claude-opus-4.7"
EFFORTS = ["low", "high", "xhigh"]
N_SAMPLES = 3
MAX_TOKENS = 16000
PER_CALL_TIMEOUT_S = 900  # 15 min hard cap per call
COST_CEILING_USD = 10.00  # abort if running total crosses this

# Non-textbook concurrency design problem: Python-specific MPMC lock-free
# queue.  The Python twist (GIL, no release/acquire primitives) is
# deliberately chosen to push the model off memorized patterns.
PROMPT = (
    "Design and implement in Python a bounded MPMC (multi-producer "
    "multi-consumer) lock-free ring buffer using only atomic "
    "compare-and-swap primitives (you may assume an atomic_cas(addr, "
    "expected, new) -> bool function is available).  The queue must:\n\n"
    "1. Support concurrent push and pop from multiple OS threads without "
    "any locks, mutexes, or condition variables.\n"
    "2. Be bounded (fixed capacity N), with push returning False if full "
    "and pop returning None if empty.\n"
    "3. Handle the ABA problem correctly — explain your scheme (tagged "
    "pointers? hazard pointers? epoch-based reclamation?) and why the "
    "alternatives are inferior for this use case.\n"
    "4. Be linearizable-FIFO-correct under contention: the observable "
    "order of pops must be consistent with some serialization of pushes "
    "that respects per-producer program order.\n"
    "5. Tolerate spurious CAS failures without deadlock or livelock.\n\n"
    "Provide:\n"
    "- The full implementation (Python-ish pseudocode fine where Python "
    "lacks the primitive, with a clear note on what the real primitive "
    "would be on CPython 3.12+).\n"
    "- A rigorous argument for linearizability — identify the "
    "linearization point of each operation and justify it.\n"
    "- A stress test sketch that would reliably detect lost-push, "
    "lost-pop, duplicate-pop, and reordering bugs under contention "
    "— not generic 'run it with 8 threads' advice.\n"
    "- Discussion of memory-ordering assumptions.  CPython technically "
    "provides sequential consistency via the GIL, but you should "
    "explain what ordering your design would REQUIRE on a weaker model "
    "(C11 memory_order_*) and why.\n"
    "- A back-of-the-envelope throughput comparison vs a Lock-based "
    "queue at k=1, 8, 64 threads.\n\n"
    "Be rigorous.  This is a subtle problem and a partial answer that "
    "waves at 'use CAS with a tag' is not acceptable — you must argue "
    "correctness."
)


def _reasoning_tokens(usage: dict) -> int:
    """Extract reasoning-token count from whichever field OpenRouter
    surfaces it in (OpenAI-style nested, or top-level).
    """
    details = (usage or {}).get("completion_tokens_details") or {}
    rt = details.get("reasoning_tokens")
    if rt is None:
        rt = (usage or {}).get("reasoning_tokens", 0) or 0
    return int(rt or 0)


async def run_one_call(
    session: aiohttp.ClientSession,
    api_key: str,
    effort: str,
    sample_idx: int,
) -> dict:
    tag = f"{effort}#{sample_idx + 1}"
    attempts = _reasoning_attempts(MODEL_ID, effort, MAX_TOKENS)
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
        print(f"  [{tag}] TIMEOUT after {elapsed:.1f}s")
        return {"effort": effort, "sample": sample_idx, "status": "timeout",
                "elapsed": elapsed, "primary": attempts[0]}
    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"  [{tag}] FAILED after {elapsed:.1f}s: {exc!r}")
        return {"effort": effort, "sample": sample_idx, "status": "failed",
                "elapsed": elapsed, "primary": attempts[0], "error": repr(exc)}

    elapsed = time.monotonic() - t0
    content_len = len(content)
    progress = progress_peak[0]
    # Delta between progress (includes reasoning chars) and content (text only)
    # gives us a char-level proxy for reasoning even if the usage field is 0.
    reasoning_chars = max(0, progress - content_len)
    u = usage or {}
    rt = _reasoning_tokens(u)
    ct = u.get("completion_tokens", 0) or 0
    cost = u.get("cost", 0.0) or 0.0
    truncated = (ct >= MAX_TOKENS)

    print(f"  [{tag}] ok  {elapsed:>6.1f}s  compl={ct:>5}  "
          f"reason_tok={rt:>5}  reason_chars={reasoning_chars:>5}  "
          f"chars={content_len:>6,}  cost=${cost:.4f}"
          f"{'  ⚠TRUNC' if truncated else ''}")
    return {
        "effort": effort,
        "sample": sample_idx,
        "status": "ok",
        "primary": attempts[0],
        "elapsed": elapsed,
        "content_chars": content_len,
        "reasoning_chars": reasoning_chars,
        "completion_tokens": ct,
        "reasoning_tokens": rt,
        "prompt_tokens": u.get("prompt_tokens", 0) or 0,
        "total_tokens": u.get("total_tokens", 0) or 0,
        "cost": cost,
        "truncated": truncated,
    }


async def main() -> None:
    api_key = load_api_key()
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY not found")
        sys.exit(1)

    print("=" * 72)
    print("TIGHT EFFORT AUDIT — Opus 4.7, n=3 per effort, max_tokens=16000")
    print("=" * 72)
    supported = _supported_efforts(MODEL_ID)
    print(f"  _supported_efforts({MODEL_ID}) = {supported}")
    print(f"  primary wire payloads we will send:")
    for e in EFFORTS:
        print(f"    {e:>6}  →  {_reasoning_attempts(MODEL_ID, e, MAX_TOKENS)[0]}")
    print(f"  prompt length:   ~{len(PROMPT)} chars")
    print(f"  total calls:     {N_SAMPLES * len(EFFORTS)}")
    print(f"  cost ceiling:    ${COST_CEILING_USD:.2f} (abort if exceeded)")

    all_results: dict = {}
    running_cost = 0.0

    async with aiohttp.ClientSession() as session:
        for effort in EFFORTS:
            print(f"\n{'─' * 72}")
            print(f"PHASE: effort={effort!r}  (n={N_SAMPLES} concurrent)")
            print(f"{'─' * 72}")
            phase_t0 = time.monotonic()
            tasks = [
                run_one_call(session, api_key, effort, i)
                for i in range(N_SAMPLES)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=False)
            phase_elapsed = time.monotonic() - phase_t0
            all_results[effort] = results

            phase_cost = sum(r.get("cost", 0.0) for r in results
                             if r["status"] == "ok")
            running_cost += phase_cost
            print(f"  phase wall time: {phase_elapsed:.1f}s   "
                  f"phase cost: ${phase_cost:.4f}   "
                  f"running total: ${running_cost:.4f}")
            if running_cost > COST_CEILING_USD:
                print(f"  ABORT: cost ceiling ${COST_CEILING_USD:.2f} "
                      f"exceeded at ${running_cost:.4f}")
                break

    # ── Per-effort aggregates ──────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("PER-EFFORT AGGREGATES (mean ± stdev across n samples)")
    print("=" * 72)
    header = (f"{'effort':<8} {'n':>3} {'elapsed_s':>20} "
              f"{'compl_tok':>18} {'reason_chars':>18} {'trunc':>6} "
              f"{'cost':>9}")
    print(header)
    print("-" * len(header))

    def _agg(vals: list) -> str:
        if not vals:
            return "—"
        if len(vals) == 1:
            return f"{vals[0]:.1f}"
        m = statistics.mean(vals)
        s = statistics.stdev(vals)
        return f"{m:.1f} ± {s:.1f}"

    for effort in EFFORTS:
        rows = [r for r in all_results.get(effort, []) if r["status"] == "ok"]
        if not rows:
            print(f"{effort:<8}  (no successful calls)")
            continue
        elapsed = [r["elapsed"] for r in rows]
        compl = [r["completion_tokens"] for r in rows]
        rchars = [r["reasoning_chars"] for r in rows]
        trunc_cnt = sum(1 for r in rows if r["truncated"])
        cost = sum(r["cost"] for r in rows)
        print(f"{effort:<8} {len(rows):>3} "
              f"{_agg(elapsed):>20} "
              f"{_agg(compl):>18} "
              f"{_agg(rchars):>18} "
              f"{trunc_cnt}/{len(rows):>3} "
              f"${cost:>7.4f}")

    # ── Differentiation verdict ────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("DIFFERENTIATION VERDICT")
    print("=" * 72)
    means = {}
    for effort in EFFORTS:
        rows = [r for r in all_results.get(effort, []) if r["status"] == "ok"]
        if rows:
            means[effort] = {
                "elapsed": statistics.mean([r["elapsed"] for r in rows]),
                "compl": statistics.mean([r["completion_tokens"] for r in rows]),
                "rchars": statistics.mean([r["reasoning_chars"] for r in rows]),
                "rtok": statistics.mean([r["reasoning_tokens"] for r in rows]),
            }
    if len(means) == 3:
        ordered = [means[e]["compl"] for e in EFFORTS]
        print(f"  completion_tokens mean:  "
              f"low={ordered[0]:.0f}  high={ordered[1]:.0f}  "
              f"xhigh={ordered[2]:.0f}")
        mono = ordered[0] <= ordered[1] <= ordered[2]
        print(f"  monotonic low→high→xhigh? {mono}")

        ordered_r = [means[e]["rchars"] for e in EFFORTS]
        print(f"  reasoning_chars mean:    "
              f"low={ordered_r[0]:.0f}  high={ordered_r[1]:.0f}  "
              f"xhigh={ordered_r[2]:.0f}")
        mono_r = ordered_r[0] <= ordered_r[1] <= ordered_r[2]
        print(f"  monotonic low→high→xhigh? {mono_r}")

        ordered_t = [means[e]["elapsed"] for e in EFFORTS]
        print(f"  elapsed (s) mean:        "
              f"low={ordered_t[0]:.1f}  high={ordered_t[1]:.1f}  "
              f"xhigh={ordered_t[2]:.1f}")

    # Dump raw results for reference
    print(f"\n  grand total cost: ${running_cost:.4f}")
    dump_path = os.path.join(ROOT, "scripts", "effort_audit_tight_results.json")
    with open(dump_path, "w") as fh:
        json.dump({
            "model": MODEL_ID,
            "efforts": EFFORTS,
            "n_samples": N_SAMPLES,
            "max_tokens": MAX_TOKENS,
            "results": all_results,
            "total_cost_usd": running_cost,
        }, fh, indent=2, default=str)
    print(f"  raw results dumped to: {dump_path}")


if __name__ == "__main__":
    asyncio.run(main())
