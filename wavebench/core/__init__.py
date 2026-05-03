"""Benchmark orchestration package.

Re-exports the public surface previously available from ``wavebench/core.py``
so ``from wavebench.core import main_async`` keeps working.

Internal organization:
  - ``orchestrator``  — ``main_async`` + module-level constants
  - ``runner``        — ``run_model`` (mode-parameterized) + ``get_unique_filename``
  - ``auto_open``     — terminal/viewer/tab launching
  - ``auto_install``  — dep detection + per-output-dir venv management

Response modes (``CodeMode``, ``TextMode``, ``TTSMode``) live in
``wavebench.modes``; see ``docs/CONTRIBUTING.md`` for the "Adding a new mode"
walkthrough.
"""

from .auto_install import (
    _DEP_DETECT_MODEL,
    _PACKAGE_FALLBACKS,
    _detect_dependencies,
    _ensure_venv,
    _install_packages,
    _venv_lock,
    _venv_python_path,
)
from .auto_open import (
    _INTERPRETER_MAP,
    _TERMINAL_SPECS,
    _find_terminal,
    _open_file,
    _open_file_in_tab,
    _open_files_as_tabs,
    _open_with_viewer,
    _reset_incremental_tabs,
    _resolve_interpreter,
    _run_in_terminal_single,
    _shell_cmd,
)
from .orchestrator import (
    MAX_CONCURRENCY,
    OUTPUT_DIR,
    REQUEST_TIMEOUT,
    main_async,
)
from .runner import (
    get_unique_filename,
    run_model,
)

__all__ = [
    "MAX_CONCURRENCY",
    "OUTPUT_DIR",
    "REQUEST_TIMEOUT",
    "_DEP_DETECT_MODEL",
    "_INTERPRETER_MAP",
    "_PACKAGE_FALLBACKS",
    "_TERMINAL_SPECS",
    "_detect_dependencies",
    "_ensure_venv",
    "_find_terminal",
    "_install_packages",
    "_open_file",
    "_open_file_in_tab",
    "_open_files_as_tabs",
    "_open_with_viewer",
    "_reset_incremental_tabs",
    "_resolve_interpreter",
    "_run_in_terminal_single",
    "_shell_cmd",
    "_venv_lock",
    "_venv_python_path",
    "get_unique_filename",
    "main_async",
    "run_model",
]
