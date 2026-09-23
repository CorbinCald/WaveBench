"""Model tools and the dispatcher shared by native tool calls and the developer's wb CLI.

Each tool does one thing and takes only the arguments it needs. Some providers
fill every optional property (empty strings, false, or zero), so every optional
argument treats those values as "not set". Results reach the model as plain
text; the full structured record of every call is saved under metadata/.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import html
import json
import re
import shlex
import time
from pathlib import Path
from typing import Any

from wavebench.web_fetch import WEB_FETCH_SCHEMA, WebFetch
from wavebench.web_search import WEB_SEARCH_SCHEMA, BraveSearch

from .config import Limits
from .workspace import Workspace

RUNTIMES = ("python", "node", "python-server", "node-server", "static")


def _tool(name: str, description: str, properties: dict | None = None, required=()) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


_PATH = {"type": "string"}

READ_FILE = _tool(
    "read_file",
    "Read a file. Returns the whole file if it fits, otherwise a section and how to continue. "
    "start_line/end_line (1-based, inclusive; 0 means the file's start/end) select lines.",
    {
        "path": _PATH,
        "start_line": {"type": "integer", "minimum": 0},
        "end_line": {"type": "integer", "minimum": 0},
    },
    ["path"],
)
WRITE_FILE = _tool(
    "write_file",
    "Create or overwrite a file with its complete content. append adds to the end instead, "
    "for writing a very large file in parts.",
    {"path": _PATH, "content": {"type": "string"}, "append": {"type": "boolean"}},
    ["path", "content"],
)
EDIT_FILE = _tool(
    "edit_file",
    "Replace old_text with new_text in a file. old_text must match exactly, including "
    "indentation, and be unique unless replace_all is set. Prefer this to rewriting a file.",
    {
        "path": _PATH,
        "old_text": {"type": "string"},
        "new_text": {"type": "string"},
        "replace_all": {"type": "boolean"},
    },
    ["path", "old_text", "new_text"],
)
LIST_FILES = _tool(
    "list_files",
    "List files recursively with line counts; path narrows it to a directory.",
    {"path": _PATH},
)
DELETE_FILE = _tool("delete_file", "Delete a file or directory.", {"path": _PATH}, ["path"])
LINT = _tool(
    "lint",
    "Syntax-check every project file without running it: Python, JavaScript (including "
    "inline HTML scripts), JSON, and HTML.",
)
SUBMIT = _tool(
    "submit",
    "Submit the project for WaveBench to run. runtime: python or node for a program that "
    "exits, python-server or node-server for an HTTP server on the PORT environment variable, "
    "static for HTML. entry: the entry file. Optional args: program arguments; preview: URL "
    "path to open. Runs after this response's other calls and is refused if one failed.",
    {
        "runtime": {"type": "string", "enum": list(RUNTIMES)},
        "entry": {"type": "string"},
        "args": {"type": "array", "items": {"type": "string"}},
        "preview": {"type": "string"},
    },
    ["runtime", "entry"],
)
SPAWN_AGENT = _tool(
    "spawn_agent",
    "Delegate a self-contained task to a fresh instance of your model working in this project "
    "with the file tools. It sees nothing else, so task must be a complete brief: objective, "
    "files it owns, interfaces to follow, and what to report. Calls in one response run in "
    "parallel; give them disjoint files. Returns the agent's report.",
    {
        "name": {"type": "string", "description": "Short label, for example physics."},
        "task": {"type": "string"},
        "read_only": {"type": "boolean", "description": "For research or review agents."},
    },
    ["name", "task"],
)

WORKSPACE_TOOLS = [READ_FILE, WRITE_FILE, EDIT_FILE, LIST_FILES, DELETE_FILE, LINT]
TOOL_SCHEMA = [*WORKSPACE_TOOLS, SUBMIT]
FILE_CHANGES = frozenset({"write_file", "edit_file", "delete_file"})
READS = frozenset({"read_file", "list_files"})
RESEARCH_TOOLS = frozenset({"web_search", "web_fetch"})
BARRIERS = frozenset({"lint", "submit"})
KNOWN_TOOLS = frozenset(
    t["function"]["name"] for t in [*TOOL_SCHEMA, SPAWN_AGENT, WEB_SEARCH_SCHEMA, WEB_FETCH_SCHEMA]
)
ARGUMENTS = {
    t["function"]["name"]: set(t["function"]["parameters"]["properties"])
    for t in [*TOOL_SCHEMA, SPAWN_AGENT, WEB_SEARCH_SCHEMA, WEB_FETCH_SCHEMA]
}
REQUIRED = {
    t["function"]["name"]: t["function"]["parameters"]["required"]
    for t in [*TOOL_SCHEMA, SPAWN_AGENT, WEB_SEARCH_SCHEMA, WEB_FETCH_SCHEMA]
}

# wb CLI verbs for the same tools.
VERBS = {
    "ls": "list_files",
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "delete": "delete_file",
    "lint": "lint",
    "submit": "submit",
    "done": "submit",
}


def parse_command(text: str, data: dict | None = None) -> dict:
    """wb VERB [PATH] [START:END] → a tool call. shlex only tokenizes; nothing is evaluated."""
    words = shlex.split(text)
    if words and words[0] == "wb":
        words.pop(0)
    if not words:
        raise ValueError("missing command")
    verb = words.pop(0)
    name = VERBS.get(verb, verb)
    if name not in KNOWN_TOOLS:
        raise ValueError(f"unknown command {verb!r}")
    arguments = dict(data or {})
    if name in {"list_files", "read_file", "write_file", "edit_file", "delete_file"} and words:
        arguments["path"] = words.pop(0)
    if name == "read_file" and words:
        bounds = words.pop(0).split(":")
        arguments["start_line"] = int(bounds[0])
        if len(bounds) == 2:
            arguments["end_line"] = int(bounds[1])
    if words:
        raise ValueError(
            "unexpected arguments; supply content, edits, or submission as JSON on stdin"
        )
    return {"name": name, "arguments": arguments}


def launch_descriptor(arguments: dict, workspace: Workspace) -> dict:
    runtime = arguments.get("runtime")
    if runtime not in RUNTIMES:
        raise ValueError("runtime must be python, node, python-server, node-server, or static")
    entry = arguments.get("entry")
    if not isinstance(entry, str) or not entry:
        raise ValueError("entry must be the relative path of the entry file")
    workspace.read(entry)  # Verifies containment, existence, regular file, and readable size.
    suffixes = {"python": {".py"}, "node": {".js", ".mjs", ".cjs"}, "static": {".html", ".htm"}}
    if Path(entry).suffix not in suffixes[runtime.removesuffix("-server")]:
        raise ValueError(f"{runtime} entry has an unsupported file extension")
    args = arguments.get("args") or []
    if (
        not isinstance(args, list)
        or len(args) > 64
        or any(not isinstance(v, str) or "\0" in v or len(v) > 4096 for v in args)
    ):
        raise ValueError("args must be at most 64 literal strings of at most 4096 characters")
    if args and runtime == "static":
        raise ValueError("static projects do not take program arguments")
    if runtime.endswith("-server") and any(
        arg.split("=", 1)[0] in {"--reload", "--watch", "--debug", "--dev"} for arg in args
    ):
        raise ValueError("development reload/watch/debug arguments are unsupported")
    preview = arguments.get("preview") or (f"/{entry}" if runtime == "static" else "/")
    if (
        not isinstance(preview, str)
        or not preview.startswith("/")
        or preview.startswith("//")
        or any(ord(c) < 32 or c in "\\#" for c in preview)
    ):
        raise ValueError("preview must be a local URL path such as / or /index.html")
    return {"runtime": runtime, "entry": entry, "args": args, "preview": preview}


def _clean_path(value, *, optional: bool = False) -> str:
    if optional and value in (None, "", ".", "/", "./"):
        return "."
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path is required")
    return value.strip().removeprefix("./")


def _number(value) -> int | None:
    """Optional line numbers: absent, null, 0, or false mean unset."""
    if value in (None, 0, False, ""):
        return None
    if type(value) is not int or value < 0:
        raise ValueError("line numbers must be positive integers")
    return value


def _plain(value: str) -> str:
    """Search snippets arrive with HTML entities and emphasis tags."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", value or "")).split())


def _lines(count: int) -> str:
    return f"{count:,} line{'' if count == 1 else 's'}"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    cut = cut if cut > limit // 2 else limit
    return text[:cut] + f"\n[Output truncated: {len(text) - cut:,} more characters not shown.]"


def render(name: str, result: dict, limits: Limits) -> str:
    """The text a model sees for one tool result; never raises."""
    try:
        return _render(name, result, limits)
    except Exception:
        fields = {k: v for k, v in result.items() if k not in {"id", "text"}}
        return _clip(json.dumps(fields, ensure_ascii=False, default=str), limits.output_chars)


def _render(name: str, result: dict, limits: Limits) -> str:
    ran_agent = name == "spawn_agent" and "agent" in result
    if not result.get("ok") and result.get("error") and not ran_agent:
        text = f"Error: {result['error']}"
    elif name == "read_file":
        text = result["text"] if result["text"] else "(empty file)"
        if result["partial"]:
            text = f"[{result['path']}: lines {result['start_line']}-{result['end_line']} of {result['total_lines']}]\n{text}"
            if result["end_line"] < result["total_lines"]:
                text += f"\n[Continue with start_line={result['end_line'] + 1}.]"
        return text  # Bounded by read_chars, which may exceed the general output limit.
    elif name == "write_file":
        if result.get("appended"):
            text = f"Appended to {result['path']} (now {_lines(result['lines'])})."
        else:
            text = f"Wrote {result['path']} ({_lines(result['lines'])})."
    elif name == "edit_file":
        count = result["replacements"]
        text = f"Edited {result['path']} ({count} replacement{'s' if count != 1 else ''})."
    elif name == "list_files":
        files = result["files"]
        width = max((len(f["path"]) for f in files), default=0)
        text = (
            "\n".join(
                f"{f['path']:<{width}}  "
                + (_lines(f["lines"]) if f["lines"] is not None else f"{f['bytes']:,} bytes")
                for f in files
            )
            or "(no files)"
        )
        if result["truncated"]:
            text += "\n[Listing truncated; pass a directory path to see more.]"
    elif name == "delete_file":
        text = f"Deleted {result['deleted']}."
    elif name == "lint":
        diagnostics = (result.get("diagnostics") or "").strip()
        if result.get("error"):
            diagnostics = f"{result['error']}\n{diagnostics}".strip()
        text = diagnostics if result["ok"] else f"Lint found problems:\n{diagnostics}"
    elif name == "submit":
        launch = result["submitted"]
        text = f"Submitted {launch['entry']} as {launch['runtime']}. WaveBench runs it after this turn."
    elif name == "web_search":
        lines = [
            f"{number}. {_plain(item.get('title'))}\n   {item.get('url')}\n   "
            + _plain(item.get("description") or item.get("snippet"))
            for number, item in enumerate(result["results"], 1)
        ]
        text = "\n".join(lines) or "No results."
    elif name == "web_fetch":
        header = [f"Source: {result.get('final_url') or result.get('url')}"]
        if result.get("final_url") and result.get("url") != result["final_url"]:
            header.append(f"Requested: {result['url']}")
        if result.get("title"):
            header.append(f"Title: {result['title']}")
        dates = [
            f"{label}: {result[key]}"
            for label, key in (("Published", "published_at"), ("Modified", "modified_at"))
            if result.get(key)
        ]
        dates.append(f"Retrieved: {result.get('fetched_at')}")
        header.append(" | ".join(dates))
        if result.get("notice"):
            header.append(result["notice"])
        text = "\n".join(header) + "\n\n" + (result.get("content") or "")
        if result.get("next_start") is not None:
            text += (
                f"\n\n[Characters {result['start']:,}-{result['next_start']:,} of "
                f"{result['total_chars']:,}. Continue with start={result['next_start']}.]"
            )
    elif name == "spawn_agent":
        files = result.get("files") or {}
        changed = "; ".join(f"{kind}: {', '.join(paths)}" for kind, paths in files.items() if paths)
        text = (
            f"Agent {result['agent']} {result['status']} after {result['turns']} "
            f"request{'' if result['turns'] == 1 else 's'} ({result['time_s']:g}s)."
            + (f" Files {changed}." if changed else " No files changed.")
            + (f" Error: {result['error']}" if result.get("error") else "")
            + f"\nReport:\n{result.get('report') or '(none)'}"
            + ("\n[Report truncated.]" if result.get("report_truncated") else "")
            + f"\n({result['agents_left']} agents left.)"
        )
    else:
        text = json.dumps({k: v for k, v in result.items() if k not in {"id", "ok"}})
    if "calls_left" in result:
        text += f"\n({result['calls_left']})"
    return _clip(text, limits.output_chars)


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
        if parent is not None:
            tools = [
                t
                for t in WORKSPACE_TOOLS
                if not (read_only and t["function"]["name"] in FILE_CHANGES)
            ]
        else:
            tools = [*TOOL_SCHEMA, *([SPAWN_AGENT] if subagents is not None else [])]
        if web_search is not None:
            tools += [WEB_SEARCH_SCHEMA, WEB_FETCH_SCHEMA]
        self.tools = copy.deepcopy(tools)
        self.submission: dict | None = None
        self.lint_results: list[dict] = []
        self._calls: dict[str, tuple[str, dict]] = {}
        self._lock = asyncio.Lock()
        self._serial = 0

    @property
    def research(self) -> Dispatcher:
        return self.parent if self.parent is not None else self

    def reopen(self) -> None:
        self.submission = None

    def close_research(self, reason: str) -> bool:
        """Withdraw research for the rest of the session; True the first time."""
        research = self.research
        if research.web_search is None or research.research_closed:
            return False
        research.research_closed = reason
        return True

    def research_available(self, name: str) -> bool:
        research = self.research
        if research.research_closed or research.web_search is None:
            return False
        if name == "web_search":
            return research.web_search_usage["calls"] < self.limits.web_search_calls
        return research.web_fetch_usage["calls"] < self.limits.web_fetch_calls

    def available_tools(self) -> list[dict]:
        tools = []
        for tool in self.tools:
            name = tool["function"]["name"]
            if name == "spawn_agent":
                if self.subagents is not None and self.subagents.available():
                    tools.append(tool)
            elif name in RESEARCH_TOOLS:
                if self.research_available(name):
                    tools.append(tool)
            else:
                tools.append(tool)
        return tools

    def calls_left(self) -> str:
        research = self.research
        search = max(0, self.limits.web_search_calls - research.web_search_usage["calls"])
        fetch = max(0, self.limits.web_fetch_calls - research.web_fetch_usage["calls"])
        if research.research_closed:
            return "Research is closed."
        return f"{search} searches and {fetch} page reads left"

    def _record_result(self, result: dict) -> None:
        """Count settled tool outcomes; replaying an identical call adds no work."""
        for usage in [self.tool_usage, *([self.parent.tool_usage] if self.parent else [])]:
            usage["calls"] += 1
            usage["failures"] += int(not result["ok"])
        self.on_tool_result((self.parent or self).tool_usage.copy())

    async def _research(self, name: str, arguments: dict) -> dict:
        research = self.research
        if research.research_closed:
            raise ValueError(
                f"research is closed ({research.research_closed}); build with the evidence you have"
            )
        usage = research.web_search_usage if name == "web_search" else research.web_fetch_usage
        limit = (
            self.limits.web_search_calls if name == "web_search" else self.limits.web_fetch_calls
        )
        if usage["calls"] >= limit:
            raise ValueError(f"{name} call limit reached ({limit} per model)")
        usage["calls"] += 1
        try:
            if name == "web_search":
                return await research.web_search.search(
                    arguments.get("query"), arguments.get("count") or 5
                )
            if research.web_fetch is None:
                raise ValueError("web_fetch is disabled for this benchmark")
            # A section always fits the tool output with its source header and continuation.
            size = min(
                arguments.get("max_chars") or 8000, max(1000, self.limits.output_chars - 2000)
            )
            return await research.web_fetch.fetch(
                arguments.get("url"), arguments.get("start") or 0, size
            )
        except BaseException:
            usage["failures"] += 1
            raise

    def _read(self, arguments: dict) -> dict:
        path = _clean_path(arguments.get("path"))
        start, end = _number(arguments.get("start_line")), _number(arguments.get("end_line"))
        text = self.workspace.read(path)
        lines = text.splitlines(keepends=True)
        total = len(lines)
        if start and start > max(total, 1):
            raise ValueError(f"start_line {start} is past the end of {path} ({total} lines)")
        first = start or 1
        last = total if end is None or end > total else max(end, first)
        chosen = lines[first - 1 : last]
        # Whole lines up to the read allowance; a single huge line is cut.
        size = 0
        for index, line in enumerate(chosen):
            size += len(line)
            if size > self.limits.read_chars:
                chosen = chosen[:index] or [line[: self.limits.read_chars]]
                last = first + len(chosen) - 1
                break
        return {
            "path": path,
            "text": "".join(chosen),
            "start_line": first,
            "end_line": last,
            "total_lines": total,
            "partial": first > 1 or last < total,
        }

    async def _execute(self, name: str, arguments: dict) -> dict:
        if self.submission is not None:
            raise ValueError("the project was already submitted; this call was skipped")
        if self.read_only and name in FILE_CHANGES:
            raise ValueError("this agent is read-only; report findings instead of changing files")
        if name == "submit":
            if self.parent is not None:
                raise ValueError("subagents cannot submit; finish with a report for the lead agent")
            self.submission = launch_descriptor(arguments, self.workspace)
            return {"submitted": self.submission}
        if name == "lint":
            self.on_phase("linting")
            result = await self.runtime.lint()
            self.lint_results.append(result)
            return {**result, "ok": result.get("exit_code") == 0 and not result.get("error")}
        operations = {
            "read_file": lambda: self._read(arguments),
            "write_file": lambda: self._write(arguments),
            "edit_file": lambda: self._edit(arguments),
            "list_files": lambda: self.workspace.tree(
                _clean_path(arguments.get("path"), optional=True)
            ),
            "delete_file": lambda: self.workspace.delete(
                _clean_path(arguments.get("path")), recursive=True
            ),
        }
        operation = asyncio.create_task(asyncio.to_thread(operations[name]))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Settle an admitted filesystem operation before closing its root FD.
            await operation
            raise
        except FileNotFoundError:
            raise ValueError(
                f"{arguments.get('path') or 'path'} does not exist; list_files shows the project's files"
            ) from None
        except (IsADirectoryError, NotADirectoryError):
            raise ValueError(f"{arguments.get('path')} is a directory, not a file") from None

    def _write(self, arguments: dict) -> dict:
        path, content = _clean_path(arguments.get("path")), arguments.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be the file text")
        if arguments.get("append") is True:
            with contextlib.suppress(FileNotFoundError):
                content = self.workspace.read(path) + content
        result = self.workspace.write(path, content)
        lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
        return {**result, "lines": lines, "appended": arguments.get("append") is True}

    def _edit(self, arguments: dict) -> dict:
        return self.workspace.edit(
            _clean_path(arguments.get("path")),
            arguments.get("old_text"),
            arguments.get("new_text"),
            replace_all=arguments.get("replace_all") is True,
        )

    async def _run(self, name: str, arguments: dict, call_id: str, earlier_failed: bool) -> dict:
        if not any(t["function"]["name"] == name for t in self.tools):
            if self.parent is not None and name == "spawn_agent":
                raise ValueError("subagents cannot spawn agents; do the work yourself and report")
            if self.parent is not None and name == "submit":
                raise ValueError("subagents cannot submit; finish with a report for the lead agent")
            if self.read_only and name in FILE_CHANGES:
                raise ValueError(
                    "this agent is read-only; report findings instead of changing files"
                )
            if name in RESEARCH_TOOLS or name == "spawn_agent":
                raise ValueError(f"{name} is not enabled for this benchmark")
            available = ", ".join(t["function"]["name"] for t in self.tools)
            raise ValueError(f"unknown tool {name!r}; available tools: {available}")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be a JSON object")
        unexpected = set(arguments) - ARGUMENTS[name]
        if unexpected:
            raise ValueError(f"{name} does not accept {', '.join(sorted(unexpected))}")
        missing = [key for key in REQUIRED[name] if arguments.get(key) is None]
        if missing:
            raise ValueError(f"{name} requires {', '.join(missing)}")
        if name == "submit" and earlier_failed:
            raise ValueError(
                "not submitted because an earlier call in this response failed; fix it, then submit"
            )
        if name in RESEARCH_TOOLS:
            return {**await self._research(name, arguments), "calls_left": self.calls_left()}
        if name == "spawn_agent":
            if self.subagents is None:
                raise ValueError("spawn_agent is not enabled for this benchmark")
            return await self.subagents.spawn(call_id, arguments)
        return await self._execute(name, arguments)

    @staticmethod
    def _conflicts(left: dict, right: dict) -> bool:
        """Whether right must wait for left. Barriers wait for, and block, everything."""
        names = {left["name"], right["name"]}
        if names & BARRIERS:
            return True
        if "spawn_agent" in names:
            # Agents start after the batch's earlier file changes, and later file
            # changes wait for them; reads, research, and other spawns overlap.
            other = right if left["name"] == "spawn_agent" else left
            return other["name"] in FILE_CHANGES
        if names & RESEARCH_TOOLS or names <= READS:
            return False

        def path(call):
            value = (
                (call.get("arguments") or {}).get("path")
                if isinstance(call.get("arguments"), dict)
                else None
            )
            parts = str(value or ".").strip("/").split("/")
            return "/".join(p for p in parts if p and p != ".")

        a, b = path(left), path(right)
        return not a or not b or a == b or a.startswith(b + "/") or b.startswith(a + "/")

    @staticmethod
    def _signature(call: dict) -> str:
        return json.dumps([call.get("name"), call.get("arguments")], sort_keys=True, default=str)

    def _save(self, call: dict, result: dict, started: float) -> None:
        self._serial += 1
        record = {
            **result,
            "tool": call.get("name"),
            "arguments": call.get("arguments"),
            "time_s": time.monotonic() - started,
        }
        (self.metadata / f"tool-{self._serial:04d}.json").write_text(
            json.dumps(record, ensure_ascii=False, default=str)
        )

    async def batch(self, calls: list[dict]) -> list[dict]:
        """Results stay in submitted order; each call waits for every earlier conflicting call.

        Each result carries the rendered text the model sees under "text".
        """
        async with self._lock:
            semaphore = asyncio.Semaphore(self.limits.parallel_calls)
            tasks: list[asyncio.Task] = []

            for index, call in enumerate(calls):
                dependencies = [
                    tasks[prev]
                    for prev in range(index)
                    if self._conflicts(calls[prev], call) or calls[prev].get("id") == call.get("id")
                ]

                async def execute(call=call, index=index, dependencies=dependencies):
                    earlier = await asyncio.gather(*dependencies)
                    call_id = call.get("id") or f"invalid-{index}"
                    name = call.get("name")
                    signature = self._signature(call)
                    if call_id in self._calls:
                        cached = self._calls[call_id]
                        if cached[0] == signature:
                            return cached[1]
                        result = {
                            "id": call_id,
                            "ok": False,
                            "error": "call ID reused with different arguments; skipped",
                        }
                        result["text"] = render(name, result, self.limits)
                        self._record_result(result)
                        return result
                    # Subagents have their own parallel window, not a file-operation slot.
                    slot = contextlib.nullcontext() if name == "spawn_agent" else semaphore
                    async with slot:
                        started = time.monotonic()
                        try:
                            if call.get("error"):
                                raise ValueError(call["error"])
                            if index >= self.limits.batch_calls:
                                raise ValueError(
                                    f"more than {self.limits.batch_calls} tool calls in one response; skipped"
                                )
                            payload = await self._run(
                                name,
                                call.get("arguments"),
                                call_id,
                                any(not r["ok"] for r in earlier),
                            )
                            result = {"id": call_id, "ok": payload.pop("ok", True), **payload}
                        except asyncio.CancelledError:
                            result = {
                                "id": call_id,
                                "ok": False,
                                "error": "cancelled; operation stopped",
                            }
                            self._calls[call_id] = (signature, result)
                            self._record_result(result)
                            self._save(call, result, started)
                            raise
                        except Exception as exc:
                            result = {
                                "id": call_id,
                                "ok": False,
                                "error": str(exc) or type(exc).__name__,
                            }
                            if name in RESEARCH_TOOLS:
                                result["calls_left"] = self.calls_left()
                        self._record_result(result)
                        self._save(call, result, started)
                        result["text"] = render(name, result, self.limits)
                        self._calls[call_id] = (signature, result)
                        return result

                tasks.append(asyncio.create_task(execute()))
            try:
                return list(await asyncio.gather(*tasks))
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for index, call in enumerate(calls):
                    call_id = call.get("id") or f"invalid-{index}"
                    if call_id not in self._calls:
                        result = {
                            "id": call_id,
                            "ok": False,
                            "error": "cancelled before execution; skipped",
                        }
                        self._calls[call_id] = (self._signature(call), result)
                        self._record_result(result)
                        self._save(call, result, time.monotonic())
                raise
