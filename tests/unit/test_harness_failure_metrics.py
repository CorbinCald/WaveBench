from __future__ import annotations

import asyncio
import json
import re

import pytest

from wavebench.harness.failure import failure_record, failure_summary, result_failure
from wavebench.harness.session import BudgetError
from wavebench.harness.transport import TurnError
from wavebench.storage import load_history, record_run, save_history
from wavebench.tui.analytics.table import display_analytics
from wavebench.tui.progress import ProgressTracker


def example():
    budget = {
        "used_tokens": 934_688,
        "limit_tokens": 1_000_000,
        "remaining_tokens": 65_312,
        "estimated": False,
        "next_input_tokens_estimate": 80_687,
    }
    return {
        "status": "failed",
        "time_s": 90,
        "error": "total token budget cannot fit another request",
        "failure": failure_record(
            BudgetError("total token budget cannot fit another request"), budget=budget
        ),
        "usage": {
            "api_turns": 8,
            "prompt_tokens": 856_594,
            "completion_tokens": 78_094,
            "total_tokens": 934_688,
            "prompt_tokens_details": {"cached_tokens": 800_000},
            "cost": 0.1,
        },
        "harness": {
            "budget": budget,
            "timing": {"api_s": 90},
            "web_search": {"enabled": True, "calls": 17},
        },
    }


@pytest.mark.parametrize("width", [52, 72, 112])
def test_legacy_token_budget_failure_still_renders(width):
    """Harness version 1 results ended on a cumulative token budget; history keeps them readable."""
    result = example()
    tracker = ProgressTracker(1, {"Gemini": result}, model_names=["Gemini"])
    values = tracker._harness_metrics("Gemini", result)
    assert values["tokens"].value == 78_094
    assert "budget" not in values
    rendered = tracker._format_result_row("Gemini", result, 1, width)
    plain = re.sub(r"\033\[[0-9;?]*[a-zA-Z]", "", rendered)
    assert "Budget " not in plain
    assert "Token budget exhausted" in plain
    assert "next input ~80,687" in plain
    assert "17" in plain.split()
    assert all(len(line) <= width for line in plain.splitlines())


@pytest.mark.parametrize(
    "exc,kwargs,category",
    [
        (TurnError("wire guard", failure_code="stream_raw_limit"), {}, "stream_limit"),
        (BudgetError("total token budget exhausted"), {}, "token_budget"),
        (BudgetError("building exceeded 50 model turns"), {}, "harness_limit"),
        (
            TurnError("conversation exceeds the model context window; no request sent"),
            {},
            "context_window",
        ),
        (TurnError("malformed SSE JSON", failure_code="malformed_stream"), {}, "model_protocol"),
        (RuntimeError("exit 1"), {"runtime": True}, "project_runtime"),
        (asyncio.CancelledError(), {}, "cancelled"),
    ],
)
def test_distinct_structured_failure_categories(exc, kwargs, category):
    assert failure_record(exc, **kwargs)["category"] == category


@pytest.mark.parametrize("columns", [60, 80, 120])
def test_history_preserves_budgets_failures_unknown_usage_and_legacy_records(
    tmp_path, monkeypatch, capsys, columns
):
    monkeypatch.setattr("wavebench.tui.analytics.table._tw", lambda: columns)
    monkeypatch.chdir(tmp_path)
    old = {
        "status": "failed",
        "time_s": 1,
        "error": "stream byte budget exhausted",
        "usage": {},
        "harness": {"version": 1},
    }
    save_history({"version": 1, "runs": [{"models": {"Old": old}}]})
    result = example()
    result["usage"].update(completion_tokens=None, cost=None)
    record_run("safe fixture", None, 90, {"Gemini": result})
    history = load_history()
    saved = history["runs"][-1]["models"]["Gemini"]
    assert saved["failure"] == result["failure"]
    assert saved["harness"]["budget"] == result["harness"]["budget"]
    assert saved["usage"]["completion_tokens"] is None
    assert saved["usage"]["cost"] is None
    assert result_failure(old)["category"] == "stream_limit"
    display_analytics(history)
    rendered = capsys.readouterr().out
    assert "Token budget exhausted" in rendered and "Stream limit reached" in rendered
    assert "934,688/1,000,000" in rendered
    assert "next input ~80,687" in rendered
    plain = re.sub(r"\033\[[0-9;?]*[a-zA-Z]", "", rendered)
    assert all(len(line) <= columns for line in plain.splitlines())
    saved["status"] = "success"
    assert result_failure(saved) is None
    saved["status"] = "cancelled"
    assert failure_summary(saved) == "Cancelled"
    assert json.loads(json.dumps(history))["version"] == 1
