from __future__ import annotations

import copy
import re

import pytest

from wavebench.tui.analytics import display_analytics
from wavebench.tui.analytics.harness import aggregate_harness


def result(
    *,
    status="success",
    prompt=1_000,
    output=100,
    cached=900,
    api_s=1,
    cost=2,
    tools=1,
    tool_failures=1,
    repair="not_needed",
    time_s=10,
):
    """The saved HarnessSession.result shape, including shared compaction usage."""
    return {
        "status": status,
        "time_s": time_s,
        "usage": {
            "api_turns": 3,
            "prompt_tokens": prompt,
            "completion_tokens": output,
            "total_tokens": prompt + output,
            "prompt_tokens_details": {"cached_tokens": cached},
            "cost": cost,
        },
        "retries": [],
        "harness": {
            "version": 1,
            "repair": repair,
            "tool_usage": {"calls": tools, "failures": tool_failures},
            "web_search": {"enabled": True, "calls": 2, "failures": 0},
            "web_fetch": {"enabled": False, "calls": 0, "failures": 0},
            "budget": {
                "used_tokens": prompt + output,
                "limit_tokens": 10_000,
                "remaining_tokens": max(0, 10_000 - prompt - output),
                "estimated": False,
            },
            "compaction": {"usage": {"api_turns": 1, "cost": 0.5}},
            "timing": {
                "generation_s": time_s,
                "repair_s": 0,
                "api_s": api_s,
                "tool_s": 1,
                "queued_s": 0,
                "setup_s": 1,
                "runtime_s": 1,
                "compaction_s": 0.1,
            },
        },
    }


def history(*results):
    return {
        "version": 1,
        "runs": [
            {"timestamp": "2026-09-12T12:00:00+00:00", "prompt": "Fixture", "models": models}
            for models in results
        ],
    }


def test_weights_follow_work_and_include_unsuccessful_runs():
    data = history(
        {"Model": result()},
        {
            "Model": result(
                status="failed",
                prompt=9_000,
                output=900,
                cached=0,
                api_s=99,
                cost=6,
                tools=9,
                tool_failures=1,
            )
        },
        {"Model": result(status="cancelled", cost=4)},
    )
    original = copy.deepcopy(data)
    models, total = aggregate_harness(data)
    model = models["Model"]
    assert (total.runs, total.passed, total.failed, total.cancelled) == (3, 1, 1, 1)
    assert model.speed.value == pytest.approx(1_100 / 101)
    assert model.cache.value == pytest.approx(1_800 / 11_000)
    assert model.tool_failures.value == pytest.approx(3 / 11)
    assert model.totals["completion_tokens"].value == 1_100
    assert model.totals["total_tokens"].value == 12_100
    assert total.totals["cost"].value == 12
    assert total.totals["unsuccessful_cost"].value == 10
    assert total.cost_per_pass.value == 12
    assert total.totals["compaction_cost"].value == 1.5  # already in the $12
    assert total.totals["turns"].value == 9  # compaction is not added again
    assert total.totals["web_search"].value == 6
    assert total.totals["web_fetch"].measurement(3).value == 0
    assert total.near_budget == 1
    assert data == original


def test_missing_and_partial_accounting_stays_unknown_or_lower_bound():
    partial = result(status="failed")
    partial["usage"] = {
        "api_turns": 2,
        "usage_complete": False,
        "completion_tokens": None,
        "known_completion_tokens": 80,
        "cost": None,
        "known_cost": 3,
    }
    legacy = {"status": "cancelled", "time_s": 5, "usage": {}, "harness": {"version": 1}}
    _, stats = aggregate_harness(
        history({"Model": result()}, {"Model": partial}, {"Model": legacy})
    )
    output = stats.totals["completion_tokens"].measurement(3)
    assert (output.value, output.prefix) == (180, "≥")
    assert stats.cost_per_pass.value == 5
    assert stats.cost_per_pass.prefix == "≥"
    assert stats.speed.value == 100  # partial usage cannot provide a speed
    assert stats.speed.count == stats.cache.count == 1
    assert stats.totals["turns"].measurement(3).prefix == "≥"
    _, old = aggregate_harness(history({"Model": legacy}))
    assert old.totals["completion_tokens"].measurement(1).value is None
    assert old.totals["cost"].measurement(1).value is None
    assert old.totals["tools"].measurement(1).value is None
    assert old.totals["web_search"].measurement(1).value is None
    assert old.cost_per_pass.value is None
    assert old.budget.mean is None
    assert old.latency() == (None, None)
    assert old.repair_known == 0


def test_success_timing_repair_failure_categories_and_estimated_budgets():
    repaired = result(repair="submitted", time_s=30)
    failed = result(status="failed", repair="abandoned", time_s=1000)
    failed["failure"] = {"category": "token_budget", "summary": "Token budget exhausted"}
    failed["harness"]["budget"].update(used_tokens=9_500, remaining_tokens=500, estimated=True)
    failed["retries"] = [{"status": 429, "wait_s": 2}]
    cancelled = result(status="cancelled", time_s=2000)
    cancelled["failure"] = failed["failure"]  # cancellation takes precedence
    _, stats = aggregate_harness(
        history({"Model": result()}, {"Model": repaired}, {"Model": failed}, {"Model": cancelled})
    )
    assert stats.latency() == (20, 30)  # failed/cancelled time excluded
    assert stats.first_pass == 1
    assert (stats.repairs, stats.recovered, stats.repair_known) == (2, 1, 4)
    assert stats.failures == {"Token budget": 1}
    assert stats.budget.mean == pytest.approx((0.11 * 3 + 0.95) / 4)
    assert stats.budget.estimated is True
    assert stats.near_budget == 1
    assert stats.totals["retries"].value == 1
    _, many = aggregate_harness(history(*[{"Model": result(time_s=i)} for i in range(1, 101)]))
    assert many.latency() == (50.5, 95)


def test_legacy_budgets_turns_and_top_level_cost_are_supported():
    saved = result()
    del saved["usage"]["api_turns"]
    del saved["usage"]["total_tokens"]
    saved["harness"]["turns"] = [{"usage": saved["usage"].copy()}]
    del saved["harness"]["budget"]
    saved["harness"].update(budget_tokens=1_100, config={"total_tokens": 10_000})
    saved["cost"] = 0  # stored cost wins, including explicit free calls
    _, stats = aggregate_harness(history({"Model": saved}))
    assert stats.totals["turns"].value == 1
    assert stats.budget.mean == 0.11
    assert stats.budget.estimated is False
    assert stats.totals["total_tokens"].value == 1_100
    assert stats.cost_per_pass.value == 0


@pytest.mark.parametrize("value", [None, -1, float("nan"), float("inf"), "123", True])
def test_invalid_measurements_do_not_poison_large_aggregates(value):
    saved = result()
    saved["cost"] = value
    saved["usage"].update(completion_tokens=value, cost=value)
    saved["usage"]["prompt_tokens_details"]["cached_tokens"] = value
    saved["harness"]["tool_usage"]["failures"] = value
    saved["harness"]["timing"]["api_s"] = value
    saved["harness"]["budget"]["limit_tokens"] = value
    _, stats = aggregate_harness(history({"Model": saved}))
    assert stats.speed.value is None
    assert stats.cache.value is None
    assert stats.tool_failures.value is None
    assert stats.budget.mean is None
    assert stats.totals["completion_tokens"].measurement(1).value is None
    assert stats.cost_per_pass.value is None


def plain_render(data, capsys, **kwargs):
    display_analytics(data, **kwargs)
    return re.sub(r"\033\[[0-9;?]*[a-zA-Z]", "", capsys.readouterr().out)


@pytest.mark.parametrize("columns", [60, 80, 120])
@pytest.mark.parametrize("compact", [False, True])
def test_rendering_large_counts_missing_usage_and_many_models(
    columns, compact, monkeypatch, capsys
):
    monkeypatch.setattr("wavebench.tui.analytics.table._tw", lambda: columns)
    missing = {"status": "failed", "usage": {}, "harness": {"version": 1}}
    data = history(
        {
            f"Harness-model-{i:02}": result(prompt=1_000_000_000, output=2_000_000, cost=100)
            for i in range(12)
        },
        {"Harness-model-00": missing},
        {"Harness-model-00": {"status": "success", "time_s": 1, "usage": {}, "cost": 999}},
    )
    text = plain_render(data, capsys, compact=compact)
    assert all(len(line) <= columns for line in text.splitlines())
    assert "Harness Totals" in text and "≥24,000,000" in text
    assert "≥$1,200.00" in text  # excludes the non-Harness run's $999
    assert "12 passed (92.3%)" in text
    if compact:
        assert "+3 more models" in text
        assert "wavebench --stats" in text
        assert "Passed active time" not in text
        assert "Recent Prompts" not in text
    else:
        assert "+3 more models" not in text
        assert "Harness-model-11 [harness]" in text
        assert "Passed active time" in text and "repair recovery" in text
        assert "Failure causes" in text and "Recent Prompts" in text


def test_rendered_lower_bounds_coverage_zero_and_sorting(monkeypatch, capsys):
    monkeypatch.setattr("wavebench.tui.analytics.table._tw", lambda: 120)
    partial = result(status="failed")
    partial["usage"] = {"known_completion_tokens": 2, "known_cost": 0.1}
    zero = result(cost=0, cached=0, tools=0, tool_failures=0)
    data = history({"Partial": partial, "Free": zero, "Priced": result(cost=5)})
    text = plain_render(data, capsys, sort_by="cost")
    assert (
        text.index("Free [harness]")
        < text.index("Priced [harness]")
        < text.index("Partial [harness]")
    )
    assert "Output ≥202 tk" in text
    assert "100.0 tk/s (2/3 runs)" in text
    assert "cache hit 45.0% (2/3 runs)" in text
    assert "cost/pass ≥$2.55" in text
    assert "$0.000" in text


def test_no_harness_panel_for_empty_or_legacy_only_history(capsys):
    assert "No history yet" in plain_render(history(), capsys)
    text = plain_render(history({"Text": {"status": "success", "time_s": 1}}), capsys)
    assert "Lifetime Analytics" in text
    assert "Harness Totals" not in text


@pytest.mark.parametrize("sort_by", ["speed", "cache", "tool_fail", "cost_per_pass", "p95"])
def test_harness_sorts_rank_measured_performance_before_missing_and_legacy(sort_by, capsys):
    data = history(
        {
            "Missing": {"status": "failed", "harness": {"version": 1}},
            "Legacy": {"status": "success", "time_s": 0.01, "cost": 0},
            "Slower": result(api_s=100, cost=10, cached=0, tool_failures=1, time_s=100),
            "Better": result(api_s=1, cost=1, cached=900, tool_failures=0, time_s=10),
        }
    )
    text = plain_render(data, capsys, sort_by=sort_by)
    assert text.index("Better [harness]") < text.index("Slower [harness]")
    assert text.index("Slower [harness]") < text.index("Missing [harness]")
    assert text.index("Slower [harness]") < text.index("Legacy")
