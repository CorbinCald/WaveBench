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
def test_gemini_example_distinguishes_output_and_cumulative_budget(width):
    result = example()
    tracker = ProgressTracker(1, {"Gemini": result}, model_names=["Gemini"])
    values = tracker._harness_metrics("Gemini", result)
    assert values["tokens"].value == 78_094
    assert values["budget"]["used_tokens"] == 934_688
    assert values["budget"]["remaining_tokens"] == 65_312
    rendered = tracker._format_result_row("Gemini", result, 1, width)
    plain = re.sub(r"\033\[[0-9;?]*[a-zA-Z]", "", rendered)
    assert "Budget " not in plain
    assert "Token budget exhausted" in plain
    assert "next input ~80,687" in plain
    assert "17" in plain.split()
    assert all(len(line) <= width for line in plain.splitlines())


def test_live_budget_counts_cached_input_and_marks_unknown_estimates():
    tracker = ProgressTracker(1, {}, model_names=["Gemini"])
    result = example()
    tracker.update_harness("Gemini", result["usage"], 90, budget=result["harness"]["budget"])
    tracker.start_harness_turn("Gemini", 2000)
    tracker.update_harness_stream("Gemini", {}, 100)
    values = tracker._harness_metrics("Gemini")
    assert values["budget"]["used_tokens"] == 936_788
    assert values["budget"]["estimated"] is True
    assert tracker._format_harness_details("Gemini", 112) == []
    tracker.update_harness_stream(
        "Gemini",
        {
            "prompt_tokens": 3000,
            "completion_tokens": 100,
            "total_tokens": 3100,
            "prompt_tokens_details": {"cached_tokens": 2900},
        },
        100,
    )
    values = tracker._harness_metrics("Gemini")
    assert values["budget"]["used_tokens"] == 937_788
    assert values["budget"]["estimated"] is False
    assert result["usage"]["total_tokens"] == 934_688


def test_total_only_provider_snapshots_do_not_double_count_visible_output():
    tracker = ProgressTracker(1, {}, model_names=["Model"])
    tracker.update_harness(
        "Model",
        {"api_turns": 0},
        0,
        budget={
            "used_tokens": 0,
            "limit_tokens": 10_000,
            "remaining_tokens": 10_000,
            "estimated": False,
        },
    )
    tracker.start_harness_turn("Model", 500)
    for total, output in [(1000, 10), (1100, 20), (1200, 30)]:
        tracker.update_harness_stream("Model", {"total_tokens": total}, output)
        budget = tracker._harness_metrics("Model")["budget"]
        assert budget["used_tokens"] == total
        assert budget["estimated"] is False
    tracker.update_harness_stream("Model", {"total_tokens": 1200}, 35)
    budget = tracker._harness_metrics("Model")["budget"]
    assert budget["used_tokens"] == 1205
    assert budget["estimated"] is True


@pytest.mark.parametrize(
    "exc,kwargs,category",
    [
        (TurnError("wire guard", failure_code="stream_raw_limit"), {}, "stream_limit"),
        (BudgetError("total token budget exhausted"), {}, "token_budget"),
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
