"""Typed Symphony config resolution and validation."""

from __future__ import annotations

import os
import re
import shlex
import tempfile
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

from symphony.errors import ConfigError
from symphony.models import (
    AgentConfig,
    GitConfig,
    HooksConfig,
    PiConfig,
    ServiceConfig,
    TrackerConfig,
    WorkflowDefinition,
)

_LINEAR_ENDPOINT = "https://api.linear.app/graphql"
_DEFAULT_ACTIVE_STATES = ["Todo", "In Progress"]
_DEFAULT_TERMINAL_STATES = ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"]
_DEFAULT_WORKING_STATE = "In Progress"
_DEFAULT_REVIEW_STATE = "Human Review"
_DEFAULT_MERGING_STATE = "Merging"
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_REF_RE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")


def load_dotenv(path: str | Path, env: MutableMapping[str, str] | None = None) -> list[str]:
    """Load simple ``KEY=value`` assignments from *path* into *env*.

    Existing non-empty environment values win, so an explicitly exported key
    is never replaced by the local ``.env`` file. The return value contains
    only the names that were loaded; values are intentionally not exposed.
    """
    target = os.environ if env is None else env
    dotenv_path = Path(path)
    if not dotenv_path.exists():
        return []

    loaded: list[str] = []
    try:
        lines = dotenv_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    for line in lines:
        parsed = _parse_dotenv_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if target.get(key):
            continue
        target[key] = value
        loaded.append(key)
    return loaded


def _env_with_workflow_dotenv(workflow_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    load_dotenv(workflow_dir / ".env", env)
    return env


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    try:
        parts = shlex.split(stripped, comments=True, posix=True)
    except ValueError:
        return None
    if not parts:
        return None
    if parts[0] == "export":
        parts = parts[1:]
    if not parts or "=" not in parts[0]:
        return None
    key, value = parts[0].split("=", 1)
    if not _ENV_NAME_RE.fullmatch(key):
        return None
    return key, value


def resolve_config(
    workflow: WorkflowDefinition, env: Mapping[str, str] | None = None, validate: bool = True
) -> ServiceConfig:
    """Apply defaults, env indirection, path normalization, and typed validation."""

    env = _env_with_workflow_dotenv(workflow.path.parent) if env is None else env
    raw = workflow.config
    tracker_raw = _map(raw.get("tracker"), "tracker")
    polling_raw = _map(raw.get("polling"), "polling")
    workspace_raw = _map(raw.get("workspace"), "workspace")
    hooks_raw = _map(raw.get("hooks"), "hooks")
    agent_raw = _map(raw.get("agent"), "agent")
    pi_raw = _map(raw.get("pi"), "pi")
    git_raw = _map(raw.get("git"), "git")

    tracker_kind = _optional_str(tracker_raw.get("kind"))
    endpoint = _optional_str(tracker_raw.get("endpoint")) or _LINEAR_ENDPOINT
    api_key = _resolve_env_ref(_optional_str(tracker_raw.get("api_key")), env)
    if not api_key and tracker_kind == "linear":
        api_key = env.get("LINEAR_API_KEY") or None
    project_slug = _optional_str(tracker_raw.get("project_slug"))
    tracker = TrackerConfig(
        kind=tracker_kind,
        endpoint=endpoint,
        api_key=api_key,
        project_slug=project_slug,
        active_states=_string_list(tracker_raw.get("active_states"), _DEFAULT_ACTIVE_STATES),
        terminal_states=_string_list(
            tracker_raw.get("terminal_states"), _DEFAULT_TERMINAL_STATES
        ),
        working_state=_optional_str(tracker_raw.get("working_state")) or _DEFAULT_WORKING_STATE,
        review_state=_optional_str(tracker_raw.get("review_state")) or _DEFAULT_REVIEW_STATE,
        merging_state=_optional_str(tracker_raw.get("merging_state")) or _DEFAULT_MERGING_STATE,
        auto_transition=_bool_value(
            tracker_raw.get("auto_transition"), False, "tracker.auto_transition"
        ),
        post_status_comments=_bool_value(
            tracker_raw.get("post_status_comments"), False, "tracker.post_status_comments"
        ),
    )

    workspace_root = _resolve_workspace_root(workspace_raw.get("root"), workflow.path.parent, env)
    hooks = HooksConfig(
        after_create=_optional_str(hooks_raw.get("after_create")),
        before_run=_optional_str(hooks_raw.get("before_run")),
        after_run=_optional_str(hooks_raw.get("after_run")),
        before_remove=_optional_str(hooks_raw.get("before_remove")),
        timeout_ms=_positive_int(hooks_raw.get("timeout_ms"), 60_000, "hooks.timeout_ms"),
    )
    agent = AgentConfig(
        max_concurrent_agents=_positive_int(
            agent_raw.get("max_concurrent_agents"), 10, "agent.max_concurrent_agents"
        ),
        max_turns=_positive_int(agent_raw.get("max_turns"), 20, "agent.max_turns"),
        max_retry_backoff_ms=_positive_int(
            agent_raw.get("max_retry_backoff_ms"), 300_000, "agent.max_retry_backoff_ms"
        ),
        max_concurrent_agents_by_state=_state_limits(
            agent_raw.get("max_concurrent_agents_by_state")
        ),
    )
    pi = PiConfig(
        command=_optional_str(pi_raw.get("command")) or "pi --mode rpc --no-session",
        turn_timeout_ms=_positive_int(
            pi_raw.get("turn_timeout_ms"), 3_600_000, "pi.turn_timeout_ms"
        ),
        read_timeout_ms=_positive_int(pi_raw.get("read_timeout_ms"), 5_000, "pi.read_timeout_ms"),
        stall_timeout_ms=_int_value(pi_raw.get("stall_timeout_ms"), 300_000, "pi.stall_timeout_ms"),
        ingest_linear_images=_bool_value(
            pi_raw.get("ingest_linear_images"), False, "pi.ingest_linear_images"
        ),
        max_linear_images=_positive_int(
            pi_raw.get("max_linear_images"), 6, "pi.max_linear_images"
        ),
        max_linear_image_bytes=_positive_int(
            pi_raw.get("max_linear_image_bytes"), 8_000_000, "pi.max_linear_image_bytes"
        ),
    )
    pr_on_merging = _bool_value(git_raw.get("pr_on_merging"), False, "git.pr_on_merging")
    git = GitConfig(
        enabled=_bool_value(git_raw.get("enabled"), False, "git.enabled"),
        repo=_resolve_env_ref(_optional_str(git_raw.get("repo")), env),
        remote=_optional_str(git_raw.get("remote")) or "origin",
        base_branch=_optional_str(git_raw.get("base_branch")) or "main",
        branch_prefix=_optional_str(git_raw.get("branch_prefix")) or "symphony",
        rebase_policy=_optional_str(git_raw.get("rebase_policy")) or "clean-only",
        push_on_merging=_bool_value(
            git_raw.get("push_on_merging"), pr_on_merging, "git.push_on_merging"
        ),
        pr_on_merging=pr_on_merging,
        timeout_ms=_positive_int(git_raw.get("timeout_ms"), 120_000, "git.timeout_ms"),
    )
    config = ServiceConfig(
        workflow_path=workflow.path,
        workflow_mtime_ns=workflow.mtime_ns,
        tracker=tracker,
        polling_interval_ms=_positive_int(
            polling_raw.get("interval_ms"), 30_000, "polling.interval_ms"
        ),
        workspace_root=workspace_root,
        hooks=hooks,
        agent=agent,
        pi=pi,
        git=git,
    )
    if validate:
        validate_dispatch_config(config)
    return config


def validate_dispatch_config(config: ServiceConfig) -> None:
    """Validate the config required to poll Linear and launch workers."""

    if config.tracker.kind != "linear":
        raise ConfigError(
            "unsupported_tracker_kind", "tracker.kind is required and must currently be 'linear'"
        )
    if not config.tracker.api_key:
        raise ConfigError(
            "missing_tracker_api_key",
            "tracker.api_key, exported LINEAR_API_KEY, or workflow-adjacent .env LINEAR_API_KEY is required",
        )
    if not config.tracker.project_slug:
        raise ConfigError(
            "missing_tracker_project_slug", "tracker.project_slug is required for Linear"
        )
    if not config.pi.command.strip():
        raise ConfigError("missing_pi_command", "pi.command must be non-empty")
    if config.git.enabled and not config.git.repo:
        raise ConfigError("missing_git_repo", "git.repo is required when git.enabled is true")
    if config.git.rebase_policy not in {"clean-only", "never"}:
        raise ConfigError(
            "unsupported_git_rebase_policy",
            "git.rebase_policy must be 'clean-only' or 'never'",
        )


def _map(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("workflow_parse_error", f"{name} must be a map/object")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _string_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if not isinstance(value, list):
        raise ConfigError("workflow_parse_error", "expected list of strings")
    return [str(item) for item in value]


def _resolve_env_ref(value: str | None, env: Mapping[str, str]) -> str | None:
    if value is None:
        return None
    match = _ENV_REF_RE.fullmatch(value.strip())
    if not match:
        return value
    resolved = env.get(match.group(1), "")
    return resolved or None


def _resolve_workspace_root(value: Any, workflow_dir: Path, env: Mapping[str, str]) -> Path:
    raw = _resolve_env_ref(_optional_str(value), env)
    if not raw:
        return Path(tempfile.gettempdir(), "symphony_workspaces").resolve()
    expanded = Path(os.path.expanduser(raw))
    if not expanded.is_absolute():
        expanded = workflow_dir / expanded
    return expanded.resolve()


def _positive_int(value: Any, default: int, name: str) -> int:
    parsed = _int_value(value, default, name)
    if parsed <= 0:
        raise ConfigError("workflow_parse_error", f"{name} must be a positive integer")
    return parsed


def _int_value(value: Any, default: int, name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError("workflow_parse_error", f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("workflow_parse_error", f"{name} must be an integer") from exc


def _bool_value(value: Any, default: bool, name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConfigError("workflow_parse_error", f"{name} must be a boolean")


def _state_limits(value: Any) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(
            "workflow_parse_error", "agent.max_concurrent_agents_by_state must be a map"
        )
    limits: dict[str, int] = {}
    for state, raw_limit in value.items():
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            continue
        if limit > 0:
            limits[str(state).lower()] = limit
    return limits
