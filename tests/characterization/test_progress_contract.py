"""Characterization tests for ``wavebench/tui/components.py``.

SCAFFOLDING — written before the file is split into ``tui/progress/`` and
``tui/analytics/`` packages. These pin external behavior of:
  - ``compute_cost`` (pure function)
  - ``render_idle_wave`` (pure, returns list of strings)
  - ``ProgressTracker`` (stateful class — construction, state transitions)
  - ``display_analytics`` (prints leaderboard; smoke-tested via capsys)

Paths are updated post-split to point at the new canonical locations
(``wavebench.tui.progress``, ``wavebench.tui.analytics``). Retire once
the migration is stable and proper unit tests supersede.

Retire after: Deliverable #2 (components) ships and is known-good.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# compute_cost — pure function
# ---------------------------------------------------------------------------


def test_compute_cost_returns_none_without_pricing() -> None:
    from wavebench.tui.analytics import compute_cost

    assert compute_cost({"prompt_tokens": 100}, {}) is None


def test_compute_cost_returns_none_without_usage() -> None:
    from wavebench.tui.analytics import compute_cost

    assert compute_cost({}, {"prompt": "0.00001"}) is None


def test_compute_cost_sums_prompt_and_completion_tokens() -> None:
    from wavebench.tui.analytics import compute_cost

    # 1000 prompt tokens @ $0.001/token + 500 completion tokens @ $0.002/token
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    pricing = {"prompt": "0.001", "completion": "0.002"}
    assert compute_cost(usage, pricing) == pytest.approx(1000 * 0.001 + 500 * 0.002)


def test_compute_cost_returns_none_for_zero_cost() -> None:
    from wavebench.tui.analytics import compute_cost

    # Free model with zero pricing returns None, not 0.0.
    usage = {"prompt_tokens": 100, "completion_tokens": 50}
    pricing = {"prompt": "0", "completion": "0"}
    assert compute_cost(usage, pricing) is None


def test_compute_cost_handles_tts_input_characters() -> None:
    from wavebench.tui.analytics import compute_cost

    usage = {"input_characters": 120}
    pricing = {"prompt": "0.000001", "request": "0.01"}
    assert compute_cost(usage, pricing) == pytest.approx(120 * 0.000001 + 0.01)


def test_compute_cost_handles_invalid_pricing_strings() -> None:
    from wavebench.tui.analytics import compute_cost

    usage = {"prompt_tokens": 100, "completion_tokens": 50}
    pricing = {"prompt": "n/a", "completion": "also bad"}
    assert compute_cost(usage, pricing) is None


# ---------------------------------------------------------------------------
# render_idle_wave — pure function
# ---------------------------------------------------------------------------


def test_render_idle_wave_returns_list_of_expected_length() -> None:
    from wavebench.tui.progress import render_idle_wave

    rows = render_idle_wave(tick=0, width=40, height=5)
    assert isinstance(rows, list)
    assert len(rows) == 5
    for r in rows:
        assert isinstance(r, str)


def test_render_idle_wave_zero_height_returns_empty_list() -> None:
    from wavebench.tui.progress import render_idle_wave

    assert render_idle_wave(tick=0, width=40, height=0) == []


def test_render_idle_wave_negative_height_returns_empty_list() -> None:
    from wavebench.tui.progress import render_idle_wave

    assert render_idle_wave(tick=5, width=40, height=-1) == []


def test_render_idle_wave_intensity_deterministic_for_same_tick() -> None:
    from wavebench.tui.progress import render_idle_wave

    a = render_idle_wave(tick=10, width=20, height=3, intensity=0.5)
    b = render_idle_wave(tick=10, width=20, height=3, intensity=0.5)
    assert a == b  # same inputs → same output


# ---------------------------------------------------------------------------
# ProgressTracker — stateful class
# ---------------------------------------------------------------------------


def test_progress_tracker_constructs_with_minimal_args() -> None:
    from wavebench.tui.progress import ProgressTracker

    tracker = ProgressTracker(total=3, results={})
    assert tracker.is_running is False
    assert tracker.rendered_final is False


def test_progress_tracker_accepts_byte_progress_unit() -> None:
    from wavebench.tui.progress import ProgressTracker

    tracker = ProgressTracker(total=1, results={}, progress_unit="bytes")
    tracker.register("tts_model")
    tracker.update("tts_model", 4096)
    tracker.unregister("tts_model")


def test_progress_tracker_result_row_formats_audio_bytes() -> None:
    from wavebench.tui.progress import ProgressTracker

    tracker = ProgressTracker(total=1, results={})
    row = tracker._format_result_row(
        "tts_model",
        {
            "status": "success",
            "time_s": 1.0,
            "file": "tts_model.mp3",
            "usage": {"audio_bytes": 4096},
        },
        rank=1,
        inner_w=100,
    )

    assert "4.0 KiB" in row


def test_progress_tracker_register_update_unregister() -> None:
    from wavebench.tui.progress import ProgressTracker

    tracker = ProgressTracker(total=1, results={})
    tracker.register("model_a")
    # update() doesn't raise and doesn't require is_running to be true.
    tracker.update("model_a", 100)
    tracker.update("model_a", 250)
    tracker.unregister("model_a")


def test_progress_tracker_mark_parsing_then_finish() -> None:
    from wavebench.tui.progress import ProgressTracker

    tracker = ProgressTracker(total=1, results={})
    tracker.register("model_a")
    tracker.update("model_a", 100)
    tracker.mark_parsing("model_a")
    tracker.finish_parsing("model_a")


def test_progress_tracker_set_output_dir() -> None:
    from wavebench.tui.progress import ProgressTracker

    tracker = ProgressTracker(total=1, results={})
    tracker.set_output_dir("/tmp/out")
    # No exception, internal state updated (no public getter but the setter
    # must succeed without crash).


def test_progress_tracker_set_ticker_with_messages() -> None:
    from wavebench.tui.progress import ProgressTracker

    tracker = ProgressTracker(total=1, results={})
    tracker.set_ticker(["effort high → max", "other notice"])


def test_progress_tracker_set_ticker_empty_list_is_noop() -> None:
    from wavebench.tui.progress import ProgressTracker

    tracker = ProgressTracker(total=1, results={})
    tracker.set_ticker([])  # must not crash


def test_progress_tracker_is_async_start_and_stop() -> None:
    import inspect

    from wavebench.tui.progress import ProgressTracker

    assert inspect.iscoroutinefunction(ProgressTracker.start)
    assert inspect.iscoroutinefunction(ProgressTracker.stop)


# ---------------------------------------------------------------------------
# display_analytics — smoke
# ---------------------------------------------------------------------------


def test_display_analytics_empty_history_shows_placeholder(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wavebench.tui.analytics import display_analytics

    display_analytics({"version": 1, "runs": []})
    out = capsys.readouterr().out
    assert "No history yet" in out


def test_display_analytics_non_empty_history_prints_leaderboard(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wavebench.tui.analytics import display_analytics

    history = {
        "version": 1,
        "runs": [
            {
                "timestamp": "2026-04-01T00:00:00+00:00",
                "prompt": "hello world",
                "output_dir": "benchmarkResults/hello",
                "total_time_s": 10.5,
                "models": {
                    "claudeOpus4.6": {
                        "status": "success",
                        "time_s": 10.5,
                        "file": "hello.py",
                        "usage": {"total_tokens": 2000},
                        "cost": 0.0012,
                    },
                },
            }
        ],
    }
    display_analytics(history, compact=False)
    out = capsys.readouterr().out
    # The table includes the model name and the "Lifetime Analytics" header.
    assert "claudeOpus4.6" in out
    assert "Lifetime Analytics" in out
    assert "1 run" in out  # "(1 run)" singular


def test_display_analytics_respects_sort_by(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wavebench.tui.analytics import display_analytics

    # Two models, different run counts; sort_by="runs" puts B first.
    history = {
        "version": 1,
        "runs": [
            {
                "timestamp": "",
                "prompt": "",
                "output_dir": "",
                "total_time_s": 1,
                "models": {
                    "model_a": {"status": "success", "time_s": 1.0, "file": "", "usage": {}}
                },
            },
            {
                "timestamp": "",
                "prompt": "",
                "output_dir": "",
                "total_time_s": 1,
                "models": {
                    "model_b": {"status": "success", "time_s": 2.0, "file": "", "usage": {}}
                },
            },
            {
                "timestamp": "",
                "prompt": "",
                "output_dir": "",
                "total_time_s": 1,
                "models": {
                    "model_b": {"status": "success", "time_s": 2.0, "file": "", "usage": {}}
                },
            },
        ],
    }
    display_analytics(history, compact=True, sort_by="runs")
    out = capsys.readouterr().out
    idx_a = out.find("model_a")
    idx_b = out.find("model_b")
    assert idx_a >= 0 and idx_b >= 0
    # model_b has 2 runs, model_a has 1 — model_b should appear first.
    assert idx_b < idx_a
