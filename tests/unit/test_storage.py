"""Unit tests for ``wavebench.storage``.

``storage.py`` reads/writes three JSON files in the current working directory:
``.benchmark_models.json``, ``.benchmark_config.json``, and
``.benchmark_history.json``. Tests use the ``tmp_state_dir`` fixture (from
``conftest.py``) to redirect ``os.getcwd()`` to a pytest tmp_path so no host
state is touched.
"""

from __future__ import annotations

import json
import threading
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
        "auto_open": "incremental",
        "auto_install": "off",
        "directory_naming": "llm",
        "tts_voice": "alloy",
        "tts_format": "mp3",
        "tts_speed": 1.0,
        "image_settings": "provider defaults",
        "image_aspect_ratio": "1:1",
        "image_size": "1K",
        "image_model_ids": [],
    }


def test_save_config_then_load_config_roundtrip(tmp_state_dir: Path) -> None:
    storage.save_config({"theme": "plum", "reasoning_effort": "low"})
    loaded = storage.load_config()
    # Partial save is merged onto defaults so any missing key still has its default.
    assert loaded["theme"] == "plum"
    assert loaded["reasoning_effort"] == "low"
    assert loaded["analytics_sort"] == "runs"  # default preserved
    assert loaded["auto_open"] == "incremental"
    assert loaded["auto_install"] == "off"
    assert loaded["directory_naming"] == "llm"
    assert loaded["tts_voice"] == "alloy"
    assert loaded["tts_format"] == "mp3"
    assert loaded["tts_speed"] == 1.0
    assert loaded["image_settings"] == "provider defaults"
    assert loaded["image_aspect_ratio"] == "1:1"
    assert loaded["image_size"] == "1K"
    assert loaded["image_model_ids"] == []


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


def test_load_history_without_runs_key_quarantines_and_defaults(
    tmp_state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Dict without the "runs" key — invalid, so it is moved aside rather than
    # left in place where the next save would overwrite it.
    (tmp_state_dir / ".benchmark_history.json").write_text('{"version": 1}')
    assert storage.load_history() == {"version": 1, "runs": []}
    assert list(tmp_state_dir.glob(".benchmark_history.json.corrupt.*"))
    assert "unreadable" in capsys.readouterr().out


def test_load_history_corrupted_json_quarantines_the_file(
    tmp_state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A Ctrl-C mid-save used to leave truncated JSON that the next run would
    # silently replace with a fresh single-run file. The corpse is now moved
    # aside with a warning so nothing is destroyed.
    truncated = '{"version": 1, "runs": [{"pro'
    (tmp_state_dir / ".benchmark_history.json").write_text(truncated)

    assert storage.load_history() == {"version": 1, "runs": []}

    assert not (tmp_state_dir / ".benchmark_history.json").exists()
    corpses = list(tmp_state_dir.glob(".benchmark_history.json.corrupt.*"))
    assert len(corpses) == 1
    assert corpses[0].read_text() == truncated
    assert "unreadable" in capsys.readouterr().out

    # A later run starts a fresh history without touching the quarantined copy.
    storage.record_run(prompt="fresh", output_dir="", total_time=1.0, model_results={})
    assert corpses[0].read_text() == truncated
    assert storage.load_history()["runs"][0]["prompt"] == "fresh"


# ---------------------------------------------------------------------------
# record_run
# ---------------------------------------------------------------------------


def test_record_run_appends_and_persists(tmp_state_dir: Path) -> None:
    results = {
        "claudeOpus4.6": {
            "status": "ok",
            "time_s": 12.345,
            "file": "snake_game.py",
            "usage": {"prompt_tokens": 10, "completion_tokens": 40},
        },
    }
    history = storage.record_run(
        prompt="make a snake game",
        output_dir="benchmarkResults/snake_game",
        total_time=13.7,
        model_results=results,
        costs={"claudeOpus4.6": 0.00012},
        reasoning_effort="high",
    )

    # The updated history is returned for immediate display.
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
    results = {"m": {"status": "fail", "time_s": 1.0, "file": None, "usage": {}}}
    history = storage.record_run(
        prompt="x",
        output_dir="",
        total_time=1.0,
        model_results=results,
        costs={"m": None},
    )
    assert "cost" not in history["runs"][0]["models"]["m"]


def test_record_run_omits_reasoning_effort_when_unset(tmp_state_dir: Path) -> None:
    history = storage.record_run(
        prompt="x",
        output_dir=None,
        total_time=1.0,
        model_results={},
    )
    # Older records didn't have reasoning_effort — the key should be absent.
    assert "reasoning_effort" not in history["runs"][0]


def test_record_run_handles_output_dir_none(tmp_state_dir: Path) -> None:
    history = storage.record_run(
        prompt="x",
        output_dir=None,
        total_time=0.5,
        model_results={},
    )
    # None collapses to empty string per current contract.
    assert history["runs"][0]["output_dir"] == ""


def test_record_run_appends_to_latest_on_disk_history(tmp_state_dir: Path) -> None:
    # A second WaveBench in the same directory recorded a run while this one
    # was mid-benchmark. record_run re-reads the file at write time, so the
    # stale snapshot loaded at run start can never clobber the newer run.
    storage.save_history({"version": 1, "runs": [{"prompt": "other terminal"}]})
    history = storage.record_run(prompt="mine", output_dir="", total_time=1.0, model_results={})
    assert [r["prompt"] for r in history["runs"]] == ["other terminal", "mine"]
    on_disk = json.loads((tmp_state_dir / ".benchmark_history.json").read_text())
    assert on_disk == history


def test_record_run_waits_for_the_history_lock(tmp_state_dir: Path) -> None:
    fcntl = pytest.importorskip("fcntl")
    done = threading.Event()

    def worker() -> None:
        storage.record_run(prompt="locked", output_dir="", total_time=1.0, model_results={})
        done.set()

    with open(tmp_state_dir / ".benchmark_history.json.lock", "w") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        blocked = not done.wait(0.2)
    # Closing the descriptor releases the flock.
    assert blocked, "record_run should wait for the lock, not race past it"
    assert done.wait(5.0), "record_run should complete once the lock is free"
    thread.join(timeout=5.0)
    assert storage.load_history()["runs"][0]["prompt"] == "locked"


def test_record_run_still_records_without_fcntl(
    tmp_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows has no fcntl; locking degrades to none but recording must work.
    monkeypatch.setattr(storage, "fcntl", None)
    history = storage.record_run(prompt="x", output_dir="", total_time=1.0, model_results={})
    assert history["runs"][0]["prompt"] == "x"
    on_disk = json.loads((tmp_state_dir / ".benchmark_history.json").read_text())
    assert on_disk == history


# ---------------------------------------------------------------------------
# Atomic writes — an interrupt mid-save must never truncate the previous file.
# ---------------------------------------------------------------------------


def test_save_history_interrupted_mid_write_keeps_previous_file(
    tmp_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.save_history({"version": 1, "runs": [{"prompt": "precious"}]})

    def interrupted_dump(obj, fh, **kwargs) -> None:
        fh.write('{"version": 1, "ru')  # a few bytes land, then Ctrl-C
        raise KeyboardInterrupt

    monkeypatch.setattr(storage.json, "dump", interrupted_dump)
    with pytest.raises(KeyboardInterrupt):
        storage.save_history({"version": 1, "runs": []})

    on_disk = json.loads((tmp_state_dir / ".benchmark_history.json").read_text())
    assert on_disk["runs"][0]["prompt"] == "precious"
    assert not (tmp_state_dir / ".benchmark_history.json.tmp").exists()


def test_save_config_interrupted_mid_write_keeps_previous_file(
    tmp_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A truncated config used to silently revert every setting to defaults.
    storage.save_config({"theme": "plum"})

    def interrupted_dump(obj, fh, **kwargs) -> None:
        fh.write('{"the')
        raise KeyboardInterrupt

    monkeypatch.setattr(storage.json, "dump", interrupted_dump)
    with pytest.raises(KeyboardInterrupt):
        storage.save_config({"theme": "default"})

    assert storage.load_config()["theme"] == "plum"
    assert not (tmp_state_dir / ".benchmark_config.json.tmp").exists()


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
