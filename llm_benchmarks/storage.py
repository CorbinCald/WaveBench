import os
import json
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from llm_benchmarks.tui.styles import _tri, S

HISTORY_FILE: str = ".benchmark_history.json"
MODELS_FILE: str = ".benchmark_models.json"
CONFIG_FILE: str = ".benchmark_config.json"

def _history_path() -> str:
    return os.path.join(os.getcwd(), HISTORY_FILE)

def _models_path() -> str:
    return os.path.join(os.getcwd(), MODELS_FILE)

def _config_path() -> str:
    return os.path.join(os.getcwd(), CONFIG_FILE)

def load_models() -> Optional[Dict[str, str]]:
    """Load the persistent model selection from disk."""
    path = _models_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return data
        except (json.JSONDecodeError, IOError):
            pass
    return None

def save_models(models: Dict[str, str]) -> None:
    """Persist the model selection to disk."""
    try:
        with open(_models_path(), "w", encoding="utf-8") as fh:
            json.dump(models, fh, indent=2)
    except IOError as exc:
        print(f"    {_tri} {S.DIM}could not save models: {exc}{S.RST}")

def load_config() -> Dict[str, Any]:
    """Load the persistent configuration from disk."""
    path = _config_path()
    defaults = {"auto_use_venv": True}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return {**defaults, **data}
        except (json.JSONDecodeError, IOError):
            pass
    return defaults

def save_config(config: Dict[str, Any]) -> None:
    """Persist the configuration to disk."""
    try:
        with open(_config_path(), "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
    except IOError as exc:
        print(f"    {_tri} {S.DIM}could not save config: {exc}{S.RST}")

def load_history() -> Dict[str, Any]:
    """Load the analytics history from disk."""
    path = _history_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict) and "runs" in data:
                    return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"version": 1, "runs": []}

def save_history(history: Dict[str, Any]) -> None:
    """Persist the analytics history to disk."""
    try:
        with open(_history_path(), "w", encoding="utf-8") as fh:
            json.dump(history, fh, indent=2)
    except IOError as exc:
        print(f"    {_tri} {S.DIM}could not save history: {exc}{S.RST}")

def record_run(history: Dict[str, Any], prompt: str, output_dir: Optional[str],
               total_time: float, model_results: Dict[str, Any]) -> None:
    """Append the results of a benchmark run to *history* and save."""
    run = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prompt": prompt,
        "output_dir": output_dir or "",
        "total_time_s": round(total_time, 2),
        "models": {
            name: {
                "status": info["status"],
                "time_s": round(info["time_s"], 2),
                "file": info.get("file"),
                "usage": info.get("usage", {})
            }
            for name, info in model_results.items()
        },
    }
    history["runs"].append(run)
    save_history(history)
