"""Harness metrics remain visible across streaming, tools, and terminal outcomes."""

from __future__ import annotations

import os
import re
from itertools import pairwise
from types import SimpleNamespace

import pytest

from wavebench.harness.accounting import Measurement, cache_read_ratio, reported_total
from wavebench.tui.progress import ProgressTracker
from wavebench.tui.progress import tracker as module


def plain(text):
    return re.sub(r"\033\[[0-9;?]*[a-zA-Z]", "", text).replace("\r", "")


@pytest.fixture
def tracker(monkeypatch):
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 10.0))
    instance = ProgressTracker(1, {}, model_names=["model"])
    instance.update_harness(
        "model",
        {
            "api_turns": 2,
            "prompt_tokens": 1000,
            "prompt_tokens_details": {"cached_tokens": 500},
            "total_tokens": 1200,
            "completion_tokens": 200,
            "cost": 0.015,
        },
        4.0,
    )
    instance.update_harness_tools("model", {"calls": 4, "failures": 1})
    instance.set_phase("model", "building")
    return instance


def test_streaming_estimates_reset_per_turn_and_settle_to_provider_totals(tracker, monkeypatch):
    tracker.start_harness_turn("model", 500)
    tracker.update("model", 400)
    monkeypatch.setattr(module.time, "monotonic", lambda: 12.0)
    tracker.update_harness_stream("model", {}, 100)
    row = plain(tracker._format_harness_metrics("model"))
    assert "~1,800 tk" in row
    assert 90 < tracker._harness_metrics("model")["rate"] < 100
    assert "$0.015" in row and "3 turns" in row

    tracker.update_harness(
        "model",
        {"api_turns": 3, "total_tokens": 1900, "completion_tokens": 300, "cost": 0.02},
        6.0,
    )
    tracker.set_phase("model", "linting")
    row = plain(tracker._format_harness_metrics("model"))
    assert "1,900 tk" in row and "0 tk/s" in row and "~" not in row
    assert "$0.020" in row and "3 turns" in row
    tracker.start_harness_turn("model", 1000)
    tracker.update("model", 40)
    tracker.update_harness_stream("model", {}, 10)
    row = plain(tracker._format_harness_metrics("model"))
    assert "~2,910 tk" in row and "4 turns" in row
    assert tracker._wave_completed_chars + tracker._active["model"]["chars"] == 440


@pytest.mark.parametrize("status", ["success", "failed", "cancelled"])
@pytest.mark.parametrize("known", [True, False])
@pytest.mark.parametrize("columns", [60, 80, 110, 120])
def test_metrics_survive_terminal_outcomes_and_unknown_usage(
    tracker, monkeypatch, capsys, status, known, columns
):
    usage = tracker._harness["model"]["usage"].copy()
    if not known:
        usage.update(
            prompt_tokens=None,
            total_tokens=None,
            completion_tokens=None,
            cost=None,
            cost_requires_provider=True,
        )
    tracker._results["model"] = {
        "status": status,
        "time_s": 100,
        "file": "a/long/project/path/" * 8,
        "error": "intentional\nfailure with multiple lines",
        "usage": usage,
        "harness": {
            "attempts": [{}],
            "timing": {"api_s": 4.0},
            "tool_usage": {"calls": 4, "failures": 1},
        },
    }
    monkeypatch.setattr(module, "_tw", lambda: columns)
    tracker._render_final()
    output = plain(capsys.readouterr().out)
    rows = [line for line in output.splitlines() if "model" in line]
    assert len(rows) == 1
    row = rows[0]
    assert "2" in row and ("1m40s" in row or "1m 40s" in row or "2m" in row)
    assert all(len(line) <= columns for line in output.splitlines())
    assert "4" in row and ("25.0%" in row or "25%" in row)
    assert "TOK" in output and "COST" in output and "FAIL" in output
    if known:
        assert "1,200" in row and "50" in row and "$0.015" in row
        assert "50.0%" in row or "50%" in row
    else:
        assert row.count("—") == 4  # Tokens, speed, cost, and cache stay explicitly unknown.


@pytest.mark.parametrize("phase", ["building", "compacting", "linting", "running", "repairing"])
@pytest.mark.parametrize("columns", [60, 80, 110, 120])
async def test_live_frame_keeps_phase_and_all_metrics(tracker, monkeypatch, phase, columns):
    monkeypatch.setattr(module.time, "monotonic", lambda: 15.0)
    tracker.set_phase("model", phase)
    monkeypatch.setattr(
        module.shutil, "get_terminal_size", lambda *args: os.terminal_size((columns, 24))
    )
    frames = []

    def capture(frame):
        frames.append(plain(frame))
        tracker._running = False

    monkeypatch.setattr(tracker, "_flush_frame", capture)
    tracker._running = True
    await tracker._animate()
    assert len(frames) == 1
    frame = frames[0]
    rows = [line for line in frame.splitlines() if "model" in line]
    assert len(rows) == 1
    row = rows[0]
    assert phase[:4] in row and "5.0s" in row
    assert "1,200" in row and "0" in row and "2" in row
    assert frame.count("$0.015") == 2  # Per-model cost and the batch total.
    assert "50.0%" in row or "50%" in row
    assert "25.0%" in row or "25%" in row
    assert "4" in row
    assert frame.count("TOK") == 1 and frame.count("COST") == 1
    assert all(len(line) <= columns for line in frame.splitlines())
    assert len([line for line in frame.splitlines() if "1,200" in line]) == 1


@pytest.mark.parametrize("count,hidden", [(1, 0), (2, 0), (5, 2), (7, 4)])
async def test_short_terminal_reserves_room_for_hidden_models(tracker, monkeypatch, count, hidden):
    tracker._model_names = [f"model-{i}" for i in range(count)]
    for name in tracker._model_names:
        tracker.update_harness(name, tracker._harness["model"]["usage"], 4.0)
        tracker.set_phase(name, "building")
    monkeypatch.setattr(
        module.shutil, "get_terminal_size", lambda *args: os.terminal_size((80, 10))
    )
    frames = []

    def capture(frame):
        frames.append(plain(frame))
        tracker._running = False

    monkeypatch.setattr(tracker, "_flush_frame", capture)
    tracker._running = True
    await tracker._animate()
    assert frames[0].count("model-") == count - hidden
    if hidden:
        assert f"+{hidden} more" in frames[0]
    else:
        assert "more…" not in frames[0]
    assert len(frames[0].splitlines()) <= 10


@pytest.mark.parametrize("width", [52, 72, 102, 112])
def test_single_row_preserves_metrics_during_retries(tracker, width):
    tracker.note_retry("model", 429, 1, 3, 2)
    row = plain(tracker._format_harness_row("model", width))
    assert "429" in row
    if width >= 72:
        assert "1/3" in row
    if width >= 100:
        assert "2s" in row
    assert "1,200" in row and "$0.015" in row
    assert "50.0%" in row or "50%" in row
    assert "25.0%" in row or "25%" in row
    assert "\n" not in row and len(row) <= width


def test_single_row_columns_stay_aligned_as_counters_grow(tracker):
    before = plain(tracker._format_harness_row("model", 72))
    tracker.update_harness(
        "model", {"api_turns": 999, "total_tokens": 123456789, "cost": 999.99}, 40
    )
    after = plain(tracker._format_harness_row("model", 72))
    assert "\n" not in after
    for old, new in (("1,200", "123.5M"), ("$0.015", "$999.99")):
        assert before.index(old) + len(old) == after.index(new) + len(new)
    assert len(before) == len(after) == 72


def test_full_percentages_and_tool_counts_fit_one_row(tracker):
    tracker._harness["model"]["usage"]["prompt_tokens_details"]["cached_tokens"] = 1000
    tracker.update_harness_tools("model", {"calls": 1000, "failures": 1000})
    row = plain(tracker._format_harness_row("model", 72))
    assert row.count("100%") == 2 and "1,000" in row
    assert "\n" not in row and len(row) == 72


@pytest.mark.parametrize("width", [52, 72, 112])
def test_compact_streaming_cost_never_rounds_small_charges_to_zero(tracker, width):
    tracker._harness["model"]["usage"].update(cost=0.0001, total_tokens=1_234_567)
    tracker.start_harness_turn("model", 1000)
    row = plain(tracker._format_harness_row("model", width))
    assert "~1.2M" in row or "~1,235,567" in row
    assert "$0.0001" in row or "$.0001" in row or "$1e-4" in row
    assert "\n" not in row and len(row) <= width


def test_cache_rate_uses_reported_tokens_and_keeps_settled_rate_until_new_report(tracker):
    tracker.start_harness_turn("model", 9000)
    tracker.update_harness_stream("model", {}, 100)
    assert tracker._harness_metrics("model")["cache_rate"] == 0.5
    current = {"prompt_tokens": 3000, "prompt_tokens_details": {"cached_tokens": 2700}}
    tracker.update_harness_stream("model", current, 100)
    assert tracker._harness_metrics("model")["cache_rate"] == 0.8
    tracker.update_harness_stream("model", current, 200)
    assert tracker._harness_metrics("model")["cache_rate"] == 0.8
    tracker.update_harness(
        "model",
        {"api_turns": 3, "prompt_tokens": 4000, "prompt_tokens_details": {"cached_tokens": 3200}},
        6,
    )
    assert tracker._harness_metrics("model")["cache_rate"] == 0.8
    assert tracker._harness_metrics("model")["tool_calls"] == 4
    tracker.start_harness_turn("model", 1000, model_id="compactor")
    tracker.update_harness_stream(
        "model", {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 0}}, 50
    )
    assert tracker._harness_metrics("model")["cache_rate"] == 0.64


def test_first_turn_and_missing_cache_usage_are_unknown_not_zero(tracker):
    tracker.update_harness("model", {"api_turns": 0}, 0)
    tracker.update_harness_tools("model", {"calls": 0, "failures": 0})
    tracker.start_harness_turn("model", 1000)
    row = plain(tracker._format_harness_tool_metrics("model"))
    assert "cache hit —" in row and "tools used 0" in row and "tool fail —" in row
    current = {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 0}}
    tracker.update_harness_stream("model", current, 50)
    assert tracker._harness_metrics("model")["cache_rate"] == 0
    tracker.update_harness("model", {"api_turns": 1}, 1)
    tracker.start_harness_turn("model", 1000)
    tracker.update_harness_stream("model", current, 50)
    assert tracker._harness_metrics("model")["cache_rate"] is None


@pytest.mark.parametrize(
    "prompt,cached,expected",
    [
        (100, 0, 0),
        (100, 100, 1),
        (0, 0, None),
        (100, None, None),
        (None, 20, None),
        (100, -1, None),
        (100, 101, None),
        (True, 1, None),
        (100, True, None),
        (100, 1.5, None),
    ],
)
def test_cache_rate_validates_native_counts(prompt, cached, expected):
    assert (
        cache_read_ratio(
            {"prompt_tokens": prompt, "prompt_tokens_details": {"cached_tokens": cached}}
        )
        == expected
    )


def test_first_turn_cost_is_live_and_settles_without_double_counting(tracker):
    tracker._pricing_lookup = {"vendor/expensive": {"prompt": "0.00001", "completion": "0.00005"}}
    tracker._model_id_map = {"model": "vendor/expensive"}
    tracker.update_harness("model", {"api_turns": 0}, 0)
    tracker.start_harness_turn("model", 1000)
    tracker.update_harness_stream("model", {}, 9000)
    live = tracker._harness_metrics("model")
    assert live["tokens"] == Measurement(10000, estimated=True)
    assert live["cost"].value == pytest.approx(0.46)
    assert live["cost"].estimated and live["turns"] == 1
    assert "~$0.46" in tracker._format_harness_metrics("model")
    assert tracker._cost_summary(live=True) == "~$0.46"
    tracker.note_retry("model", 503, 1, 3, 1)
    assert tracker._harness_metrics("model")["turns"] == 1

    # Native counts include hidden reasoning; the provider's cached-input bill
    # replaces the catalog estimate, rather than being added to it.
    usage = {"prompt_tokens": 1000, "completion_tokens": 12000, "total_tokens": 13000, "cost": 0.60}
    tracker.update_harness_stream("model", usage, 9000)
    assert tracker._harness_metrics("model")["cost"] == Measurement(0.60)
    assert tracker._harness_metrics("model")["tokens"] == Measurement(13000)
    tracker.update_harness("model", {**usage, "api_turns": 1}, 10)
    assert tracker._cost_summary(live=True) == "$0.60"
    assert tracker._harness_metrics("model")["turns"] == 1
    tracker._results["model"] = {
        "usage": {**usage, "api_turns": 1},
        "harness": {"timing": {"api_s": 10}},
    }
    assert tracker._cost_summary(live=True) == tracker._cost_summary() == "$0.60"


def test_summary_includes_queued_models_and_marks_unknown_costs(tracker):
    tracker.update_harness("queued", {"api_turns": 1, "cost": 1.5}, 2)
    tracker.update_harness("unknown", {"api_turns": 1, "cost": None}, 2)
    assert "cost unknown" in tracker._format_harness_metrics("unknown")
    assert tracker._cost_summary(live=True) == "≥$1.51"
    tracker.update_harness("unknown", {"api_turns": 1, "cost": 0}, 2)
    assert "$0.000" in tracker._format_harness_metrics("unknown")
    assert tracker._cost_summary(live=True) == "$1.51"


def test_failed_call_keeps_known_subtotals_and_never_fabricates_full_usage(tracker):
    usage = {
        "api_turns": 3,
        "total_tokens": None,
        "cost": None,
        "known_total_tokens": 1200,
        "known_cost": 0.015,
    }
    tracker.update_harness("model", usage, 5)
    row = tracker._format_harness_metrics("model")
    assert "≥1,200 tk" in row and "≥$0.015" in row and "3 turns" in row
    assert tracker._cost_summary(live=True) == "≥$0.015"


def test_compaction_estimate_uses_the_compactor_price(tracker):
    tracker._pricing_lookup = {
        "expensive": {"prompt": "1", "completion": "10"},
        "compactor": {"prompt": "0.00001", "completion": "0.00002"},
    }
    tracker._model_id_map = {"model": "expensive"}
    tracker.start_harness_turn("model", 1000, model_id="compactor")
    tracker.update_harness_stream("model", {}, 100)
    assert tracker._harness_metrics("model")["cost"].value == pytest.approx(0.027)
    assert tracker._harness_metrics("model")["turns"] == 3


def test_total_tokens_do_not_add_cache_or_reasoning_twice():
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "prompt_tokens_details": {"cached_tokens": 900, "cache_write_tokens": 10},
        "completion_tokens_details": {"reasoning_tokens": 80},
    }
    assert reported_total(usage) == 1100


def test_live_rate_softens_bursty_delivery_without_delaying_token_totals(tracker, monkeypatch):
    tracker.start_harness_turn("model", 100)
    samples = []
    for step in range(1, 241):
        now = 10.0 + step * 0.05
        monkeypatch.setattr(module.time, "monotonic", lambda now=now: now)
        # A buffered stream delivers 60 tokens every 1.2 seconds (50 tk/s).
        output = (step // 24) * 60
        if step % 24 == 0:
            tracker.update_harness_stream("model", {}, output)
        live = tracker._harness_metrics("model")
        assert live["tokens"].value == 1200 + 100 + output
        if step >= 72:
            samples.append(live["rate"])
    # The old one-second counter repeatedly jumped straight between 0 and 60.
    assert min(samples) > 30
    assert max(abs(b - a) for a, b in pairwise(samples)) < 20
    assert sum(samples) / len(samples) == pytest.approx(50, abs=5)


def test_live_rate_holds_between_refreshes_and_ignores_duplicate_callbacks(tracker, monkeypatch):
    tracker.start_harness_turn("model", 100)
    monkeypatch.setattr(module.time, "monotonic", lambda: 10.25)
    tracker.update_harness_stream("model", {}, 60)
    rate = tracker._harness_metrics("model")["rate"]
    assert 0 < rate < 60
    for now in (10.25, 10.30, 10.40, 10.49):
        monkeypatch.setattr(module.time, "monotonic", lambda now=now: now)
        tracker.update_harness_stream("model", {"completion_tokens": 600}, 60)
        assert tracker._harness_metrics("model")["rate"] == rate
    monkeypatch.setattr(module.time, "monotonic", lambda: 10.50)
    assert rate < tracker._harness_metrics("model")["rate"] < 60


def test_live_rate_tracks_token_growth_and_expires_during_five_second_pause(tracker, monkeypatch):
    tracker.start_harness_turn("model", 8800)
    assert tracker._harness_metrics("model")["rate"] == 0
    previous = tracker._harness_metrics("model")["tokens"].value
    # Totals stay exact while the displayed speed converges on five tokens per second.
    for second in range(1, 6):
        monkeypatch.setattr(module.time, "monotonic", lambda second=second: 10.0 + second)
        tracker.update_harness_stream("model", {}, 5 * second)
        live = tracker._harness_metrics("model")
        assert live["tokens"].value - previous == 5
        assert 0 < live["rate"] <= 5
        previous = live["tokens"].value
    assert live["rate"] == pytest.approx(5, abs=0.05)
    assert "~5 tk/s" in plain(tracker._format_harness_metrics("model"))

    previous_rate = live["rate"]
    for second in range(16, 21):
        monkeypatch.setattr(module.time, "monotonic", lambda second=second: float(second))
        # No callback at all while the network is idle; rendering must expire TPS.
        live = tracker._harness_metrics("model")
        assert live["tokens"] == Measurement(10025, estimated=True)
        assert 0 <= live["rate"] < previous_rate or live["rate"] == previous_rate == 0
        previous_rate = live["rate"]
        if second >= 18:
            assert live["rate"] == 0
            assert "0 tk/s" in plain(tracker._format_harness_metrics("model"))

    tracker.update_harness_stream("model", {}, 30)
    monkeypatch.setattr(module.time, "monotonic", lambda: 20.25)
    resumed_rate = tracker._harness_metrics("model")["rate"]
    assert 0 < resumed_rate < 5
    assert tracker._harness_metrics("model")["tokens"].value == 10030

    # Native billing (including hidden reasoning) must not look like streamed output.
    native = {"prompt_tokens": 8800, "completion_tokens": 500, "total_tokens": 9300}
    tracker.update_harness_stream("model", native, 30)
    assert tracker._harness_metrics("model")["tokens"] == Measurement(10500)
    assert tracker._harness_metrics("model")["rate"] == resumed_rate
    tracker.update_harness_stream("model", native, 30)  # EOF callback is not another burst.
    assert tracker._harness_metrics("model")["rate"] == resumed_rate

    tracker.update_harness("model", {**native, "api_turns": 3}, 14)
    tracker.set_phase("model", "linting")
    assert tracker._harness_metrics("model")["rate"] == 0
    tracker.start_harness_turn("model", 9000)
    assert tracker._harness_metrics("model")["rate"] == 0
    assert tracker._harness_metrics("model")["turns"] == 4


def test_intermediate_usage_does_not_freeze_later_streamed_tokens_or_cost(tracker, monkeypatch):
    tracker._pricing_lookup = {"model": {"prompt": "0.01", "completion": "0.02", "request": "1"}}
    tracker.update_harness("model", {}, 0)
    tracker.start_harness_turn("model", 100, model_id="model")
    native = {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140, "cost": 2.0}
    tracker.update_harness_stream("model", native, 20)
    assert tracker._harness_metrics("model")["tokens"] == Measurement(140)
    monkeypatch.setattr(module.time, "monotonic", lambda: 11.0)
    tracker.update_harness_stream("model", native, 25)
    live = tracker._harness_metrics("model")
    assert live["tokens"] == Measurement(145, estimated=True)
    assert live["cost"] == Measurement(2.1, estimated=True)
    rate = live["rate"]
    assert 0 < rate < 5
    assert tracker._cost_summary(live=True) == "~$2.10"
    # Final native totals replace the snapshot plus provisional new output.
    final = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost": 2.3}
    tracker.update_harness_stream("model", final, 25)
    live = tracker._harness_metrics("model")
    assert live["tokens"] == Measurement(150)
    assert live["cost"] == Measurement(2.3)
    assert live["rate"] == rate
