"""Unit coverage for ``--clear-history``.

The flag used to ``os.remove`` the file outright: a permission error meant
an unhandled traceback, and every recorded run vanished with no copy kept.
It now renames to ``.bak`` and reports failures tidily.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wavebench import __main__ as main_mod


def _run_clear_history(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["wavebench", "--clear-history"])
    main_mod.main()


def test_clear_history_keeps_a_bak_copy(
    tmp_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {"version": 1, "runs": [{"prompt": "precious"}]}
    (tmp_state_dir / ".benchmark_history.json").write_text(json.dumps(payload))

    _run_clear_history(monkeypatch)

    assert not (tmp_state_dir / ".benchmark_history.json").exists()
    backup = tmp_state_dir / ".benchmark_history.json.bak"
    assert json.loads(backup.read_text()) == payload
    assert "History cleared" in capsys.readouterr().out


def test_clear_history_reports_a_failed_rename_tidily(
    tmp_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_state_dir / ".benchmark_history.json").write_text('{"version": 1, "runs": []}')

    def denied(*_args: object, **_kwargs: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(main_mod.os, "replace", denied)

    _run_clear_history(monkeypatch)  # must not raise

    assert "Could not clear history" in capsys.readouterr().out
    assert (tmp_state_dir / ".benchmark_history.json").exists()


def test_clear_history_with_no_history_says_so(
    tmp_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_clear_history(monkeypatch)
    assert "No history to clear" in capsys.readouterr().out
