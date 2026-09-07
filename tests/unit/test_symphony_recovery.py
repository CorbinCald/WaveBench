"""Successful work stops, retries are bounded, and executable config stays fixed."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from symphony.errors import ConfigError
from symphony.models import Issue, RunResult
from symphony.orchestrator import Orchestrator
from symphony.workspace import WorkspaceManager, _redact_url_credentials
from tests.unit.test_symphony_orchestrator import (
    BlockingRunner,
    FakeTracker,
    make_config,
    make_workflow,
)


async def settle(orchestrator):
    for entry in list(orchestrator.state.running.values()):
        await entry.task
    await asyncio.sleep(0)
    if orchestrator._pending:
        await asyncio.gather(*orchestrator._pending)


async def test_manual_state_management_does_not_repeat_successful_work(tmp_path):
    config = make_config(tmp_path)
    issue = Issue("1", "WB-1", "Build a thing", "Todo")
    runner = BlockingRunner()
    tracker = FakeTracker([issue])
    orchestrator = Orchestrator(
        config,
        make_workflow(tmp_path),
        tracker,
        runner,
        WorkspaceManager(config.workspace_root, config.hooks),
    )
    await orchestrator.tick()
    await settle(orchestrator)
    await orchestrator.tick()
    await settle(orchestrator)
    assert runner.started == ["WB-1"]
    assert orchestrator.state.retry_attempts == {}
    assert tracker.transitions == []


@pytest.mark.parametrize("missing_review", [False, True])
async def test_failures_and_missing_review_state_stop_at_attempt_limit(tmp_path, missing_review):
    config = make_config(tmp_path, auto_transition=missing_review, post_status_comments=True)
    issue = Issue("1", "WB-1", "Build a thing", "Todo")

    class Runner(BlockingRunner):
        async def run_attempt(self, issue, attempt, on_event=None):
            self.started.append(issue.identifier)
            return RunResult(missing_review, "finished" if missing_review else "failed")

    class Tracker(FakeTracker):
        async def update_issue_state(self, issue_id, state_name):
            if state_name == "Human Review":
                return False
            return await super().update_issue_state(issue_id, state_name)

    runner, tracker = Runner(), Tracker([issue])
    orchestrator = Orchestrator(
        config,
        make_workflow(tmp_path),
        tracker,
        runner,
        WorkspaceManager(config.workspace_root, config.hooks),
    )
    await orchestrator.tick()
    await settle(orchestrator)
    for retry_index in (1, 2):
        retry = orchestrator.state.retry_attempts["1"]
        assert retry.attempt == retry_index
        retry.timer_task.cancel()
        await orchestrator.handle_retry("1")
        await settle(orchestrator)
    await orchestrator.tick()
    assert len(runner.started) == config.agent.max_attempts == 3
    assert not orchestrator.state.retry_attempts
    assert not orchestrator.state.claimed
    assert "1" in orchestrator.state.completed
    assert "attempt limit" in tracker.comments[-1][1].lower()


@pytest.mark.parametrize("setting", ["hooks", "pi", "git", "workspace_root"])
def test_reload_rejects_changed_execution_settings(tmp_path, setting):
    config = make_config(tmp_path)
    runner = BlockingRunner()
    manager = WorkspaceManager(config.workspace_root, config.hooks)
    orchestrator = Orchestrator(config, make_workflow(tmp_path), FakeTracker([]), runner, manager)
    changes = {
        "hooks": replace(config.hooks, before_run="echo changed"),
        "pi": replace(config.pi, command="different-program"),
        "git": replace(config.git, repo="https://example.invalid/changed.git"),
        "workspace_root": tmp_path / "changed",
    }
    updated = replace(config, **{setting: changes[setting]})
    with pytest.raises(ConfigError, match="restart"):
        orchestrator.update_config(updated, make_workflow(tmp_path))
    assert orchestrator.config is config
    assert manager.root == config.workspace_root.resolve()


def test_git_error_redacts_credentials_in_command_and_stderr():
    secret_url = "https://x-access-token:dummy-private-token@example.invalid/repo.git"
    message = _redact_url_credentials(f"git clone {secret_url}: unable to access '{secret_url}'")
    assert "dummy-private-token" not in message
    assert "x-access-token" not in message
    assert "https://[redacted]@example.invalid/repo.git" in message


@pytest.mark.parametrize(
    "url, authenticated",
    [
        ("https://uploads.linear.app/example.png", True),
        ("https://linearusercontent.com/example.png", True),
        ("https://untrusted.uploads.linear.app/example.png", False),
        ("http://uploads.linear.app/example.png", False),
        ("https://example.invalid/example.png", False),
    ],
)
def test_image_authentication_stays_on_exact_https_hosts(url, authenticated):
    from symphony.linear import _image_request_headers

    headers = _image_request_headers(url, "test-credential")
    assert ("Authorization" in headers) is authenticated


def test_project_slug_can_be_supplied_privately(tmp_path):
    from symphony.config import resolve_config
    from symphony.models import WorkflowDefinition

    workflow = WorkflowDefinition(
        path=tmp_path / "WORKFLOW.md",
        config={"tracker": {"kind": "linear", "project_slug": "$LINEAR_PROJECT_SLUG"}},
        prompt_template="Work on {{ issue.identifier }}",
    )
    config = resolve_config(
        workflow, env={"LINEAR_API_KEY": "test-key", "LINEAR_PROJECT_SLUG": "private-project"}
    )
    assert config.tracker.project_slug == "private-project"
