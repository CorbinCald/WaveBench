"""Unit tests for ``wavebench.storage``.

``storage.py`` reads/writes three JSON files in the current working directory:
``.benchmark_models.json``, ``.benchmark_config.json``, and
``.benchmark_history.json``. Tests use the ``tmp_state_dir`` fixture (from
``conftest.py``) to redirect ``os.getcwd()`` to a pytest tmp_path so no host
state is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wavebench import storage

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def test_load_models_missing_file_returns_none(tmp_state_dir: Path) -> None:
    assert storage.load_models() is None


def test_save_models_then_load_models_roundtrip(tmp_state_dir: Path) -> None:
    mapping = {
        "claudeOpus4.6": "anthropic/claude-opus-4.6",
        "gemini3_0Pro": "google/gemini-3-pro-preview",
    }
    storage.save_models(mapping)
    assert (tmp_state_dir / ".benchmark_models.json").exists()
    assert storage.load_models() == mapping


def test_load_models_corrupted_json_returns_none(tmp_state_dir: Path) -> None:
    (tmp_state_dir / ".benchmark_models.json").write_text("this is not json{{{")
    assert storage.load_models() is None


def test_load_models_non_dict_returns_none(tmp_state_dir: Path) -> None:
    # JSON is valid but not a dict — the function rejects it rather than crashing.
    (tmp_state_dir / ".benchmark_models.json").write_text("[1, 2, 3]")
    assert storage.load_models() is None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_load_config_missing_file_returns_defaults(tmp_state_dir: Path) -> None:
    cfg = storage.load_config()
    assert cfg == {
        "reasoning_effort": "high",
        "analytics_sort": "runs",
        "theme": "default",
        "auto_open": "off",
        "auto_install": "off",
        "directory_naming": "llm",
    }


def test_save_config_then_load_config_roundtrip(tmp_state_dir: Path) -> None:
    storage.save_config({"theme": "plum", "reasoning_effort": "low"})
    loaded = storage.load_config()
    # Partial save is merged onto defaults so any missing key still has its default.
    assert loaded["theme"] == "plum"
    assert loaded["reasoning_effort"] == "low"
    assert loaded["analytics_sort"] == "runs"  # default preserved
    assert loaded["auto_open"] == "off"
    assert loaded["auto_install"] == "off"
    assert loaded["directory_naming"] == "llm"


def test_load_config_corrupted_json_returns_defaults(tmp_state_dir: Path) -> None:
    (tmp_state_dir / ".benchmark_config.json").write_text("garbage{")
    cfg = storage.load_config()
    assert cfg["theme"] == "default"


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def test_load_history_missing_returns_empty(tmp_state_dir: Path) -> None:
    h = storage.load_history()
    assert h == {"version": 1, "runs": []}


def test_save_history_then_load_history_roundtrip(tmp_state_dir: Path) -> None:
    storage.save_history({"version": 1, "runs": [{"prompt": "hi"}]})
    assert storage.load_history() == {"version": 1, "runs": [{"prompt": "hi"}]}


def test_load_history_without_runs_key_returns_empty(tmp_state_dir: Path) -> None:
    # Dict without the "runs" key — treated as invalid and defaulted.
    (tmp_state_dir / ".benchmark_history.json").write_text('{"version": 1}')
    assert storage.load_history() == {"version": 1, "runs": []}


def test_load_history_corrupted_json_returns_empty(tmp_state_dir: Path) -> None:
    (tmp_state_dir / ".benchmark_history.json").write_text("not json")
    assert storage.load_history() == {"version": 1, "runs": []}


# ---------------------------------------------------------------------------
# record_run
# ---------------------------------------------------------------------------


def test_record_run_appends_and_persists(tmp_state_dir: Path) -> None:
    history: dict = {"version": 1, "runs": []}
    results = {
        "claudeOpus4.6": {
            "status": "ok",
            "time_s": 12.345,
            "file": "snake_game.py",
            "usage": {"prompt_tokens": 10, "completion_tokens": 40},
        },
    }
    storage.record_run(
        history,
        prompt="make a snake game",
        output_dir="benchmarkResults/snake_game",
        total_time=13.7,
        model_results=results,
        costs={"claudeOpus4.6": 0.00012},
        reasoning_effort="high",
    )

    # In-memory append is reflected.
    assert len(history["runs"]) == 1
    run = history["runs"][0]
    assert run["prompt"] == "make a snake game"
    assert run["total_time_s"] == 13.7
    assert run["reasoning_effort"] == "high"
    assert run["models"]["claudeOpus4.6"]["status"] == "ok"
    assert run["models"]["claudeOpus4.6"]["time_s"] == 12.35  # rounded to 2dp
    assert run["models"]["claudeOpus4.6"]["cost"] == 0.00012

    # Persisted to disk.
    on_disk = json.loads((tmp_state_dir / ".benchmark_history.json").read_text())
    assert on_disk == history


def test_record_run_omits_cost_when_none(tmp_state_dir: Path) -> None:
    history: dict = {"version": 1, "runs": []}
    results = {"m": {"status": "fail", "time_s": 1.0, "file": None, "usage": {}}}
    storage.record_run(
        history,
        prompt="x",
        output_dir="",
        total_time=1.0,
        model_results=results,
        costs={"m": None},
    )
    assert "cost" not in history["runs"][0]["models"]["m"]


def test_record_run_omits_reasoning_effort_when_unset(tmp_state_dir: Path) -> None:
    history: dict = {"version": 1, "runs": []}
    storage.record_run(
        history,
        prompt="x",
        output_dir=None,
        total_time=1.0,
        model_results={},
    )
    # Older records didn't have reasoning_effort — the key should be absent.
    assert "reasoning_effort" not in history["runs"][0]


def test_record_run_handles_output_dir_none(tmp_state_dir: Path) -> None:
    history: dict = {"version": 1, "runs": []}
    storage.record_run(
        history,
        prompt="x",
        output_dir=None,
        total_time=0.5,
        model_results={},
    )
    # None collapses to empty string per current contract.
    assert history["runs"][0]["output_dir"] == ""


# ---------------------------------------------------------------------------
# IOError handling — save functions must not raise on bad disk state.
# ---------------------------------------------------------------------------


def test_save_models_swallows_ioerror(
    tmp_state_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", boom)
    # Should not raise.
    storage.save_models({"m": "anthropic/claude"})
    # Error was reported to stdout (not stderr in current impl).
    captured = capsys.readouterr()
    assert "could not save models" in captured.out
