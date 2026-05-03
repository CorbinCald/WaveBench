"""Polling, dispatch, reconciliation, and retry coordination for Symphony."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from symphony.config import validate_dispatch_config
from symphony.models import (
    Issue,
    OrchestratorState,
    RetryEntry,
    RunningEntry,
    RunResult,
    ServiceConfig,
    WorkflowDefinition,
)

_LOG = logging.getLogger(__name__)


class Orchestrator:
    """Single-authority scheduler for Symphony issue runs."""

    def __init__(
        self,
        config: ServiceConfig,
        workflow: WorkflowDefinition,
        tracker: Any,
        agent_runner: Any,
        workspace_manager: Any,
    ):
        self.config = config
        self.workflow = workflow
        self.tracker = tracker
        self.agent_runner = agent_runner
        self.workspace_manager = workspace_manager
        self.state = OrchestratorState(
            poll_interval_ms=config.polling_interval_ms,
            max_concurrent_agents=config.agent.max_concurrent_agents,
        )
        self._stop = asyncio.Event()

    def update_config(self, config: ServiceConfig, workflow: WorkflowDefinition) -> None:
        self.config = config
        self.workflow = workflow
        self.state.poll_interval_ms = config.polling_interval_ms
        self.state.max_concurrent_agents = config.agent.max_concurrent_agents
        if hasattr(self.agent_runner, "config"):
            self.agent_runner.config = config
        if hasattr(self.agent_runner, "workflow"):
            self.agent_runner.workflow = workflow
        if hasattr(self.workspace_manager, "root"):
            self.workspace_manager.root = config.workspace_root.resolve()
        if hasattr(self.workspace_manager, "hooks"):
            self.workspace_manager.hooks = config.hooks

    async def run(self) -> None:
        await self.startup_terminal_workspace_cleanup()
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.state.poll_interval_ms / 1000
                )
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    async def startup_terminal_workspace_cleanup(self) -> None:
        try:
            issues = await self.tracker.fetch_issues_by_states(self.config.tracker.terminal_states)
        except Exception as exc:
            _LOG.warning("startup_cleanup_failed error=%s", exc)
            return
        for issue in issues:
            try:
                await self.workspace_manager.remove(issue.identifier)
                _LOG.info(
                    "workspace_cleanup_completed issue_id=%s issue_identifier=%s",
                    issue.id,
                    issue.identifier,
                )
            except Exception as exc:
                _LOG.warning(
                    "workspace_cleanup_failed issue_id=%s issue_identifier=%s error=%s",
                    issue.id,
                    issue.identifier,
                    exc,
                )

    async def tick(self) -> None:
        await self.reconcile_running_issues()
        try:
            validate_dispatch_config(self.config)
        except Exception as exc:
            _LOG.error("dispatch_validation_failed error=%s", exc)
            return
        try:
            issues = await self.tracker.fetch_candidate_issues()
        except Exception as exc:
            _LOG.warning("candidate_fetch_failed error=%s", exc)
            return
        for issue in sort_for_dispatch(issues):
            if self.available_slots() <= 0:
                break
            if self.should_dispatch(issue):
                self.dispatch_issue(issue, attempt=None)

    async def reconcile_running_issues(self) -> None:
        await self._reconcile_stalled_runs()
        if not self.state.running:
            return
        issue_ids = list(self.state.running)
        try:
            refreshed = await self.tracker.fetch_issue_states_by_ids(issue_ids)
        except Exception as exc:
            _LOG.debug("state_refresh_failed keep_workers_running=true error=%s", exc)
            return
        refreshed_by_id = {issue.id: issue for issue in refreshed}
        active = _state_set(self.config.tracker.active_states)
        terminal = _state_set(self.config.tracker.terminal_states)
        for issue_id, entry in list(self.state.running.items()):
            issue = refreshed_by_id.get(issue_id)
            if issue is None:
                continue
            normalized = issue.state.lower()
            if normalized in terminal:
                await self.terminate_running_issue(issue_id, cleanup_workspace=True)
            elif normalized in active:
                entry.issue = issue
            else:
                await self.terminate_running_issue(issue_id, cleanup_workspace=False)

    def should_dispatch(self, issue: Issue) -> bool:
        if not issue.id or not issue.identifier or not issue.title or not issue.state:
            return False
        normalized = issue.state.lower()
        if normalized not in _state_set(self.config.tracker.active_states):
            return False
        if normalized in _state_set(self.config.tracker.terminal_states):
            return False
        if issue.id in self.state.running or issue.id in self.state.claimed:
            return False
        if self.available_slots() <= 0:
            return False
        if not self._state_slot_available(normalized):
            return False
        if normalized == "todo":
            terminal = _state_set(self.config.tracker.terminal_states)
            for blocker in issue.blocked_by:
                if blocker.state is None or blocker.state.lower() not in terminal:
                    return False
        return True

    def available_slots(self) -> int:
        return max(self.config.agent.max_concurrent_agents - len(self.state.running), 0)

    def dispatch_issue(self, issue: Issue, attempt: int | None) -> None:
        if issue.id in self.state.running:
            return
        task = asyncio.create_task(self._run_worker(issue, attempt))
        self.state.running[issue.id] = RunningEntry(
            issue=issue,
            task=task,
            identifier=issue.identifier,
            retry_attempt=attempt,
            started_at=datetime.now(UTC),
        )
        self.state.claimed.add(issue.id)
        retry = self.state.retry_attempts.pop(issue.id, None)
        if retry and retry.timer_task:
            retry.timer_task.cancel()
        task.add_done_callback(
            lambda done, issue_id=issue.id: asyncio.create_task(self._worker_done(issue_id, done))
        )
        _LOG.info(
            "dispatch_started issue_id=%s issue_identifier=%s attempt=%s",
            issue.id,
            issue.identifier,
            attempt,
        )

    async def terminate_running_issue(self, issue_id: str, cleanup_workspace: bool) -> None:
        entry = self.state.running.pop(issue_id, None)
        if entry is None:
            return
        entry.task.cancel()
        self._add_runtime(entry)
        self.state.claimed.discard(issue_id)
        if cleanup_workspace:
            try:
                await self.workspace_manager.remove(entry.identifier)
            except Exception as exc:
                _LOG.warning(
                    "workspace_cleanup_failed issue_id=%s issue_identifier=%s error=%s",
                    issue_id,
                    entry.identifier,
                    exc,
                )
        _LOG.info(
            "run_terminated issue_id=%s issue_identifier=%s cleanup_workspace=%s",
            issue_id,
            entry.identifier,
            cleanup_workspace,
        )

    def schedule_retry(
        self, issue_id: str, identifier: str, attempt: int, error: str | None, continuation: bool = False
    ) -> None:
        previous = self.state.retry_attempts.pop(issue_id, None)
        if previous and previous.timer_task:
            previous.timer_task.cancel()
        delay_ms = 1000 if continuation else min(
            10_000 * (2 ** max(attempt - 1, 0)), self.config.agent.max_retry_backoff_ms
        )
        due_at_ms = time.monotonic() * 1000 + delay_ms
        timer_task = asyncio.create_task(self._retry_after(issue_id, delay_ms / 1000))
        self.state.retry_attempts[issue_id] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_ms=due_at_ms,
            error=error,
            timer_task=timer_task,
        )
        self.state.claimed.add(issue_id)
        _LOG.info(
            "retry_scheduled issue_id=%s issue_identifier=%s attempt=%s delay_ms=%s error=%s",
            issue_id,
            identifier,
            attempt,
            delay_ms,
            error,
        )

    async def _run_worker(self, issue: Issue, attempt: int | None) -> RunResult:
        issue = await self._mark_issue_started(issue, attempt)
        return await self.agent_runner.run_attempt(issue, attempt, self._on_agent_event(issue.id))

    async def _worker_done(self, issue_id: str, task: asyncio.Task[Any]) -> None:
        entry = self.state.running.pop(issue_id, None)
        if entry is None:
            return
        self._add_runtime(entry)
        try:
            result = task.result()
        except asyncio.CancelledError:
            self.state.claimed.discard(issue_id)
            return
        except Exception as exc:
            result = RunResult(False, "failed", str(exc))
        if result.success:
            self.state.completed.add(issue_id)
            if await self._mark_issue_ready_for_review(entry, result):
                self.state.claimed.discard(issue_id)
            else:
                self.schedule_retry(issue_id, entry.identifier, 1, None, continuation=True)
        else:
            await self._post_status_comment(
                entry.issue,
                (
                    f"Symphony run failed for {entry.identifier}: "
                    f"{result.error or result.status}. Retrying with backoff."
                ),
            )
            next_attempt = (entry.retry_attempt or 0) + 1
            self.schedule_retry(issue_id, entry.identifier, next_attempt, result.error or result.status)

    async def _mark_issue_started(self, issue: Issue, attempt: int | None) -> Issue:
        if not self.config.tracker.auto_transition:
            return issue
        updated = issue
        working_state = self.config.tracker.working_state
        moved_to_working = False
        if working_state and issue.state.lower() != working_state.lower():
            moved_to_working = await self._update_issue_state(issue, working_state, "started")
        if moved_to_working and working_state:
            updated = replace(issue, state=working_state)
            entry = self.state.running.get(issue.id)
            if entry is not None:
                entry.issue = updated
        attempt_label = f" retry attempt {attempt}" if attempt is not None else ""
        await self._post_status_comment(
            updated,
            (
                f"Symphony picked up {updated.identifier}{attempt_label}. "
                f"Workspace: `{self._workspace_path_for_comment(updated.identifier)}`"
            ),
        )
        return updated

    async def _mark_issue_ready_for_review(self, entry: RunningEntry, result: RunResult) -> bool:
        if not self.config.tracker.auto_transition:
            return False
        review_state = self.config.tracker.review_state
        if not review_state:
            return False
        moved = await self._update_issue_state(entry.issue, review_state, "ready_for_review")
        if moved:
            await self._post_status_comment(
                replace(entry.issue, state=review_state),
                (
                    f"Symphony completed {entry.identifier} and moved it to {review_state}.\n\n"
                    f"Workspace: `{self._workspace_path_for_comment(entry.identifier)}`\n"
                    "Review the workspace diff and validation output before merging."
                ),
            )
        return moved

    async def _update_issue_state(self, issue: Issue, state_name: str, action: str) -> bool:
        if issue.state.lower() == state_name.lower():
            return True
        updater = getattr(self.tracker, "update_issue_state", None)
        if updater is None:
            _LOG.warning(
                "linear_state_update_unavailable issue_id=%s issue_identifier=%s target_state=%s action=%s",
                issue.id,
                issue.identifier,
                state_name,
                action,
            )
            return False
        try:
            updated = await updater(issue.id, state_name)
        except Exception as exc:
            _LOG.warning(
                "linear_state_update_failed issue_id=%s issue_identifier=%s target_state=%s action=%s error=%s",
                issue.id,
                issue.identifier,
                state_name,
                action,
                exc,
            )
            return False
        _LOG.info(
            "linear_state_update_completed issue_id=%s issue_identifier=%s target_state=%s action=%s",
            issue.id,
            issue.identifier,
            state_name,
            action,
        )
        return bool(updated)

    def _workspace_path_for_comment(self, identifier: str) -> Path:
        try:
            return self.workspace_manager.path_for(identifier)
        except Exception:
            return self.config.workspace_root / identifier

    async def _post_status_comment(self, issue: Issue, body: str) -> None:
        if not self.config.tracker.post_status_comments:
            return
        creator = getattr(self.tracker, "create_comment", None)
        if creator is None:
            _LOG.debug(
                "linear_comment_unavailable issue_id=%s issue_identifier=%s",
                issue.id,
                issue.identifier,
            )
            return
        try:
            await creator(issue.id, body)
        except Exception as exc:
            _LOG.warning(
                "linear_comment_failed issue_id=%s issue_identifier=%s error=%s",
                issue.id,
                issue.identifier,
                exc,
            )

    async def _retry_after(self, issue_id: str, delay_seconds: float) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            await self.handle_retry(issue_id)
        except asyncio.CancelledError:
            return

    async def handle_retry(self, issue_id: str) -> None:
        retry = self.state.retry_attempts.pop(issue_id, None)
        if retry is None:
            return
        try:
            candidates = await self.tracker.fetch_candidate_issues()
        except Exception as exc:
            self.schedule_retry(issue_id, retry.identifier, retry.attempt + 1, f"retry poll failed: {exc}")
            return
        issue = next((candidate for candidate in candidates if candidate.id == issue_id), None)
        if issue is None:
            self.state.claimed.discard(issue_id)
            return
        if self.available_slots() <= 0 or not self.should_dispatch_for_retry(issue):
            self.schedule_retry(
                issue_id,
                issue.identifier,
                retry.attempt + 1,
                "no available orchestrator slots",
            )
            return
        self.dispatch_issue(issue, attempt=retry.attempt)

    def should_dispatch_for_retry(self, issue: Issue) -> bool:
        was_claimed = issue.id in self.state.claimed
        if was_claimed:
            self.state.claimed.discard(issue.id)
        try:
            return self.should_dispatch(issue)
        finally:
            if was_claimed:
                self.state.claimed.add(issue.id)

    async def _reconcile_stalled_runs(self) -> None:
        stall_timeout_ms = self.config.pi.stall_timeout_ms
        if stall_timeout_ms <= 0:
            return
        now = datetime.now(UTC)
        for issue_id, entry in list(self.state.running.items()):
            anchor = entry.last_agent_timestamp or entry.started_at
            elapsed_ms = (now - anchor).total_seconds() * 1000
            if elapsed_ms > stall_timeout_ms:
                entry.task.cancel()
                self.state.running.pop(issue_id, None)
                self._add_runtime(entry)
                next_attempt = (entry.retry_attempt or 0) + 1
                self.schedule_retry(
                    issue_id,
                    entry.identifier,
                    next_attempt,
                    f"stalled for {int(elapsed_ms)}ms",
                )

    def _on_agent_event(self, issue_id: str) -> Callable[[str, dict[str, Any]], None]:
        def handle(name: str, payload: dict[str, Any]) -> None:
            entry = self.state.running.get(issue_id)
            if entry is None:
                return
            entry.last_agent_event = name
            entry.last_agent_timestamp = datetime.now(UTC)
            message = payload.get("message") or payload.get("event")
            if message is not None:
                entry.last_agent_message = str(message)[:500]
            if payload.get("session_id"):
                entry.session_id = str(payload["session_id"])
            if payload.get("agent_pid"):
                entry.agent_pid = int(payload["agent_pid"])
            if name == "turn_started":
                entry.turn_count += 1
            usage = payload.get("usage")
            if isinstance(usage, dict):
                self._record_usage_delta(entry, usage)
            if payload.get("rate_limits"):
                self.state.agent_rate_limits = payload["rate_limits"]

        return handle

    def _record_usage_delta(self, entry: RunningEntry, usage: dict[str, Any]) -> None:
        fields = [
            ("input_tokens", "agent_input_tokens", "last_reported_input_tokens"),
            ("output_tokens", "agent_output_tokens", "last_reported_output_tokens"),
            ("total_tokens", "agent_total_tokens", "last_reported_total_tokens"),
        ]
        for payload_key, total_attr, last_attr in fields:
            value = usage.get(payload_key)
            if not isinstance(value, int):
                continue
            previous = getattr(entry, last_attr)
            delta = max(value - previous, 0)
            setattr(entry, total_attr, getattr(entry, total_attr) + delta)
            setattr(entry, last_attr, value)
            self.state.agent_totals[payload_key] += delta

    def _state_slot_available(self, normalized_state: str) -> bool:
        limit = self.config.agent.max_concurrent_agents_by_state.get(
            normalized_state, self.config.agent.max_concurrent_agents
        )
        running_for_state = sum(
            1 for entry in self.state.running.values() if entry.issue.state.lower() == normalized_state
        )
        return running_for_state < limit

    def _add_runtime(self, entry: RunningEntry) -> None:
        elapsed = (datetime.now(UTC) - entry.started_at).total_seconds()
        self.state.agent_totals["seconds_running"] += max(elapsed, 0)

    def snapshot(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        running = []
        for issue_id, entry in self.state.running.items():
            running.append(
                {
                    "issue_id": issue_id,
                    "issue_identifier": entry.identifier,
                    "state": entry.issue.state,
                    "session_id": entry.session_id,
                    "turn_count": entry.turn_count,
                    "last_event": entry.last_agent_event,
                    "last_message": entry.last_agent_message,
                    "started_at": entry.started_at.isoformat(),
                    "last_event_at": entry.last_agent_timestamp.isoformat()
                    if entry.last_agent_timestamp
                    else None,
                    "tokens": {
                        "input_tokens": entry.agent_input_tokens,
                        "output_tokens": entry.agent_output_tokens,
                        "total_tokens": entry.agent_total_tokens,
                    },
                }
            )
        retrying = [
            {
                "issue_id": retry.issue_id,
                "issue_identifier": retry.identifier,
                "attempt": retry.attempt,
                "due_at_ms": retry.due_at_ms,
                "error": retry.error,
            }
            for retry in self.state.retry_attempts.values()
        ]
        totals = dict(self.state.agent_totals)
        totals["seconds_running"] += sum(
            max((now - entry.started_at).total_seconds(), 0) for entry in self.state.running.values()
        )
        return {
            "generated_at": now.isoformat(),
            "counts": {"running": len(running), "retrying": len(retrying)},
            "running": running,
            "retrying": retrying,
            "agent_totals": totals,
            "rate_limits": self.state.agent_rate_limits,
        }


def sort_for_dispatch(issues: list[Issue]) -> list[Issue]:
    def key(issue: Issue) -> tuple[int, datetime, str]:
        priority = issue.priority if issue.priority is not None else 999_999
        created_at = issue.created_at or datetime.max.replace(tzinfo=UTC)
        return priority, created_at, issue.identifier

    return sorted(issues, key=key)


def _state_set(states: list[str]) -> set[str]:
    return {state.lower() for state in states}
