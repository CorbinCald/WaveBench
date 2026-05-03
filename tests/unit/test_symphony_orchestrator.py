from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from symphony.models import (
    AgentConfig,
    BlockerRef,
    HooksConfig,
    Issue,
    PiConfig,
    RunResult,
    ServiceConfig,
    TrackerConfig,
    WorkflowDefinition,
)
from symphony.orchestrator import Orchestrator, sort_for_dispatch
from symphony.workspace import WorkspaceManager


class FakeTracker:
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        self.transitions: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []

    async def fetch_candidate_issues(self) -> list[Issue]:
        return self.issues

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        return [issue for issue in self.issues if issue.state in state_names]

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        return [issue for issue in self.issues if issue.id in issue_ids]

    async def update_issue_state(self, issue_id: str, state_name: str) -> bool:
        self.transitions.append((issue_id, state_name))
        for issue in self.issues:
            if issue.id == issue_id:
                issue.state = state_name
        return True

    async def create_comment(self, issue_id: str, body: str) -> bool:
        self.comments.append((issue_id, body))
        return True


class BlockingRunner:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.release = None

    async def run_attempt(self, issue: Issue, attempt: int | None, on_event=None) -> RunResult:
        self.started.append(issue.identifier)
        if self.release is not None:
            await self.release.wait()
        return RunResult(True, "succeeded")


def make_config(
    tmp_path: Path,
    max_concurrent: int = 1,
    auto_transition: bool = False,
    post_status_comments: bool = False,
) -> ServiceConfig:
    return ServiceConfig(
        workflow_path=tmp_path / "WORKFLOW.md",
        workflow_mtime_ns=1,
        tracker=TrackerConfig(
            kind="linear",
            endpoint="https://api.linear.app/graphql",
            api_key="secret",
            project_slug="demo",
            active_states=["Todo", "In Progress"],
            terminal_states=["Done", "Closed", "Cancelled", "Canceled", "Duplicate"],
            working_state="In Progress",
            review_state="Human Review",
            merging_state="Merging",
            auto_transition=auto_transition,
            post_status_comments=post_status_comments,
        ),
        polling_interval_ms=1000,
        workspace_root=tmp_path / "workspaces",
        hooks=HooksConfig(),
        agent=AgentConfig(max_concurrent_agents=max_concurrent),
        pi=PiConfig(command="pi --mode rpc --no-session", stall_timeout_ms=0),
    )


def make_workflow(tmp_path: Path) -> WorkflowDefinition:
    return WorkflowDefinition(tmp_path / "WORKFLOW.md", {}, "Prompt", 1)


def test_sort_for_dispatch_priority_created_identifier() -> None:
    now = datetime.now(UTC)
    issues = [
        Issue("2", "WB-2", "B", "Todo", priority=None, created_at=now),
        Issue("3", "WB-3", "C", "Todo", priority=1, created_at=now),
        Issue("1", "WB-1", "A", "Todo", priority=1, created_at=now - timedelta(days=1)),
    ]

    assert [issue.identifier for issue in sort_for_dispatch(issues)] == ["WB-1", "WB-3", "WB-2"]


@pytest.mark.asyncio
async def test_tick_dispatches_highest_priority_with_available_slot(tmp_path: Path) -> None:
    config = make_config(tmp_path, max_concurrent=1)
    issues = [
        Issue("low", "WB-2", "Low", "Todo", priority=4),
        Issue("high", "WB-1", "High", "Todo", priority=1),
    ]
    runner = BlockingRunner()
    runner.release = asyncio.Event()
    orchestrator = Orchestrator(
        config,
        make_workflow(tmp_path),
        FakeTracker(issues),
        runner,
        WorkspaceManager(config.workspace_root, config.hooks),
    )

    await orchestrator.tick()
    await asyncio.sleep(0)

    assert runner.started == ["WB-1"]
    assert set(orchestrator.state.running) == {"high"}

    runner.release.set()
    await asyncio.sleep(0)
    for retry in orchestrator.state.retry_attempts.values():
        if retry.timer_task:
            retry.timer_task.cancel()


@pytest.mark.asyncio
async def test_todo_with_non_terminal_blocker_is_not_dispatched(tmp_path: Path) -> None:
    config = make_config(tmp_path, max_concurrent=2)
    blocked = Issue(
        "blocked",
        "WB-1",
        "Blocked",
        "Todo",
        priority=1,
        blocked_by=[BlockerRef(identifier="WB-0", state="In Progress")],
    )
    ready = Issue("ready", "WB-2", "Ready", "Todo", priority=2)
    runner = BlockingRunner()
    runner.release = asyncio.Event()
    orchestrator = Orchestrator(
        config,
        make_workflow(tmp_path),
        FakeTracker([blocked, ready]),
        runner,
        WorkspaceManager(config.workspace_root, config.hooks),
    )

    await orchestrator.tick()
    await asyncio.sleep(0)

    assert runner.started == ["WB-2"]

    runner.release.set()
    await asyncio.sleep(0)
    for retry in orchestrator.state.retry_attempts.values():
        if retry.timer_task:
            retry.timer_task.cancel()


@pytest.mark.asyncio
async def test_auto_transition_moves_issue_to_working_then_human_review(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, auto_transition=True, post_status_comments=True)
    issue = Issue("issue-1", "WB-1", "Implement thing", "Todo", priority=1)
    tracker = FakeTracker([issue])
    runner = BlockingRunner()
    orchestrator = Orchestrator(
        config,
        make_workflow(tmp_path),
        tracker,
        runner,
        WorkspaceManager(config.workspace_root, config.hooks),
    )

    await orchestrator.tick()
    for _ in range(5):
        await asyncio.sleep(0)

    assert runner.started == ["WB-1"]
    assert tracker.transitions == [("issue-1", "In Progress"), ("issue-1", "Human Review")]
    assert issue.state == "Human Review"
    assert orchestrator.state.running == {}
    assert orchestrator.state.retry_attempts == {}
    assert "picked up WB-1" in tracker.comments[0][1]
    assert "moved it to Human Review" in tracker.comments[1][1]
