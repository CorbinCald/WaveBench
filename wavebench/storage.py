"""JSON persistence for models, config, and analytics history.

Three files live in the working directory (gitignored):

    .benchmark_models.json   — selected models mapping
    .benchmark_config.json   — settings (theme, reasoning_effort, naming, …)
    .benchmark_history.json  — append-only analytics history

Load functions fall back to defaults on missing or corrupted files so a
clean startup always works; save functions swallow IOError and report to
stdout so a read-only disk never aborts a run mid-flight.

Saves write a sibling ``.tmp`` file, fsync it, and ``os.replace`` it over
the target, so an interrupt mid-write can never truncate what is already
on disk.  ``record_run`` is the only history mutator: it re-reads the file
under an exclusive ``flock`` before appending, so concurrent runs in the
same directory append to each other's history instead of clobbering it.
A history file that no longer parses is quarantined to
``.corrupt.<timestamp>`` with a warning rather than silently overwritten
by the next save.

Paths are computed from ``os.getcwd()`` at call time, which is what makes
tests isolate correctly via ``monkeypatch.chdir(tmp_path)``.
"""

import json
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from datetime import datetime, timezone
from typing import Any

from wavebench.tui.styles import S, _tri

try:  # fcntl is POSIX-only; without it locking degrades gracefully (saves stay atomic)
    import fcntl
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]

HISTORY_FILE: str = ".benchmark_history.json"
MODELS_FILE: str = ".benchmark_models.json"
CONFIG_FILE: str = ".benchmark_config.json"


def _history_path() -> str:
    return os.path.join(os.getcwd(), HISTORY_FILE)


def _models_path() -> str:
    return os.path.join(os.getcwd(), MODELS_FILE)


def _config_path() -> str:
    return os.path.join(os.getcwd(), CONFIG_FILE)


def _atomic_write_json(path: str, payload: Any) -> None:
    """Write *payload* to *path* so readers never observe a partial file.

    ``open(path, "w")`` truncates before the first byte lands, so an
    interrupt mid-``json.dump`` used to leave truncated JSON behind.
    Writing a sibling tmp file, fsyncing, and ``os.replace``-ing is atomic
    on POSIX: the old file stays intact until the new one is durably
    complete.
    """
    tmp = f"{path}.tmp"
    replaced = False
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        replaced = True
    finally:
        if not replaced:
            with suppress(OSError):
                os.remove(tmp)


def load_models() -> dict[str, str] | None:
    """Load the persistent model selection from disk."""
    path = _models_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return data
        except (OSError, json.JSONDecodeError):
            pass
    return None


def save_models(models: dict[str, str]) -> None:
    """Persist the model selection to disk."""
    try:
        _atomic_write_json(_models_path(), models)
    except OSError as exc:
        print(f"    {_tri} {S.DIM}could not save models: {exc}{S.RST}")


def load_config() -> dict[str, Any]:
    """Load the persistent configuration from disk."""
    path = _config_path()
    defaults = {
        "reasoning_effort": "high",
        "analytics_sort": "runs",
        "theme": "default",
        "auto_open": "off",
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
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return {**defaults, **data}
        except (OSError, json.JSONDecodeError):
            pass
    return defaults


def save_config(config: dict[str, Any]) -> None:
    """Persist the configuration to disk."""
    try:
        _atomic_write_json(_config_path(), config)
    except OSError as exc:
        print(f"    {_tri} {S.DIM}could not save config: {exc}{S.RST}")


def _quarantine_history(path: str, reason: str) -> None:
    """Move an unreadable history file aside so the next save cannot destroy it."""
    print(f"\n    {_tri} {S.HYEL}analytics history is unreadable ({reason}){S.RST}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = f"{path}.corrupt.{stamp}"
    try:
        os.replace(path, target)
    except OSError as exc:
        print(f"      {S.YEL}could not move it aside ({exc}) — it may get overwritten{S.RST}")
        return
    kept = os.path.basename(target)
    print(f"      {S.YEL}kept the file as {kept} — new runs start a fresh history{S.RST}")


def load_history() -> dict[str, Any]:
    """Load the analytics history from disk.

    A file that exists but cannot be parsed is quarantined rather than
    ignored: returning a fresh history while the broken file stays in
    place would let the next save silently overwrite every run it still
    holds.
    """
    path = _history_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "runs" in data:
                return data
            _quarantine_history(path, "unrecognized structure")
        except json.JSONDecodeError as exc:
            _quarantine_history(path, f"invalid JSON at line {exc.lineno}")
        except OSError:
            pass
    return {"version": 1, "runs": []}


def save_history(history: dict[str, Any]) -> None:
    """Persist the analytics history to disk."""
    try:
        _atomic_write_json(_history_path(), history)
    except OSError as exc:
        print(f"    {_tri} {S.DIM}could not save history: {exc}{S.RST}")


@contextmanager
def _history_lock() -> Iterator[None]:
    """Hold an exclusive advisory lock around a history read-modify-write.

    The lock lives on a sidecar ``.lock`` file rather than the history file
    itself because ``os.replace`` swaps the history inode — a lock taken on
    the old inode would not exclude the next writer.  ``flock`` releases on
    close (including process death), so a crashed run cannot wedge it.
    Best-effort: without ``fcntl`` or with an unwritable directory, the
    caller proceeds unlocked, which matches the pre-lock behavior.
    """
    with ExitStack() as stack:
        if fcntl is not None:
            with suppress(OSError):
                lock_fh = stack.enter_context(open(_history_path() + ".lock", "w"))
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        yield


def record_run(
    prompt: str,
    output_dir: str | None,
    total_time: float,
    model_results: dict[str, Any],
    costs: dict[str, float | None] | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Append one benchmark run to the on-disk history and return the result.

    The only mutator of the history file.  It re-reads the file fresh under
    an exclusive lock before appending, so a run that spent twenty minutes
    benchmarking cannot write its stale start-of-run snapshot back over
    whatever other processes recorded in the meantime.

    *reasoning_effort* is stamped on the record when provided so lifetime
    analytics can later stratify runs by the effort level in force — past
    runs (without this field) simply read as "unknown".
    """
    costs = costs or {}
    run = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prompt": prompt,
        "output_dir": output_dir or "",
        "total_time_s": round(total_time, 2),
        **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
        "models": {
            name: {
                "status": info["status"],
                "time_s": round(info["time_s"], 2),
                "file": info.get("file"),
                "usage": info.get("usage", {}),
                **({"cost": round(costs[name], 6)} if costs.get(name) is not None else {}),
                **({"retries": info["retries"]} if info.get("retries") else {}),
            }
            for name, info in model_results.items()
        },
    }
    with _history_lock():
        history = load_history()
        history["runs"].append(run)
        save_history(history)
    return history
