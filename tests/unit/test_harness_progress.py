"""Harness metrics remain visible across streaming, tools, and terminal outcomes."""

from __future__ import annotations

import os
import re
from types import SimpleNamespace

import pytest

from wavebench.harness.accounting import Measurement, reported_total
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
        {"api_turns": 2, "total_tokens": 1200, "completion_tokens": 200, "cost": 0.015},
        4.0,
    )
    instance.set_phase("model", "building")
    return instance


def test_streaming_estimates_reset_per_turn_and_settle_to_provider_totals(tracker, monkeypatch):
    tracker.start_harness_turn("model", 500)
    tracker.update("model", 400)
    tracker.update_harness_stream("model", {}, 100)
    monkeypatch.setattr(module.time, "monotonic", lambda: 12.0)
    row = plain(tracker._format_harness_metrics("model"))
    assert "~1,800 tk" in row and "~50 tk/s" in row
    assert "$0.015" in row and "3 turns" in row

    tracker.update_harness(
        "model",
        {"api_turns": 3, "total_tokens": 1900, "completion_tokens": 300, "cost": 0.02},
        6.0,
    )
    tracker.set_phase("model", "linting")
    row = plain(tracker._format_harness_metrics("model"))
    assert "1,900 tk" in row and "50 tk/s" in row and "~" not in row
    assert "$0.020" in row and "3 turns" in row
    tracker.start_harness_turn("model", 1000)
    tracker.update("model", 40)
    tracker.update_harness_stream("model", {}, 10)
    row = plain(tracker._format_harness_metrics("model"))
    assert "~2,910 tk" in row and "4 turns" in row
    assert tracker._wave_completed_chars + tracker._active["model"]["chars"] == 440


@pytest.mark.parametrize("status", ["success", "failed", "cancelled"])
@pytest.mark.parametrize("known", [True, False])
@pytest.mark.parametrize("columns", [80, 110])
def test_metrics_survive_terminal_outcomes_and_unknown_usage(
    tracker, monkeypatch, capsys, status, known, columns
):
    usage = tracker._harness["model"]["usage"].copy()
    if not known:
        usage.update(
            total_tokens=None, completion_tokens=None, cost=None, cost_requires_provider=True
        )
    tracker._results["model"] = {
        "status": status,
        "time_s": 100,
        "file": "a/long/project/path/" * 8,
        "error": "intentional\nfailure with multiple lines",
        "usage": usage,
        "harness": {"attempts": [{}], "timing": {"api_s": 4.0}},
    }
    monkeypatch.setattr(module, "_tw", lambda: columns)
    tracker._render_final()
    output = plain(capsys.readouterr().out)
    rows = [line for line in output.splitlines() if "model" in line]
    assert len(rows) == 1
    row = rows[0]
    assert "2 turns" in row and "1m 40s" in row
    assert all(len(line) <= columns for line in output.splitlines())
    if known:
        assert "1,200 tk" in row and "50 tk/s" in row and "$0.015" in row
    else:
        assert "tk unknown" in row and "cost unknown" in row and "tk/s —" in row


@pytest.mark.parametrize("phase", ["building", "compacting", "linting", "running", "repairing"])
@pytest.mark.parametrize("columns", [80, 110])
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
    assert phase in row and "5.0s" in row
    assert "1,200 tk" in row and "50 tk/s" in row and "2 turns" in row
    assert frame.count("$0.015") == 2  # Per-model cost and the batch total.
    assert all(len(line) <= columns for line in frame.splitlines())


@pytest.mark.parametrize("count,hidden", [(5, 0), (7, 3)])
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
