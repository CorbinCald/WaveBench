"""Polling, dispatch, reconciliation, and retry coordination for Symphony."""

from __future__ import annotations

import asyncio
import logging
import re
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
    PullRequestResult,
    RetryEntry,
    RunningEntry,
    RunResult,
    ServiceConfig,
    WorkflowDefinition,
)

_LOG = logging.getLogger(__name__)
_NO_REVIEWABLE_CHANGES_MARKER = "workspace has no dirty files or commits ahead of the base branch"


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
        if hasattr(self.workspace_manager, "git"):
            self.workspace_manager.git = config.git

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
        await self.process_merging_issues()
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

    async def process_merging_issues(self) -> None:
        if not (self.config.git.enabled and self.config.git.pr_on_merging):
            return
        merging_state = self.config.tracker.merging_state
        if not merging_state:
            return
        try:
            issues = await self.tracker.fetch_issues_by_states([merging_state])
        except Exception as exc:
            _LOG.warning("merging_fetch_failed error=%s", exc)
            return
        now_ms = time.monotonic() * 1000
        for issue in sort_for_dispatch(issues):
            if issue.id in self.state.running or issue.id in self.state.claimed:
                continue
            if issue.id in self.state.merging_processed:
                continue
            retry_after = self.state.merging_retry_after_ms.get(issue.id, 0)
            if retry_after > now_ms:
                continue
            self.state.claimed.add(issue.id)
            try:
                result = await self.workspace_manager.create_pull_request_for_issue(issue)
            except Exception as exc:
                self.state.merging_retry_after_ms[issue.id] = (
                    now_ms + self.config.agent.max_retry_backoff_ms
                )
                _LOG.warning(
                    "merging_pr_failed issue_id=%s issue_identifier=%s error=%s",
                    issue.id,
                    issue.identifier,
                    exc,
                )
                await self._post_status_comment(
                    issue,
                    (
                        f"Symphony could not prepare a pull request for {issue.identifier}: {exc}. "
                        "It will retry later."
                    ),
                )
            else:
                self.state.merging_processed.add(issue.id)
                self.state.merging_retry_after_ms.pop(issue.id, None)
                await self._mark_pull_request_ready(issue, result)
            finally:
                self.state.claimed.discard(issue.id)

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
        if issue.id in self.state.review_blocked:
            if normalized == "todo":
                self.state.review_blocked.discard(issue.id)
            else:
                return False
        if normalized != "todo" and _latest_comment_is_no_reviewable_changes_notice(issue):
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
            if await self._mark_issue_ready_for_review(entry, result):
                self.state.completed.add(issue_id)
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
        try:
            has_reviewable_changes = await self._workspace_has_reviewable_changes(entry.issue)
        except Exception as exc:
            _LOG.warning(
                "review_gate_check_failed issue_id=%s issue_identifier=%s error=%s",
                entry.issue.id,
                entry.identifier,
                exc,
            )
            await self._post_status_comment(
                entry.issue,
                (
                    f"Symphony could not verify workspace changes for {entry.identifier}: {exc}. "
                    "It will retry before moving the issue to review."
                ),
            )
            return False
        if not has_reviewable_changes:
            self.state.review_blocked.add(entry.issue.id)
            _LOG.info(
                "review_gate_no_changes issue_id=%s issue_identifier=%s status=%s",
                entry.issue.id,
                entry.identifier,
                result.status,
            )
            trace = _agent_trace_summary(entry)
            await self._post_status_comment(
                entry.issue,
                (
                    f"Symphony finished a run for {entry.identifier}, but the workspace has no "
                    "dirty files or commits ahead of the base branch, so it was not moved to "
                    f"{review_state}.\n\n"
                    f"Workspace: `{self._workspace_path_for_comment(entry.identifier)}`\n\n"
                    f"Run summary:\n{trace}\n\n"
                    "Update the issue/workspace and move it back to Todo when it should be retried."
                ),
            )
            return True
        moved = await self._update_issue_state(entry.issue, review_state, "ready_for_review")
        if moved:
            self.state.review_blocked.discard(entry.issue.id)
            await self._post_status_comment(
                replace(entry.issue, state=review_state),
                (
                    f"Symphony completed {entry.identifier} and moved it to {review_state}.\n\n"
                    f"Workspace: `{self._workspace_path_for_comment(entry.identifier)}`\n"
                    "Review the workspace diff and validation output before moving it to Merging."
                ),
            )
        return moved

    async def _workspace_has_reviewable_changes(self, issue: Issue) -> bool:
        checker = getattr(self.workspace_manager, "has_reviewable_changes_for_issue", None)
        if checker is None:
            return True
        return bool(await checker(issue))

    async def _mark_pull_request_ready(self, issue: Issue, result: PullRequestResult) -> None:
        pr_line = f"\nPR: {result.pr_url}" if result.pr_url else ""
        await self._post_status_comment(
            issue,
            (
                f"Symphony prepared {issue.identifier} for merging.\n\n"
                f"Branch: `{result.branch}`\n"
                f"Base: `{result.base_branch}`\n"
                f"Ahead/behind: {result.ahead}/{result.behind}\n"
                f"Pushed: {'yes' if result.pushed else 'no'}"
                f"{pr_line}"
            ),
        )
        if result.pr_url:
            creator = getattr(self.tracker, "create_attachment", None)
            if creator is None:
                return
            try:
                await creator(issue.id, "Symphony pull request", result.pr_url, result.branch)
            except Exception as exc:
                _LOG.warning(
                    "linear_attachment_failed issue_id=%s issue_identifier=%s error=%s",
                    issue.id,
                    issue.identifier,
                    exc,
                )

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
            entry.last_agent_timestamp = datetime.now(UTC)
            message = payload.get("message") or payload.get("event")
            if name == "agent_step":
                _record_agent_step(entry, payload)
            else:
                entry.last_agent_event = name
                if message is not None:
                    entry.last_agent_message = str(message)[:500]
            if payload.get("session_id"):
                entry.session_id = str(payload["session_id"])
            if payload.get("agent_pid"):
                entry.agent_pid = int(payload["agent_pid"])
            if name == "turn_started":
                entry.turn_count += 1
            if name == "message_update":
                _record_first_turn_response_text(entry, payload)
                _record_provider_status(entry, payload)
            if name in {"message_end", "turn_end", "agent_end", "auto_retry_start", "auto_retry_end"}:
                _record_provider_status(entry, payload)
            if name == "tool_execution_start":
                entry.tool_execution_count += 1
                tool_name = _tool_name_from_event(payload)
                if tool_name and tool_name not in entry.tool_names:
                    entry.tool_names.append(tool_name)
            if _should_log_agent_event(name):
                _LOG.info(
                    "agent_event issue_id=%s issue_identifier=%s event=%s message=%r",
                    issue_id,
                    entry.identifier,
                    name,
                    entry.last_agent_message,
                )
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
                    "tool_execution_count": entry.tool_execution_count,
                    "tool_names": list(entry.tool_names),
                    "prompt_image_count": entry.prompt_image_count,
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


def _latest_comment_is_no_reviewable_changes_notice(issue: Issue) -> bool:
    if not issue.comments:
        return False
    body = issue.comments[0].body
    return _NO_REVIEWABLE_CHANGES_MARKER in body and "not moved to" in body


def _record_agent_step(entry: RunningEntry, payload: dict[str, Any]) -> None:
    image_count = payload.get("image_count")
    if isinstance(image_count, int):
        entry.prompt_image_count = max(entry.prompt_image_count, image_count)


def _agent_trace_summary(entry: RunningEntry) -> str:
    tool_line = f"- Tool executions: {entry.tool_execution_count}{_tool_names_suffix(entry)}"
    lines = [
        f"- Pi turns completed: {entry.turn_count}",
        f"- Images sent: {entry.prompt_image_count}",
        tool_line,
    ]
    response_excerpt = _first_words(entry.first_turn_response_text, 100)
    if response_excerpt:
        lines.append(f"- First response excerpt: {response_excerpt}")
    elif entry.provider_status:
        lines.append(f"- Provider status: {entry.provider_status}")
    else:
        lines.append("- Provider status: unavailable from Pi RPC events.")
    if entry.tool_execution_count == 0:
        lines.append("- No tools ran, so the agent did not inspect or modify files.")
    return "\n".join(lines)


def _tool_names_suffix(entry: RunningEntry) -> str:
    if not entry.tool_names:
        return ""
    names = ", ".join(entry.tool_names[:5])
    if len(entry.tool_names) > 5:
        names += ", ..."
    return f" ({names})"


def _record_first_turn_response_text(entry: RunningEntry, payload: dict[str, Any]) -> None:
    if entry.turn_count != 1:
        return
    delta = _message_update_text_delta(payload)
    if not delta:
        return
    entry.first_turn_response_text = (entry.first_turn_response_text + delta)[:8_000]


def _message_update_text_delta(payload: dict[str, Any]) -> str | None:
    raw_payload = payload.get("payload")
    if not isinstance(raw_payload, dict):
        return None
    update = raw_payload.get("assistantMessageEvent")
    if not isinstance(update, dict):
        return None
    if update.get("type") != "text_delta":
        return None
    value = update.get("delta") or update.get("text") or update.get("content")
    if value is None:
        return None
    return str(value)


def _first_words(text: str, limit: int) -> str:
    words = text.split()
    if not words or limit <= 0:
        return ""
    excerpt = " ".join(words[:limit])
    if len(words) > limit:
        excerpt += " ..."
    return excerpt


def _record_provider_status(entry: RunningEntry, payload: dict[str, Any]) -> None:
    status = _provider_status_from_event(payload)
    if not status:
        return
    if not entry.provider_status or _provider_status_is_error(status):
        entry.provider_status = status


def _provider_status_from_event(payload: dict[str, Any]) -> str | None:
    raw_payload = payload.get("payload")
    if not isinstance(raw_payload, dict):
        return None

    event_type = raw_payload.get("type")
    if event_type == "message_update":
        update = raw_payload.get("assistantMessageEvent")
        if not isinstance(update, dict):
            return None
        message = update.get("message") or update.get("error") or update.get("partial")
        if isinstance(message, dict):
            return _format_provider_status(message, fallback_reason=update.get("reason"))
        return None

    if event_type in {"message_end", "turn_end"}:
        message = raw_payload.get("message")
        if isinstance(message, dict):
            return _format_provider_status(message)
        return None

    if event_type == "agent_end":
        messages = raw_payload.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "assistant":
                    status = _format_provider_status(message)
                    if status:
                        return status
        return None

    if event_type == "auto_retry_start":
        return _format_provider_error_status(raw_payload.get("errorMessage"), prefix="retrying")
    if event_type == "auto_retry_end" and raw_payload.get("success") is False:
        return _format_provider_error_status(raw_payload.get("finalError"), prefix="retry_failed")
    return None


def _format_provider_status(message: dict[str, Any], fallback_reason: Any = None) -> str | None:
    if message.get("role") not in {None, "assistant"}:
        return None
    parts: list[str] = []
    provider = _optional_status_text(message.get("provider") or message.get("api"))
    model = _optional_status_text(message.get("responseModel") or message.get("model"))
    reason = _optional_status_text(message.get("stopReason") or fallback_reason)
    error = _optional_status_text(message.get("errorMessage"))
    status_code = _status_code_from_message(message, error)
    if provider:
        parts.append(f"provider={provider}")
    if model:
        parts.append(f"model={model}")
    if reason:
        parts.append(f"stopReason={reason}")
    if status_code:
        parts.append(f"status={status_code}")
    if error:
        parts.append(f"message={_truncate_status_text(error)}")
    return ", ".join(parts) if parts else None


def _format_provider_error_status(value: Any, prefix: str) -> str | None:
    error = _optional_status_text(value)
    if not error:
        return None
    status_code = _status_code_from_text(error)
    parts = [prefix]
    if status_code:
        parts.append(f"status={status_code}")
    parts.append(f"message={_truncate_status_text(error)}")
    return ", ".join(parts)


def _status_code_from_message(message: dict[str, Any], error: str | None) -> str | None:
    for key in ("statusCode", "status", "code"):
        value = message.get(key)
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()[:40]
    return _status_code_from_text(error or "")


def _status_code_from_text(text: str) -> str | None:
    match = re.search(r"(?<!\d)([45]\d{2})(?!\d)", text)
    return match.group(1) if match else None


def _optional_status_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\n", " ").strip()
    return text or None


def _truncate_status_text(text: str, limit: int = 220) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def _provider_status_is_error(status: str) -> bool:
    lowered = status.lower()
    return "stopreason=error" in lowered or "retry" in lowered or "status=4" in lowered or "status=5" in lowered


def _tool_name_from_event(payload: dict[str, Any]) -> str | None:
    raw_payload = payload.get("payload")
    if isinstance(raw_payload, dict):
        tool_name = raw_payload.get("toolName") or raw_payload.get("tool_name")
        if tool_name:
            return str(tool_name)[:80]
    return None


def _should_log_agent_event(name: str) -> bool:
    return name in {
        "agent_start",
        "agent_end",
        "turn_started",
        "tool_execution_start",
        "tool_execution_end",
        "extension_ui_request",
        "auto_retry_start",
        "auto_retry_end",
    }
