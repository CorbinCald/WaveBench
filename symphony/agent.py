"""Pi RPC runner used by Symphony workers."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from symphony.errors import AgentError, TemplateError, WorkspaceError
from symphony.models import Issue, PiConfig, RunResult, ServiceConfig, WorkflowDefinition
from symphony.workflow import render_prompt
from symphony.workspace import WorkspaceManager

_LOG = logging.getLogger(__name__)
EventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]
_DIALOG_UI_METHODS = {"select", "confirm", "input", "editor"}


class PiRpcClient:
    """JSONL client for ``pi --mode rpc``.

    Pi RPC uses one JSON object per LF-delimited line. Commands are sent to stdin,
    responses and events are read from stdout. See Pi's ``docs/rpc.md``.
    """

    def __init__(
        self,
        config: PiConfig,
        workspace_root: Path,
        on_event: EventCallback | None = None,
    ):
        self.config = config
        self.workspace_root = workspace_root.resolve()
        self.on_event = on_event
        self.process: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None
        self._next_id = 1
        self._stderr_task: asyncio.Task[None] | None = None

    async def start(self, workspace: Path, issue: Issue) -> None:
        workspace = workspace.resolve()
        self._assert_workspace(workspace)
        try:
            self.process = await asyncio.create_subprocess_exec(
                "bash",
                "-lc",
                self.config.command,
                cwd=str(workspace),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=10 * 1024 * 1024,
            )
        except FileNotFoundError as exc:
            raise AgentError("pi_not_found", "bash or pi command was not found") from exc
        except OSError as exc:
            raise AgentError("startup_failed", f"failed to start pi RPC process: {exc}") from exc

        self._stderr_task = asyncio.create_task(self._drain_stderr())
        state = await self._request({"type": "get_state"}, "get_state", self.config.read_timeout_ms)
        data = state.get("data") if isinstance(state.get("data"), dict) else {}
        self.session_id = str(data.get("sessionId") or self.process.pid)
        await self._emit(
            "session_started",
            {
                "event": "session_started",
                "timestamp": _now_iso(),
                "session_id": self.session_id,
                "agent_pid": self.process.pid,
                "issue_id": issue.id,
                "issue_identifier": issue.identifier,
            },
        )

    async def run_turn(self, prompt: str, workspace: Path, issue: Issue) -> RunResult:
        workspace = workspace.resolve()
        self._assert_workspace(workspace)
        request_id = self._request_id()
        await self._send({"id": request_id, "type": "prompt", "message": prompt})
        response = await self._read_response(request_id, "prompt", self.config.read_timeout_ms)
        if not response.get("success"):
            return RunResult(False, "response_error", str(response.get("error") or "prompt rejected"))
        return await self._read_agent_completion(issue)

    async def stop(self) -> None:
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            try:
                await self._send({"type": "abort"})
            except AgentError:
                pass
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()

    async def _request(
        self, command: dict[str, Any], expected_command: str, timeout_ms: int
    ) -> dict[str, Any]:
        request_id = self._request_id()
        await self._send({"id": request_id, **command})
        response = await self._read_response(request_id, expected_command, timeout_ms)
        if not response.get("success"):
            raise AgentError(
                "response_error", str(response.get("error") or f"{expected_command} failed")
            )
        return response

    async def _read_response(
        self, request_id: int, expected_command: str, timeout_ms: int
    ) -> dict[str, Any]:
        async def read_until() -> dict[str, Any]:
            while True:
                message = await self._read_message()
                if message.get("type") == "response" and message.get("id") == request_id:
                    if message.get("command") not in {None, expected_command}:
                        raise AgentError(
                            "response_error",
                            f"unexpected response command: {message.get('command')}",
                        )
                    return message
                result = await self._handle_event(message)
                if result is not None and not result.success:
                    raise AgentError(result.status, result.error or result.status)

        try:
            return await asyncio.wait_for(read_until(), timeout_ms / 1000)
        except asyncio.TimeoutError as exc:
            raise AgentError("response_timeout", f"{expected_command} timed out") from exc

    async def _read_agent_completion(self, issue: Issue) -> RunResult:
        async def read_until_done() -> RunResult:
            while True:
                message = await self._read_message()
                if message.get("type") == "response":
                    if message.get("success") is False:
                        return RunResult(False, "response_error", str(message.get("error")))
                    continue
                result = await self._handle_event(message)
                if result is not None:
                    return result

        try:
            return await asyncio.wait_for(read_until_done(), self.config.turn_timeout_ms / 1000)
        except asyncio.TimeoutError:
            return RunResult(False, "timed_out", f"Pi turn timed out for {issue.identifier}")

    async def _read_message(self) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdout is None:
            raise AgentError("process_exit", "Pi RPC process is not running")
        line = await process.stdout.readline()
        if not line:
            returncode = process.returncode
            if returncode is None:
                returncode = await process.wait()
            raise AgentError("process_exit", f"Pi RPC process exited with code {returncode}")
        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            await self._emit(
                "malformed",
                {
                    "event": "malformed",
                    "timestamp": _now_iso(),
                    "message": line[:500].decode(errors="replace"),
                },
            )
            raise AgentError("malformed", "Pi RPC emitted invalid JSON") from exc

    async def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise AgentError("process_exit", "Pi RPC process is not running")
        process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        await process.stdin.drain()

    async def _handle_event(self, message: dict[str, Any]) -> RunResult | None:
        event_type = str(message.get("type") or "unknown")
        if event_type == "extension_ui_request":
            await self._handle_extension_ui_request(message)

        event_name = "turn_started" if event_type == "turn_start" else event_type
        payload: dict[str, Any] = {
            "event": event_name,
            "timestamp": _now_iso(),
            "session_id": self.session_id,
            "agent_pid": self.process.pid if self.process else None,
            "message": _summarize_event(message),
            "payload": message,
        }
        usage = _extract_usage(message)
        if usage:
            payload["usage"] = usage
        await self._emit(event_name, payload)

        if event_type == "agent_end":
            return RunResult(True, "succeeded")
        if event_type == "message_update":
            update = message.get("assistantMessageEvent")
            if isinstance(update, dict) and update.get("type") == "error":
                return RunResult(False, "turn_failed", _event_error(update))
        if event_type == "auto_retry_end" and message.get("success") is False:
            return RunResult(False, "turn_failed", str(message.get("finalError") or message))
        return None

    async def _handle_extension_ui_request(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if method in _DIALOG_UI_METHODS and request_id:
            await self._send({"type": "extension_ui_response", "id": request_id, "cancelled": True})

    async def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        result = self.on_event(name, payload)
        if result is not None:
            await result

    async def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            _LOG.debug("pi_stderr pid=%s message=%r", process.pid, line.decode(errors="replace")[:500])

    def _request_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _assert_workspace(self, workspace: Path) -> None:
        try:
            workspace.relative_to(self.workspace_root)
        except ValueError as exc:
            raise AgentError("invalid_workspace_cwd", f"cwd escapes workspace root: {workspace}") from exc


class AgentRunner:
    """Wrap workspace preparation, prompt rendering, and the Pi RPC client."""

    def __init__(
        self,
        config: ServiceConfig,
        workflow: WorkflowDefinition,
        workspace_manager: WorkspaceManager,
        tracker: Any = None,
    ):
        self.config = config
        self.workflow = workflow
        self.workspace_manager = workspace_manager
        self.tracker = tracker

    async def run_attempt(
        self, issue: Issue, attempt: int | None = None, on_event: EventCallback | None = None
    ) -> RunResult:
        workspace = await self.workspace_manager.create_for_issue(issue)
        client: PiRpcClient | None = None
        try:
            await self.workspace_manager.before_run(workspace.path)
            client = PiRpcClient(self.config.pi, self.config.workspace_root, on_event)
            await client.start(workspace.path, issue)
            current_issue = issue
            for turn_number in range(1, self.config.agent.max_turns + 1):
                prompt = _build_turn_prompt(
                    self.workflow.prompt_template, current_issue, attempt, turn_number
                )
                result = await client.run_turn(prompt, workspace.path, current_issue)
                if not result.success:
                    return result
                if self.config.tracker.auto_transition or self.tracker is None:
                    return result
                refreshed = await self.tracker.fetch_issue_states_by_ids([issue.id])
                if refreshed:
                    current_issue = refreshed[0]
                if current_issue.state.lower() not in {
                    state.lower() for state in self.config.tracker.active_states
                }:
                    return result
            return RunResult(True, "max_turns_reached")
        except (AgentError, WorkspaceError, TemplateError) as exc:
            return RunResult(False, getattr(exc, "code", "failed"), str(exc))
        finally:
            if client is not None:
                await client.stop()
            await self.workspace_manager.after_run(workspace.path)


def _build_turn_prompt(template: str, issue: Issue, attempt: int | None, turn_number: int) -> str:
    if turn_number == 1:
        prompt = render_prompt(template, issue, attempt)
    else:
        prompt = (
            f"Continue working on Linear issue {issue.identifier}. "
            "Resume from the current workspace state. Do not repeat completed investigation."
        )
    return _append_issue_comments(prompt, issue)


def _append_issue_comments(prompt: str, issue: Issue) -> str:
    if not issue.comments:
        return prompt
    lines = [prompt.rstrip(), "", "Linear comments (latest first, max 12):"]
    for index, comment in enumerate(issue.comments, start=1):
        metadata = [f"comment {index}"]
        if comment.created_at is not None:
            metadata.append(comment.created_at.isoformat())
        if comment.author:
            metadata.append(comment.author)
        if comment.url:
            metadata.append(comment.url)
        lines.append(f"--- {' | '.join(metadata)} ---")
        lines.append(comment.body)
        lines.append("--- end comment ---")
    return "\n".join(lines)


def _extract_usage(message: dict[str, Any]) -> dict[str, int]:
    if message.get("type") != "agent_end":
        return {}
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    messages = message.get("messages")
    if not isinstance(messages, list):
        return {}
    for item in messages:
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        usage = item.get("usage")
        if not isinstance(usage, dict):
            continue
        current_input = _int_or_zero(usage.get("input") or usage.get("input_tokens"))
        current_output = _int_or_zero(usage.get("output") or usage.get("output_tokens"))
        cache_read = _int_or_zero(usage.get("cacheRead") or usage.get("cache_read"))
        cache_write = _int_or_zero(usage.get("cacheWrite") or usage.get("cache_write"))
        input_tokens += current_input
        output_tokens += current_output
        total_tokens += _int_or_zero(usage.get("totalTokens") or usage.get("total_tokens")) or (
            current_input + current_output + cache_read + cache_write
        )
    if input_tokens == 0 and output_tokens == 0 and total_tokens == 0:
        return {}
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def _summarize_event(message: dict[str, Any]) -> str:
    event_type = str(message.get("type") or "unknown")
    if event_type == "message_update":
        update = message.get("assistantMessageEvent")
        if isinstance(update, dict):
            if update.get("type") == "text_delta":
                return str(update.get("delta") or "")[:500]
            return str(update.get("type") or event_type)
    if event_type == "tool_execution_start":
        return f"tool {message.get('toolName')} started"
    if event_type == "tool_execution_end":
        return f"tool {message.get('toolName')} ended"
    return event_type


def _event_error(update: dict[str, Any]) -> str:
    error = update.get("error")
    if isinstance(error, dict):
        return str(error.get("errorMessage") or error.get("message") or error)
    return str(error or update)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
