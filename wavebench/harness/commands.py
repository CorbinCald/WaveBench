"""One dispatcher shared by native tool calls and the developer's wb CLI."""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from pathlib import Path
from typing import Any

from .config import Limits
from .workspace import Workspace

TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "wb",
            "description": (
                "Workspace commands; relative paths. write replaces UTF-8 content; edit replaces "
                "one exact old match with new. read start/end are inclusive line numbers. "
                "delete needs recursive for a subtree. lint performs trusted static checks. "
                "Batch independent native calls. lint checks the entire project. "
                "done must be alone and requires runtime and entry: "
                '{"command":"done","runtime":"python","entry":"main.py"}. It submits, never executes. '
                "Runtimes: python, node (exiting programs), python-server, node-server "
                "(HTTP on PORT), static (HTML). args are literal program arguments. "
                "preview is a local /path for HTTP readiness. No shell, package scripts, or GUI."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "enum": ["ls", "read", "write", "edit", "delete", "lint", "done"],
                    },
                    "path": {"type": "string"},
                    "content": {
                        "type": "string",
                        "description": "Actual file text with real newlines; do not double-escape newlines or quotes.",
                    },
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "start": {"type": "integer", "minimum": 1},
                    "end": {"type": "integer", "minimum": 1},
                    "recursive": {"type": "boolean"},
                    "runtime": {
                        "type": "string",
                        "enum": ["python", "node", "python-server", "node-server", "static"],
                    },
                    "entry": {
                        "type": "string",
                        "description": "Required for done: relative path of an existing program/HTML entry file.",
                    },
                    "args": {"type": "array", "items": {"type": "string"}},
                    "preview": {"type": "string"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    }
]


def parse_command(text: str, data: dict | None = None) -> dict:
    """Parse verbs directly. shlex is only a tokenizer, never a shell evaluator."""
    words = shlex.split(text)
    if words and words[0] == "wb":
        words.pop(0)
    if not words:
        raise ValueError("missing command")
    command = {"command": words.pop(0), **(data or {})}
    if command["command"] in {"ls", "read", "write", "edit", "delete"} and words:
        command["path"] = words.pop(0)
    if command["command"] == "read" and words:
        bounds = words.pop(0).split(":")
        command["start"] = int(bounds[0])
        if len(bounds) == 2:
            command["end"] = int(bounds[1])
    if command["command"] == "delete" and words == ["--recursive"]:
        command["recursive"] = True
        words.clear()
    if words:
        raise ValueError(
            "unexpected arguments; supply content/edits/launch descriptor as JSON or stdin"
        )
    return command


def launch_descriptor(command: dict, workspace: Workspace) -> dict:
    runtime = command.get("runtime")
    if runtime not in {"python", "node", "python-server", "node-server", "static"}:
        raise ValueError(
            "unsupported runtime/validation environment; use python, node, their -server variants, or static"
        )
    entry = command.get("entry")
    workspace.read(entry)  # Verifies containment, existence, regular file, and readable size.
    suffixes = {"python": {".py"}, "node": {".js", ".mjs", ".cjs"}, "static": {".html", ".htm"}}
    kind = runtime.removesuffix("-server")
    if Path(entry).suffix not in suffixes[kind]:
        raise ValueError(f"{runtime} entry has an unsupported file extension")
    args = command.get("args", [])
    if (
        not isinstance(args, list)
        or len(args) > 64
        or any(not isinstance(v, str) or "\0" in v or len(v) > 4096 for v in args)
    ):
        raise ValueError("args must be at most 64 literal strings of at most 4096 characters")
    if runtime.endswith("-server") and any(
        arg.split("=", 1)[0] in {"--reload", "--watch", "--debug", "--dev"} for arg in args
    ):
        raise ValueError("development reload/watch/debug arguments are unsupported")
    # Runtime options cannot be inserted before the validated entry point.
    preview = command.get("preview") or (f"/{entry}" if runtime == "static" else "/")
    if (
        not isinstance(preview, str)
        or not preview.startswith("/")
        or preview.startswith("//")
        or any(ord(c) < 32 or c in "\\#" for c in preview)
    ):
        raise ValueError("preview must be a local absolute URL path, e.g. / or /index.html")
    if args and runtime == "static":
        raise ValueError("static projects do not take program arguments")
    return {"runtime": runtime, "entry": entry, "args": args, "preview": preview}


class Dispatcher:
    def __init__(
        self, workspace: Workspace, runtime: Any, metadata: Path, limits: Limits, on_phase=None
    ):
        self.workspace = workspace
        self.runtime = runtime
        self.metadata = metadata
        self.limits = limits
        self.on_phase = on_phase or (lambda _: None)
        self.submission: dict | None = None
        self.lint_results: list[dict] = []
        self._calls: dict[str, tuple[str, dict]] = {}
        self._lock = asyncio.Lock()
        self._serial = 0

    def reopen(self) -> None:
        self.submission = None

    async def _execute(self, command: dict) -> dict:
        if not isinstance(command, dict):
            raise ValueError("command arguments must be an object")
        verb = command.get("command")
        allowed = {
            "ls": {"path"},
            "read": {"path", "start", "end"},
            "write": {"path", "content"},
            "edit": {"path", "old", "new"},
            "delete": {"path", "recursive"},
            "lint": set(),
            "done": {"runtime", "entry", "args", "preview"},
        }
        known = set(TOOL_SCHEMA[0]["function"]["parameters"]["properties"])
        if verb not in allowed or set(command) - known:
            raise ValueError("unknown verb or unexpected arguments")
        if self.submission is not None:
            raise ValueError("phase already submitted; skipped")
        # Some providers normalize optional schema fields to required fields.
        # Ignore fields belonging to other verbs; they cannot change execution.
        kwargs = {k: v for k, v in command.items() if k in allowed[verb]}
        if "recursive" in kwargs and type(kwargs["recursive"]) is not bool:
            raise ValueError("recursive must be a boolean")
        if verb == "done":
            self.submission = launch_descriptor(command, self.workspace)
            return {"submitted": self.submission, "execution": "waiting for WaveBench scheduler"}
        if verb == "lint":
            self.on_phase("linting")
            result = await self.runtime.lint()
            self.lint_results.append(result)
            return result
        operation = asyncio.create_task(asyncio.to_thread(getattr(self.workspace, verb), **kwargs))
        try:
            value = await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Settle an admitted filesystem operation before closing its root FD.
            await operation
            raise
        return {"content": value} if verb in {"read", "ls"} else value

    @staticmethod
    def _conflicts(left: dict, right: dict) -> bool:
        if left.get("command") in {"lint", "done"} or right.get("command") in {"lint", "done"}:
            return True
        if left.get("command") in {"ls", "read"} and right.get("command") in {"ls", "read"}:
            return False
        a = str(left.get("path", ".")).strip("/")
        b = str(right.get("path", ".")).strip("/")
        a = "/".join(p for p in a.split("/") if p and p != ".")
        b = "/".join(p for p in b.split("/") if p and p != ".")
        return not a or not b or a == b or a.startswith(b + "/") or b.startswith(a + "/")

    async def batch(self, calls: list[dict]) -> list[dict]:
        """Results stay in submitted order; a dependency waits for every prior conflict."""
        async with self._lock:
            semaphore = asyncio.Semaphore(self.limits.parallel_calls)
            tasks = []
            commands = []
            has_done = any(
                (c.get("arguments") or {}).get("command") == "done"
                for c in calls
                if isinstance(c.get("arguments"), dict)
            )
            for index, call in enumerate(calls):
                command = call.get("arguments", {})
                commands.append(command if isinstance(command, dict) else {})
                dependencies = [
                    task
                    for prev, task in enumerate(tasks)
                    if self._conflicts(commands[prev], commands[index])
                    or calls[prev].get("id") == call.get("id")
                ]

                async def execute(
                    call=call, command=command, index=index, dependencies=dependencies
                ):
                    await asyncio.gather(*dependencies)
                    call_id = call.get("id", f"invalid-{index}")
                    signature = json.dumps(command, sort_keys=True)
                    cached = self._calls.get(call_id)
                    if cached:
                        if cached[0] == signature:
                            return cached[1]
                        return {
                            "id": call_id,
                            "ok": False,
                            "error": "call ID reused with different arguments; skipped",
                        }
                    async with semaphore:
                        started = time.monotonic()
                        try:
                            if not isinstance(call_id, str) or not call_id:
                                raise ValueError("missing call ID")
                            if index >= self.limits.batch_calls:
                                raise ValueError("batch call budget exceeded; skipped")
                            if has_done and len(calls) != 1:
                                raise ValueError(
                                    "done must be submitted alone; entire batch skipped"
                                )
                            if call.get("name", "wb") != "wb":
                                raise ValueError(
                                    'unknown tool; call the function named wb with {"command":"write", "path":"...", "content":"..."}, or another documented command'
                                )
                            if call.get("error"):
                                raise ValueError(call["error"])
                            payload = await self._execute(command)
                            result = {
                                "id": call_id,
                                "ok": payload.get("exit_code", 0) == 0,
                                **payload,
                            }
                        except asyncio.CancelledError:
                            result = {
                                "id": call_id,
                                "ok": False,
                                "error": "cancelled; operation stopped",
                            }
                            self._calls[call_id] = (signature, result)
                            raise
                        except Exception as exc:
                            result = {
                                "id": call_id,
                                "ok": False,
                                "error": str(exc) or type(exc).__name__,
                            }
                        finally:
                            self._serial += 1
                            if "result" in locals():
                                full = {
                                    **result,
                                    "command": command,
                                    "time_s": time.monotonic() - started,
                                }
                                (self.metadata / f"tool-{self._serial:04d}.json").write_text(
                                    json.dumps(full, ensure_ascii=False)
                                )
                        encoded = json.dumps(result, ensure_ascii=False)
                        if len(encoded) > self.limits.output_chars:
                            result = {
                                "id": call_id,
                                "ok": result["ok"],
                                "truncated": True,
                                "content": encoded[: self.limits.output_chars],
                                "diagnostics": f"tool-{self._serial:04d}.json",
                            }
                        self._calls[call_id] = (signature, result)
                        return result

                tasks.append(asyncio.create_task(execute()))
            try:
                return await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for index, call in enumerate(calls):
                    call_id = call.get("id", f"invalid-{index}")
                    if call_id not in self._calls:
                        self._serial += 1
                        result = {
                            "id": call_id,
                            "ok": False,
                            "error": "cancelled before execution; skipped",
                        }
                        command = call.get("arguments", {})
                        self._calls[call_id] = (json.dumps(command, sort_keys=True), result)
                        (self.metadata / f"tool-{self._serial:04d}.json").write_text(
                            json.dumps({**result, "command": command})
                        )
                raise
