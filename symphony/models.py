"""Core domain models for Symphony."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class BlockerRef:
    id: str | None = None
    identifier: str | None = None
    state: str | None = None


@dataclass(slots=True)
class IssueComment:
    id: str
    body: str
    author: str | None = None
    url: str | None = None
    created_at: datetime | None = None

    def to_template_data(self) -> dict[str, Any]:
        data = asdict(self)
        if isinstance(self.created_at, datetime):
            data["created_at"] = self.created_at.isoformat()
        return data


@dataclass(slots=True)
class Issue:
    id: str
    identifier: str
    title: str
    state: str
    description: str | None = None
    priority: int | None = None
    branch_name: str | None = None
    url: str | None = None
    labels: list[str] = field(default_factory=list)
    blocked_by: list[BlockerRef] = field(default_factory=list)
    comments: list[IssueComment] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_template_data(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("created_at", "updated_at"):
            value = data[key]
            if isinstance(value, datetime):
                data[key] = value.isoformat()
        data["comments"] = [comment.to_template_data() for comment in self.comments]
        return data


@dataclass(slots=True)
class WorkflowDefinition:
    path: Path
    config: dict[str, Any]
    prompt_template: str
    mtime_ns: int | None = None


@dataclass(slots=True)
class TrackerConfig:
    kind: str | None
    endpoint: str
    api_key: str | None
    project_slug: str | None
    active_states: list[str]
    terminal_states: list[str]
    working_state: str | None = None
    review_state: str | None = None
    merging_state: str | None = None
    auto_transition: bool = False
    post_status_comments: bool = False


@dataclass(slots=True)
class HooksConfig:
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = 60_000


@dataclass(slots=True)
class AgentConfig:
    max_concurrent_agents: int = 10
    max_turns: int = 20
    max_retry_backoff_ms: int = 300_000
    max_concurrent_agents_by_state: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class PiConfig:
    command: str = "pi --mode rpc --no-session"
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 5_000
    stall_timeout_ms: int = 300_000


@dataclass(slots=True)
class ServiceConfig:
    workflow_path: Path
    workflow_mtime_ns: int | None
    tracker: TrackerConfig
    polling_interval_ms: int
    workspace_root: Path
    hooks: HooksConfig
    agent: AgentConfig
    pi: PiConfig


@dataclass(slots=True)
class Workspace:
    path: Path
    workspace_key: str
    created_now: bool


@dataclass(slots=True)
class RunResult:
    success: bool
    status: str
    error: str | None = None


@dataclass(slots=True)
class RetryEntry:
    issue_id: str
    identifier: str
    attempt: int
    due_at_ms: float
    error: str | None = None
    timer_task: asyncio.Task[Any] | None = None


@dataclass(slots=True)
class RunningEntry:
    issue: Issue
    task: asyncio.Task[Any]
    identifier: str
    started_at: datetime
    retry_attempt: int | None = None
    session_id: str | None = None
    agent_pid: int | None = None
    last_agent_event: str | None = None
    last_agent_timestamp: datetime | None = None
    last_agent_message: str | None = None
    agent_input_tokens: int = 0
    agent_output_tokens: int = 0
    agent_total_tokens: int = 0
    last_reported_input_tokens: int = 0
    last_reported_output_tokens: int = 0
    last_reported_total_tokens: int = 0
    turn_count: int = 0


@dataclass(slots=True)
class OrchestratorState:
    poll_interval_ms: int
    max_concurrent_agents: int
    running: dict[str, RunningEntry] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)
    retry_attempts: dict[str, RetryEntry] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    agent_totals: dict[str, float] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "seconds_running": 0.0,
        }
    )
    agent_rate_limits: dict[str, Any] | None = None
