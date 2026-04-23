"""Shared pytest fixtures for WaveBench tests.

These fixtures are available to every test under ``tests/`` automatically.
The goal is to keep individual test files free of boilerplate for
temp state isolation, stdout suppression, and streaming-response mocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``storage.py`` persistence files to an isolated tmp dir.

    ``storage.py`` computes paths via ``os.getcwd()`` at call time, so we
    switch the working directory for the duration of the test. Any
    ``.benchmark_*.json`` files written during the test land in ``tmp_path``
    and are cleaned up automatically when pytest removes the tmp_path.

    Use pytest's built-in ``capsys`` fixture (instead of a local shim) when
    you need to assert on printed output — it hooks the capture pipeline
    at the correct layer.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Clear WaveBench-relevant env vars so tests don't leak host state.

    Currently only scrubs ``OPENROUTER_API_KEY``, but the fixture is a
    single place to add more as they appear.
    """
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return monkeypatch
