"""JSON persistence for models, config, and analytics history.

Three files live in the working directory (gitignored):

    .benchmark_models.json   — selected models mapping
    .benchmark_config.json   — settings (theme, reasoning_effort, naming, …)
    .benchmark_history.json  — append-only analytics history

Load functions fall back to defaults on missing or corrupted files so a
clean startup always works; save functions swallow IOError and report to
stdout so a read-only disk never aborts a run mid-flight.

Paths are computed from ``os.getcwd()`` at call time, which is what makes
tests isolate correctly via ``monkeypatch.chdir(tmp_path)``.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any

from wavebench.tui.styles import S, _tri

HISTORY_FILE: str = ".benchmark_history.json"
MODELS_FILE: str = ".benchmark_models.json"
CONFIG_FILE: str = ".benchmark_config.json"


def _history_path() -> str:
    return os.path.join(os.getcwd(), HISTORY_FILE)


def _models_path() -> str:
    return os.path.join(os.getcwd(), MODELS_FILE)


def _config_path() -> str:
    return os.path.join(os.getcwd(), CONFIG_FILE)


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
        with open(_models_path(), "w", encoding="utf-8") as fh:
            json.dump(models, fh, indent=2)
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
        with open(_config_path(), "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
    except OSError as exc:
        print(f"    {_tri} {S.DIM}could not save config: {exc}{S.RST}")


def load_history() -> dict[str, Any]:
    """Load the analytics history from disk."""
    path = _history_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict) and "runs" in data:
                    return data
        except (OSError, json.JSONDecodeError):
            pass
    return {"version": 1, "runs": []}


def save_history(history: dict[str, Any]) -> None:
    """Persist the analytics history to disk."""
    try:
        with open(_history_path(), "w", encoding="utf-8") as fh:
            json.dump(history, fh, indent=2)
    except OSError as exc:
        print(f"    {_tri} {S.DIM}could not save history: {exc}{S.RST}")


def record_run(
    history: dict[str, Any],
    prompt: str,
    output_dir: str | None,
    total_time: float,
    model_results: dict[str, Any],
    costs: dict[str, float | None] | None = None,
    reasoning_effort: str | None = None,
) -> None:
    """Append the results of a benchmark run to *history* and save.

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
    history["runs"].append(run)
    save_history(history)
