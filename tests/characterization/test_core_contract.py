"""Characterization tests for ``wavebench/core.py``.

SCAFFOLDING — written before decomposing ``core.py`` into a ``core/``
package. These pin the external behavior of pure helpers and module-level
constants so the mechanical split can be verified. Retire or convert to
proper unit tests once the split is stable.

Retire after: Deliverable #2 (core) ships and is known-good.
"""

from __future__ import annotations

import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Public import contract
# ---------------------------------------------------------------------------


def test_main_async_is_importable() -> None:
    """``from wavebench.core import main_async`` must keep working."""
    from wavebench.core import main_async

    assert callable(main_async)


def test_get_unique_filename_is_importable() -> None:
    from wavebench.core import get_unique_filename

    assert callable(get_unique_filename)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_constants_preserved() -> None:
    from wavebench import core

    assert core.OUTPUT_DIR == "benchmarkResults"
    assert core.MAX_CONCURRENCY == 12
    assert core.REQUEST_TIMEOUT == 1800
    # SYSTEM_PROMPT_* constants moved into ``wavebench.modes.code`` and
    # ``wavebench.modes.text`` in Deliverable #3 — they are no longer
    # module-level in ``core``. The equivalent assertions now target the
    # modes via :func:`Mode.frame_prompt`.
    from wavebench.modes import CODE_MODE, TEXT_MODE
    from wavebench.modes.code import CodeMode

    assert "expert programmer" in CODE_MODE.frame_prompt("")
    assert "Do not include any external modules" in CODE_MODE.frame_prompt("")
    assert "third-party packages" in CodeMode(allow_deps=True).frame_prompt("")
    assert "Markdown" in TEXT_MODE.frame_prompt("")


# ---------------------------------------------------------------------------
# get_unique_filename — pure function
# ---------------------------------------------------------------------------


def test_get_unique_filename_fresh_directory(tmp_path) -> None:
    from wavebench.core import get_unique_filename

    name = get_unique_filename(str(tmp_path), "snake_game", ".py")
    assert name == "snake_game.py"


def test_get_unique_filename_normalizes_extension_dot(tmp_path) -> None:
    from wavebench.core import get_unique_filename

    # Both "py" and ".py" should yield the same result.
    assert get_unique_filename(str(tmp_path), "x", "py") == "x.py"
    assert get_unique_filename(str(tmp_path), "x", ".py") == "x.py"


def test_get_unique_filename_appends_version_suffix(tmp_path) -> None:
    from wavebench.core import get_unique_filename

    (tmp_path / "x.py").write_text("")
    assert get_unique_filename(str(tmp_path), "x", ".py") == "x_v2.py"

    (tmp_path / "x_v2.py").write_text("")
    assert get_unique_filename(str(tmp_path), "x", ".py") == "x_v3.py"


def test_get_unique_filename_strips_cursor_substring(tmp_path) -> None:
    # The function removes the substring "cursor" case-insensitively from
    # base_name — a historical quirk pinned to survive the refactor.
    from wavebench.core import get_unique_filename

    name = get_unique_filename(str(tmp_path), "cursorBrand", ".py")
    assert name == "Brand.py"


# ---------------------------------------------------------------------------
# _shell_cmd — pure function
# ---------------------------------------------------------------------------


def test_shell_cmd_quotes_interp_and_path() -> None:
    from wavebench.core import _shell_cmd

    cmd = _shell_cmd("/usr/bin/python3", "/tmp/my file.py")
    # shlex.quote wraps paths with spaces in single quotes.
    assert "'/tmp/my file.py'" in cmd
    assert "/usr/bin/python3" in cmd
    # Always includes the "press Enter" prompt to keep terminal open.
    assert "read -rp" in cmd


# ---------------------------------------------------------------------------
# _resolve_interpreter — near-pure (reads sys.executable, shutil.which)
# ---------------------------------------------------------------------------


def test_resolve_interpreter_python_returns_sys_executable() -> None:
    from wavebench.core import _resolve_interpreter

    assert _resolve_interpreter("foo.py", ".py") == sys.executable


def test_resolve_interpreter_unknown_extension_returns_none() -> None:
    from wavebench.core import _resolve_interpreter

    assert _resolve_interpreter("foo.xyz", ".xyz") is None


def test_resolve_interpreter_js_returns_node_or_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from wavebench.core import _resolve_interpreter

    # When "node" is on PATH, we get a path; otherwise None.
    fake_node = "/usr/local/bin/node"
    monkeypatch.setattr("shutil.which", lambda name: fake_node if name == "node" else None)
    assert _resolve_interpreter("foo.js", ".js") == fake_node


# ---------------------------------------------------------------------------
# Module constants for auto-open subsystem
# ---------------------------------------------------------------------------


def test_interpreter_map_shape() -> None:
    from wavebench.core import _INTERPRETER_MAP

    # .py maps to None (meaning: use sys.executable).
    assert _INTERPRETER_MAP[".py"] is None
    # .js and .sh map to named binaries.
    assert _INTERPRETER_MAP[".js"] == "node"
    assert _INTERPRETER_MAP[".sh"] == "bash"


def test_terminal_specs_shape() -> None:
    from wavebench.core import _TERMINAL_SPECS

    # Each entry is (binary, exec_flag, tab_flag_or_None).
    assert len(_TERMINAL_SPECS) >= 1
    for entry in _TERMINAL_SPECS:
        assert len(entry) == 3
        binary, exec_flag, tab_flag = entry
        assert isinstance(binary, str) and binary
        # exec_flag may be None (for kitty) or a string.
        assert exec_flag is None or isinstance(exec_flag, str)
        # tab_flag may be None (no tabs) or a string.
        assert tab_flag is None or isinstance(tab_flag, str)


def test_dep_detect_model_is_set() -> None:
    from wavebench.core import _DEP_DETECT_MODEL

    assert isinstance(_DEP_DETECT_MODEL, str)
    assert "/" in _DEP_DETECT_MODEL  # OpenRouter-style id


# ---------------------------------------------------------------------------
# _venv_python_path — pure function
# ---------------------------------------------------------------------------


def test_venv_python_path_linux_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch sys.platform globally — _venv_python_path reads it from its
    # own module's sys reference, which resolves to the shared sys module.
    monkeypatch.setattr("sys.platform", "linux")
    from wavebench.core import _venv_python_path

    p = _venv_python_path("/tmp/out")
    assert p == os.path.join("/tmp/out", ".venv", "bin", "python")


def test_venv_python_path_windows_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    from wavebench.core import _venv_python_path

    p = _venv_python_path("C:\\out")
    assert p.endswith(os.path.join("Scripts", "python.exe"))


# ---------------------------------------------------------------------------
# Auto-install package fallbacks
# ---------------------------------------------------------------------------


def test_package_fallbacks_pygame_prefers_ce() -> None:
    from wavebench.core import _PACKAGE_FALLBACKS

    assert _PACKAGE_FALLBACKS["pygame"][0] == "pygame-ce"
    assert "pygame" in _PACKAGE_FALLBACKS["pygame"]


# ---------------------------------------------------------------------------
# Process-level async entry points are importable (signature smoke test)
# ---------------------------------------------------------------------------


def test_run_model_is_async_callable() -> None:
    # ``process_model`` and ``process_model_text`` were collapsed into a
    # single ``run_model(mode, ...)`` in Deliverable #3.
    import inspect

    from wavebench.core import run_model

    assert inspect.iscoroutinefunction(run_model)


def test_main_async_is_async_callable() -> None:
    import inspect

    from wavebench.core import main_async

    assert inspect.iscoroutinefunction(main_async)
