"""One dispatcher shared by native tool calls and the developer's wb CLI."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import shlex
import time
from pathlib import Path
from typing import Any

from wavebench.web_fetch import WEB_FETCH_SCHEMA, WebFetch, fit_fetch_result
from wavebench.web_search import WEB_SEARCH_SCHEMA, BraveSearch

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

SPAWN_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn_agent",
        "description": (
            "Delegate one self-contained task to a subagent of your own model. Use it for any "
            "project with several files or subsystems: one agent per independent file or module, "
            "all spawned in the same turn so they run in parallel with disjoint files. The agent "
            "works in this same workspace with wb file tools and lint, but cannot submit or "
            "spawn, and it starts with an empty context: task must be a complete brief with the "
            "objective, the exact files it owns, interfaces or contracts to follow, constraints, "
            "and what to report. Each call returns when its agent finishes, with the report, the "
            "files it changed, and its usage. You remain responsible for integration, lint, "
            "and done."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short label for the agent, for example api-routes.",
                },
                "task": {"type": "string", "description": "Complete brief for the agent."},
                "read_only": {
                    "type": "boolean",
                    "description": "True for research or review agents that must not change files.",
                },
            },
            "required": ["name", "task"],
            "additionalProperties": False,
        },
    },
}


def _subagent_schema() -> list[dict]:
    """The lead's wb schema without submission; subagents report instead of calling done."""
    schema = copy.deepcopy(TOOL_SCHEMA[0])
    function = schema["function"]
    function["description"] = (
        "Workspace commands; relative paths. write replaces UTF-8 content; edit replaces "
        "one exact old match with new. read start/end are inclusive line numbers. "
        "delete needs recursive for a subtree. lint performs trusted static checks on the "
        "entire project. Batch independent native calls. There is no done: the lead agent "
        "submits the project after reading your report. No shell, package scripts, or GUI."
    )
    properties = function["parameters"]["properties"]
    properties["command"]["enum"] = [c for c in properties["command"]["enum"] if c != "done"]
    for key in ("runtime", "entry", "args", "preview"):
        properties.pop(key)
    return [schema]


SUBAGENT_TOOL_SCHEMA = _subagent_schema()
RESEARCH_TOOLS = frozenset({"web_search", "web_fetch"})


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
        self,
        workspace: Workspace,
        runtime: Any,
        metadata: Path,
        limits: Limits,
        on_phase=None,
        on_tool_result=None,
        web_search: BraveSearch | None = None,
        *,
        subagents=None,
        parent: Dispatcher | None = None,
        read_only: bool = False,
    ):
        self.workspace = workspace
        self.runtime = runtime
        self.metadata = metadata
        self.limits = limits
        self.on_phase = on_phase or (lambda _: None)
        self.on_tool_result = on_tool_result or (lambda _: None)
        self.tool_usage = {"calls": 0, "failures": 0}
        # A subagent's dispatcher routes research through its lead's quotas and
        # adds its tool counts to the lead's totals; it can neither submit nor spawn.
        self.subagents = subagents
        self.parent = parent
        self.read_only = read_only
        if parent is not None:
            web_search = parent.web_search
        self.web_search = web_search
        self.web_search_usage = {"calls": 0, "failures": 0}
        self.web_fetch = (
            parent.web_fetch if parent is not None else WebFetch() if web_search else None
        )
        self.web_fetch_usage = {"calls": 0, "failures": 0}
        self.research_closed: str | None = None
        self.research_deadline: float | None = None
        self.research_progress: dict = {}
        if parent is not None:
            self.tools = [
                *SUBAGENT_TOOL_SCHEMA,
                *(t for t in parent.tools if t["function"]["name"] in RESEARCH_TOOLS),
            ]
        else:
            self.tools = (
                [*TOOL_SCHEMA, WEB_FETCH_SCHEMA, WEB_SEARCH_SCHEMA] if web_search else TOOL_SCHEMA
            )
            if subagents is not None:
                self.tools = [*self.tools, SPAWN_TOOL_SCHEMA]
        self.submission: dict | None = None
        self.lint_results: list[dict] = []
        self._calls: dict[str, tuple[str, dict]] = {}
        self._lock = asyncio.Lock()
        self._serial = 0

    def _record_result(self, result: dict) -> dict:
        """Count settled tool outcomes; replaying an identical call adds no work."""
        self.tool_usage["calls"] += 1
        self.tool_usage["failures"] += int(not result["ok"])
        totals = self.tool_usage
        if self.parent is not None:
            self.parent.tool_usage["calls"] += 1
            self.parent.tool_usage["failures"] += int(not result["ok"])
            totals = self.parent.tool_usage
        self.on_tool_result(totals.copy())
        return result

    def reopen(self) -> None:
        self.submission = None

    def available_tools(self) -> list[dict]:
        research = self.parent if self.parent is not None else self
        tools = []
        for tool in self.tools:
            name = tool["function"]["name"]
            if name == "wb":
                tools.append(tool)
            elif name == "spawn_agent":
                if self.subagents is not None and self.subagents.available():
                    tools.append(tool)
            elif not research.research_closed and (
                research.web_search_usage["calls"] < self.limits.web_search_calls
                if name == "web_search"
                else research.web_fetch_usage["calls"] < self.limits.web_fetch_calls
            ):
                tools.append(tool)
        return tools

    def research_budget(self) -> dict:
        if self.parent is not None:
            return self.parent.research_budget()
        return {
            **self.research_progress,
            "search_calls_left": max(
                0, self.limits.web_search_calls - self.web_search_usage["calls"]
            ),
            "read_calls_left": max(0, self.limits.web_fetch_calls - self.web_fetch_usage["calls"]),
            "closed": self.research_closed,
        }

    async def _research(self, name: str, command: dict) -> dict:
        if self.parent is not None:
            # Shared allowances, deadline, and closure: the lead's research budget is per model.
            return await self.parent._research(name, command)
        if self.research_closed:
            raise ValueError(
                f"Research is closed: {self.research_closed}. Build, validate, and submit with wb done"
            )
        try:
            return await asyncio.wait_for(
                self._search(command) if name == "web_search" else self._fetch(command),
                timeout=max(0, self.research_deadline - time.monotonic())
                if self.research_deadline is not None
                else None,
            )
        except asyncio.TimeoutError:
            self.research_closed = "research time allowance reached"
            raise ValueError(
                "Research time allowance reached; use the available evidence and finish the project"
            ) from None

    async def _search(self, arguments: dict) -> dict:
        if self.web_search is None:
            raise ValueError("web_search is disabled for this benchmark")
        if self.submission is not None:
            raise ValueError("phase already submitted; skipped")
        if self.web_search_usage["calls"] >= self.limits.web_search_calls:
            raise ValueError("web search call budget exhausted")
        self.web_search_usage["calls"] += 1
        try:
            if not isinstance(arguments, dict) or set(arguments) - {"query", "count"}:
                raise ValueError("web_search accepts only query and count")
            return await self.web_search.search(arguments.get("query"), arguments.get("count", 5))
        except BaseException:
            self.web_search_usage["failures"] += 1
            raise

    async def _fetch(self, arguments: dict) -> dict:
        if self.web_fetch is None:
            raise ValueError("web_fetch is disabled for this benchmark; enable web search")
        if self.submission is not None:
            raise ValueError("phase already submitted; skipped")
        if self.web_fetch_usage["calls"] >= self.limits.web_fetch_calls:
            raise ValueError("web fetch call budget exhausted")
        self.web_fetch_usage["calls"] += 1
        try:
            if not isinstance(arguments, dict) or set(arguments) - {"url", "start", "max_chars"}:
                raise ValueError("web_fetch accepts only url, start, and max_chars")
            return await self.web_fetch.fetch(
                arguments.get("url"), arguments.get("start", 0), arguments.get("max_chars", 8000)
            )
        except BaseException:
            self.web_fetch_usage["failures"] += 1
            raise

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
        if self.parent is not None and verb == "done":
            raise ValueError("subagents cannot submit; finish with a report for the lead agent")
        if self.read_only and verb in {"write", "edit", "delete"}:
            raise ValueError("this agent is read-only; report findings instead of changing files")
        if self.submission is not None:
            raise ValueError("phase already submitted; skipped")
        # Some providers normalize optional schema fields to required fields.
        # Ignore fields belonging to other verbs; they cannot change execution.
        kwargs = {k: v for k, v in command.items() if k in allowed[verb]}
        required = {
            "read": ("path",),
            "write": ("path", "content"),
            "edit": ("path", "old", "new"),
            "delete": ("path",),
        }
        missing = [key for key in required.get(verb, ()) if kwargs.get(key) is None]
        if missing:
            raise ValueError(f"{verb} requires {', '.join(missing)}")
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

    async def _spawn(self, call_id: str, command: dict) -> dict:
        if self.parent is not None:
            raise ValueError("subagents cannot spawn agents; do the work yourself and report")
        if self.subagents is None:
            raise ValueError("spawn_agent is not enabled for this benchmark")
        return await self.subagents.spawn(call_id, command)

    @staticmethod
    def _conflicts(left: dict, right: dict, left_tool: str = "wb", right_tool: str = "wb") -> bool:
        if left.get("command") in {"lint", "done"} or right.get("command") in {"lint", "done"}:
            return True
        if "spawn_agent" in (left_tool, right_tool):
            # Agents start after the batch's earlier file changes, and later file
            # changes wait for them; reads, research, and other spawns overlap.
            other = right if left_tool == "spawn_agent" else left
            return other.get("command") in {"write", "edit", "delete"}
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
            names = []
            # A subagent's done is rejected individually; only the lead can submit.
            has_done = self.parent is None and any(
                (c.get("arguments") or {}).get("command") == "done" and c.get("name", "wb") == "wb"
                for c in calls
                if isinstance(c.get("arguments"), dict)
            )
            for index, call in enumerate(calls):
                command = call.get("arguments", {})
                commands.append(command if isinstance(command, dict) else {})
                names.append(call.get("name", "wb"))
                dependencies = [
                    task
                    for prev, task in enumerate(tasks)
                    if self._conflicts(commands[prev], commands[index], names[prev], names[index])
                    or calls[prev].get("id") == call.get("id")
                ]

                async def execute(
                    call=call, command=command, index=index, dependencies=dependencies
                ):
                    await asyncio.gather(*dependencies)
                    call_id = call.get("id", f"invalid-{index}")
                    signature = json.dumps([call.get("name", "wb"), command], sort_keys=True)
                    cached = self._calls.get(call_id)
                    if cached:
                        if cached[0] == signature:
                            return cached[1]
                        return self._record_result(
                            {
                                "id": call_id,
                                "ok": False,
                                "error": "call ID reused with different arguments; skipped",
                            }
                        )
                    # Subagent runs are governed by their own parallel window, not
                    # the file-operation slots, so file work continues beside them.
                    slot = (
                        contextlib.nullcontext() if call.get("name") == "spawn_agent" else semaphore
                    )
                    async with slot:
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
                            if call.get("name", "wb") not in {
                                "wb",
                                "web_search",
                                "web_fetch",
                                "spawn_agent",
                            }:
                                raise ValueError(
                                    'unknown tool; call the function named wb with {"command":"write", "path":"...", "content":"..."}, or another documented command'
                                )
                            if call.get("error"):
                                raise ValueError(call["error"])
                            if call.get("name") in {"web_search", "web_fetch"}:
                                payload = await self._research(call["name"], command)
                            elif call.get("name") == "spawn_agent":
                                payload = await self._spawn(call_id, command)
                            else:
                                payload = await self._execute(command)
                            result = {
                                "id": call_id,
                                "ok": payload.get("exit_code", 0) == 0,
                                **payload,
                            }
                            if call.get("name") in {"web_search", "web_fetch"}:
                                result["research_budget"] = self.research_budget()
                            if call.get("name") == "web_fetch":
                                result = fit_fetch_result(result, self.limits.output_chars)
                                if not result["ok"]:
                                    research = self.parent if self.parent is not None else self
                                    research.web_fetch_usage["failures"] += 1
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
                            if call.get("name") in {"web_search", "web_fetch"}:
                                result["research_budget"] = self.research_budget()
                        finally:
                            self._serial += 1
                            if "result" in locals():
                                self._record_result(result)
                                full = {
                                    **result,
                                    "command": command,
                                    "tool": call.get("name", "wb"),
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
                        self._calls[call_id] = (
                            json.dumps([call.get("name", "wb"), command], sort_keys=True),
                            result,
                        )
                        self._record_result(result)
                        (self.metadata / f"tool-{self._serial:04d}.json").write_text(
                            json.dumps(
                                {**result, "command": command, "tool": call.get("name", "wb")}
                            )
                        )
                raise
