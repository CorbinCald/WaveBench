from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from symphony.models import (
    AgentConfig,
    BlockerRef,
    GitConfig,
    HooksConfig,
    Issue,
    IssueComment,
    PiConfig,
    PullRequestResult,
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
        self.attachments: list[tuple[str, str, str, str | None]] = []

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

    async def create_attachment(
        self, issue_id: str, title: str, url: str, subtitle: str | None = None
    ) -> bool:
        self.attachments.append((issue_id, title, url, subtitle))
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


class RespondingRunner(BlockingRunner):
    def __init__(self, response_text: str) -> None:
        super().__init__()
        self.response_text = response_text

    async def run_attempt(self, issue: Issue, attempt: int | None, on_event=None) -> RunResult:
        self.started.append(issue.identifier)
        if on_event is not None:
            on_event("turn_started", {"event": "turn_started"})
            on_event(
                "message_update",
                {
                    "event": "message_update",
                    "message": self.response_text,
                    "payload": {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "text_delta",
                            "delta": self.response_text,
                        },
                    },
                },
            )
            on_event("agent_end", {"event": "agent_end"})
        return RunResult(True, "succeeded")


class ProviderStatusRunner(BlockingRunner):
    async def run_attempt(self, issue: Issue, attempt: int | None, on_event=None) -> RunResult:
        self.started.append(issue.identifier)
        if on_event is not None:
            message = {
                "role": "assistant",
                "provider": "openai",
                "model": "gpt-5.5",
                "stopReason": "stop",
                "content": [],
            }
            on_event("turn_started", {"event": "turn_started"})
            on_event(
                "message_update",
                {
                    "event": "message_update",
                    "payload": {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "done",
                            "reason": "stop",
                            "message": message,
                        },
                    },
                },
            )
            on_event("agent_end", {"event": "agent_end", "payload": {"type": "agent_end", "messages": [message]}})
        return RunResult(True, "succeeded")


class FakePrWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.hooks = HooksConfig()
        self.git = None
        self.created_for: list[str] = []

    def path_for(self, identifier: str) -> Path:
        return self.root / identifier

    async def remove(self, identifier: str) -> None:
        return None

    async def create_pull_request_for_issue(self, issue: Issue) -> PullRequestResult:
        self.created_for.append(issue.identifier)
        return PullRequestResult(
            branch=f"symphony/{issue.identifier.lower()}",
            base_branch="main",
            pr_url=f"https://github.com/example/repo/pull/{issue.identifier}",
            pushed=True,
            created=True,
            ahead=1,
            behind=0,
        )


class FakeReviewWorkspace(FakePrWorkspace):
    def __init__(self, root: Path, has_reviewable_changes: bool) -> None:
        super().__init__(root)
        self.has_reviewable_changes = has_reviewable_changes

    async def has_reviewable_changes_for_issue(self, issue: Issue) -> bool:
        return self.has_reviewable_changes


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


@pytest.mark.asyncio
async def test_auto_transition_does_not_move_to_review_without_workspace_changes(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, auto_transition=True, post_status_comments=True)
    issue = Issue("issue-1", "WB-1", "Implement thing", "Todo", priority=1)
    tracker = FakeTracker([issue])
    response_words = [f"word{index}" for index in range(1, 102)]
    runner = RespondingRunner(" ".join(response_words))
    workspace = FakeReviewWorkspace(config.workspace_root, has_reviewable_changes=False)
    orchestrator = Orchestrator(config, make_workflow(tmp_path), tracker, runner, workspace)

    await orchestrator.tick()
    for _ in range(5):
        await asyncio.sleep(0)

    assert runner.started == ["WB-1"]
    assert tracker.transitions == [("issue-1", "In Progress")]
    assert issue.state == "In Progress"
    assert orchestrator.state.running == {}
    assert orchestrator.state.retry_attempts == {}
    assert orchestrator.state.review_blocked == {"issue-1"}
    assert "picked up WB-1" in tracker.comments[0][1]
    assert "not moved to Human Review" in tracker.comments[1][1]
    assert "Run summary:" in tracker.comments[1][1]
    assert "Tool executions: 0" in tracker.comments[1][1]
    assert "First response excerpt:" in tracker.comments[1][1]
    assert "word100 ..." in tracker.comments[1][1]
    assert "word101" not in tracker.comments[1][1]
    assert "No tools ran" in tracker.comments[1][1]
    assert "Recent steps" not in tracker.comments[1][1]

    await orchestrator.tick()

    assert runner.started == ["WB-1"]

    issue.state = "Todo"
    await orchestrator.tick()
    for _ in range(5):
        await asyncio.sleep(0)

    assert runner.started == ["WB-1", "WB-1"]


@pytest.mark.asyncio
async def test_no_change_summary_includes_provider_status_when_response_text_missing(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, auto_transition=True, post_status_comments=True)
    issue = Issue("issue-1", "WB-1", "Implement thing", "Todo", priority=1)
    tracker = FakeTracker([issue])
    runner = ProviderStatusRunner()
    workspace = FakeReviewWorkspace(config.workspace_root, has_reviewable_changes=False)
    orchestrator = Orchestrator(config, make_workflow(tmp_path), tracker, runner, workspace)

    await orchestrator.tick()
    for _ in range(5):
        await asyncio.sleep(0)

    assert "First response excerpt:" not in tracker.comments[1][1]
    assert "Provider status: provider=openai, model=gpt-5.5, stopReason=stop" in tracker.comments[1][1]


def test_in_progress_issue_with_no_change_comment_is_not_dispatched_after_restart(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    issue = Issue(
        "issue-1",
        "WB-1",
        "Implement thing",
        "In Progress",
        comments=[
            IssueComment(
                "comment-1",
                "Symphony finished a run for WB-1, but the workspace has no dirty files "
                "or commits ahead of the base branch, so it was not moved to Human Review.",
            )
        ],
    )
    orchestrator = Orchestrator(
        config,
        make_workflow(tmp_path),
        FakeTracker([issue]),
        BlockingRunner(),
        WorkspaceManager(config.workspace_root, config.hooks),
    )

    assert orchestrator.should_dispatch(issue) is False

    issue.state = "Todo"

    assert orchestrator.should_dispatch(issue) is True


@pytest.mark.asyncio
async def test_merging_state_creates_pull_request_without_dispatching_worker(tmp_path: Path) -> None:
    config = make_config(tmp_path, max_concurrent=2, post_status_comments=True)
    config.git = GitConfig(
        enabled=True,
        repo="https://example.invalid/repo.git",
        push_on_merging=True,
        pr_on_merging=True,
    )
    issue = Issue("issue-1", "WB-1", "Implement thing", "Merging", priority=1)
    tracker = FakeTracker([issue])
    runner = BlockingRunner()
    workspace = FakePrWorkspace(config.workspace_root)
    orchestrator = Orchestrator(config, make_workflow(tmp_path), tracker, runner, workspace)

    await orchestrator.tick()

    assert runner.started == []
    assert workspace.created_for == ["WB-1"]
    assert "prepared WB-1 for merging" in tracker.comments[0][1]
    assert tracker.attachments == [
        (
            "issue-1",
            "Symphony pull request",
            "https://github.com/example/repo/pull/WB-1",
            "symphony/wb-1",
        )
    ]
