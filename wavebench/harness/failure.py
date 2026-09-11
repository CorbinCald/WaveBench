"""Stable failure categories and readable summaries, including older result records."""

from __future__ import annotations

import asyncio


def budget_record(harness: dict) -> dict:
    """Read current or legacy budget metadata without substituting output tokens."""
    if isinstance(harness.get("budget"), dict):
        return harness["budget"]
    used = harness.get("budget_tokens")
    limit = (harness.get("config") or {}).get("total_tokens")
    if type(used) is not int or type(limit) is not int:
        return {}
    turns = harness.get("turns") or []
    from .accounting import reported_total

    return {
        "used_tokens": used,
        "limit_tokens": limit,
        "remaining_tokens": max(0, limit - used),
        "estimated": any(reported_total(turn.get("usage") or {}) is None for turn in turns),
    }


def failure_record(
    exc: BaseException | str,
    *,
    phase: str = "",
    budget: dict | None = None,
    stream: dict | None = None,
    runtime: bool = False,
) -> dict:
    """Keep failure classification separate from raw logs and provider accounting."""
    text = str(exc).lower()
    code = getattr(exc, "failure_code", None)
    diagnostics = getattr(exc, "diagnostics", None) or stream or {}
    if isinstance(exc, asyncio.CancelledError) or code == "stream_cancelled":
        category, code, summary = "cancelled", "cancelled", "Cancelled"
    elif (
        code
        in {
            "stream_raw_limit",
            "stream_output_limit",
            "stream_frame_limit",
            "stream_assembly_limit",
            "stream_timeout",
            "stream_idle_timeout",
        }
        or "stream byte budget" in text
    ):
        category, code, summary = "stream_limit", code or "stream_raw_limit", "Stream limit reached"
    elif (
        "token budget" in text or "context budget" in text or "context cannot be compacted" in text
    ):
        category, code, summary = (
            "token_budget",
            code or "token_budget_exhausted",
            "Token budget exhausted",
        )
    elif code in {"unsupported_tools", "reasoning_rejected", "http_error"}:
        category, summary = (
            "model_protocol",
            {
                "unsupported_tools": "Tool calling unsupported",
                "reasoning_rejected": "Reasoning setting rejected",
                "http_error": "Provider request rejected",
            }[code],
        )
    elif type(exc).__name__ == "BudgetError" or isinstance(exc, asyncio.TimeoutError):
        category, code, summary = (
            "harness_limit",
            "time_or_turn_limit",
            "Harness time or turn limit reached",
        )
    elif code or type(exc).__name__ == "TurnError" or "unsupported tool" in text:
        category, code, summary = (
            "model_protocol",
            code or "invalid_response",
            "Response or tool protocol failed",
        )
        if code == "output_truncated":
            summary = "Output allowance exhausted"
        elif code == "project_abandoned":
            summary = "Model ended without submission"
    elif runtime:
        category, code, summary = (
            "project_runtime",
            "project_runtime_failed",
            "Project runtime failed",
        )
    elif type(exc).__name__ == "SetupError":
        category, code, summary = (
            "environment",
            "environment_setup_failed",
            "Runtime environment unavailable",
        )
    else:
        category, code, summary = "unknown", "benchmark_failed", "Benchmark failed"
    record = {"category": category, "code": code, "summary": summary, "phase": phase}
    if diagnostics:
        record["diagnostics"] = diagnostics
    if category == "token_budget" and budget:
        record["budget"] = budget.copy()
    return record


def result_failure(result: dict) -> dict | None:
    """Success/cancellation win over stale errors; old failures remain readable."""
    status = result.get("status")
    if status == "success":
        return None
    if status == "cancelled":
        return {"category": "cancelled", "code": "cancelled", "summary": "Cancelled"}
    failure = result.get("failure")
    if isinstance(failure, dict) and failure.get("summary"):
        return failure
    if status != "failed" or not result.get("harness"):
        return None
    harness = result["harness"]
    return failure_record(
        result.get("error") or "",
        phase=harness.get("phase", ""),
        budget=budget_record(harness),
        runtime=bool(harness.get("attempts")),
    )


def failure_summary(result: dict) -> str:
    failure = result_failure(result)
    if not failure:
        return ""
    summary = failure["summary"]
    budget = failure.get("budget") or {}
    remaining = budget.get("remaining_tokens")
    next_input = budget.get("next_input_tokens_estimate")
    if type(remaining) is int and type(next_input) is int:
        summary += f"\n{remaining:,} tokens remain; next input ~{next_input:,}"
    elif failure.get("category") == "stream_limit":
        limit = (failure.get("diagnostics") or {}).get("limit")
        if limit:
            summary += f" ({limit.replace('_', ' ')})"
    return summary
