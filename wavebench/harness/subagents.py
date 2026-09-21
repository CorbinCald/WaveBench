"""Bounded, parallel subagents that share a lead agent's workspace and budget.

The lead model delegates with ``spawn_agent``. Each subagent is a fresh
conversation of the same model with the same file tools, no submission, and no
nesting. Its requests are charged to the lead's total token budget and phase
time. Only a bounded report and the files it changed return to the lead.
"""

from __future__ import annotations

import asyncio
import json
import time

from wavebench import api
from wavebench.prompt_cache import CachePolicy
from wavebench.tokens import PromptEstimate, context_usage, prompt_tokens

from .accounting import reported_total
from .budget import finish_output_tokens, finish_reserve
from .commands import Dispatcher
from .config import Limits
from .failure import failure_record
from .transport import GEMINI_PROVIDER_ROUTES, TurnError
from .workspace import safe_name

MAX_NAME_CHARS = 40
MAX_TASK_CHARS = 24_000
MIN_REQUEST_OUTPUT = 1_024
FINAL_NOTICE = (
    "[WaveBench] This is your final model request. Reply now with your report as plain "
    "text and no tool calls; further tool calls will not run."
)


def subagents_status(config: dict) -> str:
    if config.get("subagents", "off") != "on":
        return "Off"
    harness = config.get("harness") or {}
    defaults = Limits()
    parallel = harness.get("subagent_parallel", defaults.subagent_parallel)
    cap = harness.get("subagent_cap", defaults.subagent_cap)
    return f"On ({parallel} parallel, {cap} total)"


def lead_instructions(parallel: int, cap: int) -> str:
    return (
        " Subagents: spawn_agent delegates one self-contained task to a subagent of your own "
        "model that shares this workspace, tools, and budget; up to "
        f"{parallel} run at once and {cap} in total. Delegate independent, well-specified parts "
        "(separate modules, pages, or research) by calling spawn_agent several times in one "
        "turn; do small or tightly coupled work yourself. Each agent starts with an empty "
        "context, so brief it completely: objective, the files it owns, interfaces to follow, "
        "constraints, and the report you need. Give parallel agents disjoint files, review their "
        "work before integrating, and submit only with your own wb done."
    )


def subagent_prompt(auto_install: str, web_search: bool, limits: Limits, *, read_only: bool) -> str:
    from . import session  # The session module imports this one; resolve lazily.

    return (
        "You are a subagent inside a WaveBench project workspace, working for a lead agent that "
        "integrates, lints, and submits the project. Complete only the task below, then report. "
        + (
            "This agent is read-only: use wb ls, read, and lint; do not write, edit, or delete. "
            if read_only
            else "Use wb file tools on the shared workspace and batch independent operations. "
            "Change only the files your task assigns to you; other agents may be editing other "
            "files at the same time. "
        )
        + "You cannot submit the project or spawn agents. "
        + session.dependency_notice(auto_install)
        + (session.research_notice() if web_search else "")
        + f" Limits: at most {limits.subagent_turns} model requests and "
        f"{limits.subagent_seconds} active seconds; a reminder arrives before your final "
        "request. Finish with a plain-text reply and no tool calls: a concise report of what "
        "you did or found, the files you changed, interfaces the lead must know, lint results, "
        "and anything unfinished or uncertain. The lead sees only that report."
    )


def bind_gemini_provider(model_id: str, current: str | None, turn) -> str | None:
    if not model_id.lower().lstrip("~").startswith("google/gemini-"):
        return current
    if turn.provider not in GEMINI_PROVIDER_ROUTES:
        raise TurnError(
            "Gemini provider identity unavailable; no tools executed",
            turn.usage,
            failure_code="provider_identity_missing",
        )
    if current is not None and current != turn.provider:
        raise TurnError(
            "Gemini provider changed despite routing restriction; no tools executed",
            turn.usage,
            failure_code="provider_changed",
        )
    return turn.provider


class SubagentPool:
    """Per-model admission: total cap, parallel window, and shared-budget reservations."""

    def __init__(self, session, limits: Limits):
        self.session = session
        self.limits = limits
        self.parallel = limits.subagent_parallel
        self.cap = limits.subagent_cap
        self.semaphore = asyncio.Semaphore(limits.subagent_parallel)
        self.runs: list[SubagentRun] = []
        self.rejected = 0
        self.reserved_tokens = 0
        self.seconds = 0.0
        self._chars: dict[int, int] = {}

    @property
    def remaining(self) -> int:
        return max(0, self.cap - len(self.runs))

    def available(self) -> bool:
        return self.remaining > 0 and not self.session.finishing

    def usage(self) -> dict:
        statuses = [run.status for run in self.runs]
        return {
            "enabled": True,
            "parallel": self.parallel,
            "cap": self.cap,
            "spawned": len(self.runs),
            "active": sum(status in {"pending", "running"} for status in statuses),
            "completed": statuses.count("completed"),
            "failed": sum(status not in {"pending", "running", "completed"} for status in statuses),
            "rejected": self.rejected,
        }

    def record(self) -> dict:
        session = self.session
        return {
            **self.usage(),
            "turn_limit": self.limits.subagent_turns,
            "time_limit_s": self.limits.subagent_seconds,
            "report_chars": self.limits.subagent_report_chars,
            "usage": session.usage([t for t in session.turns if t["phase"] == "subagent"]),
            "runs": [run.record() for run in self.runs],
        }

    def affordable(self) -> int:
        """Tokens one subagent request may use while the lead keeps its finishing reserve."""
        session, limits = self.session, self.limits
        pending = sum(run.status in {"pending", "running"} for run in self.runs)
        growth = pending * PromptEstimate().bound(limits.subagent_report_chars // 3 + 512)
        output = finish_output_tokens(
            min(
                limits.turn_tokens,
                api._MODEL_MAX_COMPLETION_CACHE.get(session.model_id, limits.turn_tokens),
            )
        )
        reserve = finish_reserve(
            (session._next_input_tokens or 0) + growth, output, limits.output_chars
        )
        return limits.total_tokens - session.budget_tokens - self.reserved_tokens - reserve

    def note_progress(self, number: int, chars: int | None) -> None:
        if chars is None:
            self._chars.pop(number, None)
        else:
            self._chars[number] = chars
        tracker = self.session.tracker
        if tracker and tracker.is_running:
            tracker.update(self.session.name, sum(self._chars.values()))

    @staticmethod
    def validate(arguments) -> tuple[str, str, bool]:
        if not isinstance(arguments, dict) or set(arguments) - {"name", "task", "read_only"}:
            raise ValueError("spawn_agent accepts only name, task, and read_only")
        name, task = arguments.get("name"), arguments.get("task")
        read_only = arguments.get("read_only", False)
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS:
            raise ValueError(f"name must be a label of 1-{MAX_NAME_CHARS} characters")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be a complete brief for the agent")
        if len(task) > MAX_TASK_CHARS:
            raise ValueError(f"task exceeds {MAX_TASK_CHARS:,} characters")
        if type(read_only) is not bool:
            raise ValueError("read_only must be a boolean")
        return name.strip(), task, read_only

    async def spawn(self, call_id: str, arguments) -> dict:
        session = self.session
        try:
            name, task, read_only = self.validate(arguments)
            if session.dispatcher.submission is not None:
                raise ValueError("phase already submitted; skipped")
            if session.finishing:
                raise ValueError(
                    "the finishing reserve is active; finish and submit the project yourself"
                )
            if self.remaining <= 0:
                raise ValueError(
                    f"agent cap reached ({self.cap} per model); continue the work yourself"
                )
        except ValueError:
            self.rejected += 1
            raise
        run = SubagentRun(self, len(self.runs) + 1, name, task, read_only, call_id)
        self.runs.append(run)
        if session.phase_name != "delegating":
            session.phase("delegating")
        started = time.monotonic()
        try:
            async with self.semaphore:
                await run.run()
        finally:
            self.seconds += time.monotonic() - started
            session.save()
        return run.result_for_lead()


class SubagentRun:
    """One bounded conversation; its turns are charged to the lead's session."""

    def __init__(
        self, pool: SubagentPool, number: int, name: str, task: str, read_only: bool, call_id: str
    ):
        session = pool.session
        self.pool, self.session, self.limits = pool, session, pool.limits
        self.number, self.name, self.task = number, name, task
        self.read_only, self.call_id = read_only, call_id
        self.label = f"{number:02d}-{safe_name(name)}"
        self.metadata = session.metadata / "subagents" / self.label
        self.metadata.mkdir(parents=True, exist_ok=True)
        self.dispatcher = Dispatcher(
            session.workspace,
            session.runtime,
            self.metadata,
            self.limits,
            on_tool_result=session.on_tool_result,
            parent=session.dispatcher,
            read_only=read_only,
        )
        self.messages = [
            {
                "role": "system",
                "content": subagent_prompt(
                    session.auto_install,
                    session.dispatcher.web_search is not None,
                    self.limits,
                    read_only=read_only,
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Overall project request (context from the user):\n{session.prompt}\n\n"
                    f"Your task from the lead agent (agent {number}, {name}):\n{task}"
                ),
            },
        ]
        self.turns: list[dict] = []
        self.status = "pending"
        self.report = ""
        self.error: str | None = None
        self.failure: dict | None = None
        self.files: dict[str, set[str]] = {"written": set(), "edited": set(), "deleted": set()}
        self.pending_calls = 0
        self.estimate = PromptEstimate()
        self.cache_policy = CachePolicy(session.model_id)
        self.gemini_provider: str | None = None
        self.api_seconds = 0.0
        self.tool_seconds = 0.0
        self.started: float | None = None
        self.finished: float | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._turn_usage: dict = {}
        self._turn_output_tokens = 0
        self._stream_diagnostics: dict = {}

    @property
    def elapsed(self) -> float:
        if self.started is None:
            return 0.0
        return (self.finished or time.monotonic()) - self.started

    def on_usage(self, usage: dict, output_tokens: int) -> None:
        self._turn_usage = usage
        delta = output_tokens - self._turn_output_tokens
        self._turn_output_tokens = max(self._turn_output_tokens, output_tokens)
        tracker = self.session.tracker
        if delta > 0 and tracker and tracker.is_running:
            tracker.note_harness_output(self.session.name, delta)

    def on_diagnostics(self, diagnostics: dict) -> None:
        self._stream_diagnostics = diagnostics

    async def request(self, tools: list[dict], max_tokens: int, input_bound: int):
        from . import session as controller

        session = self.session
        live = session.tracker is not None and session.tracker.is_running
        return await controller.call_conversation(
            session.client,
            session.api_key,
            session.model_id,
            self.messages,
            tools,
            max_tokens=max_tokens,
            input_tokens_bound=input_bound,
            stream_limits=self.limits,
            cache_policy=self.cache_policy,
            gemini_provider=self.gemini_provider,
            reasoning_effort=session.reasoning_effort,
            on_progress=(lambda chars: self.pool.note_progress(self.number, chars))
            if live
            else None,
            on_retry=session.on_retry,
            on_usage=self.on_usage,
            on_diagnostics=self.on_diagnostics,
        )

    def account(self, record: dict, input_bound: int, message: dict | None) -> None:
        charged = reported_total(record["usage"])
        if charged is None:
            charged = input_bound + max(
                prompt_tokens([message], []) if message else 0, self._turn_output_tokens
            )
        session = self.session
        session.turns.append(record)
        self.turns.append(record)
        session.budget_tokens += charged

    def publish(self) -> None:
        session, tracker = self.session, self.session.tracker
        if tracker and tracker.is_running:
            tracker.update_harness(
                session.name, session.usage(), session.api_seconds, budget=session.budget_record()
            )

    def note_files(self, calls: list[dict], results: list[dict]) -> None:
        kinds = {"write": "written", "edit": "edited", "delete": "deleted"}
        for call, result in zip(calls, results, strict=True):
            command = call.get("arguments") or {}
            if call.get("name", "wb") != "wb" or not result.get("ok"):
                continue
            verb, path = command.get("command"), command.get("path")
            if verb in kinds and isinstance(path, str):
                self.files[kinds[verb]].add(path)

    async def run(self) -> None:
        session, limits = self.session, self.limits
        self.status = "running"
        self.started, self.started_at = time.monotonic(), time.time()
        deadline = self.started + limits.subagent_seconds
        max_turns = limits.subagent_turns
        try:
            for turn_index in range(max_turns):
                final = turn_index + 1 == max_turns
                if final:
                    self.messages.append({"role": "user", "content": FINAL_NOTICE})
                tools = self.dispatcher.available_tools()
                local_input = await asyncio.to_thread(prompt_tokens, self.messages, tools)
                input_bound = self.estimate.bound(local_input)
                output_tokens = min(
                    limits.turn_tokens,
                    api._MODEL_MAX_COMPLETION_CACHE.get(session.model_id, limits.turn_tokens),
                )
                if final:
                    output_tokens = finish_output_tokens(output_tokens)
                remaining = self.pool.affordable() - input_bound
                if remaining < MIN_REQUEST_OUTPUT:
                    self.status = "budget_exhausted"
                    self.error = (
                        "shared token budget cannot fit another subagent request while keeping "
                        f"the lead's finishing reserve ({session.budget_tokens:,} / "
                        f"{limits.total_tokens:,} tokens used; next input estimate {input_bound:,})"
                    )
                    return
                request_output = min(output_tokens, remaining)
                reservation = input_bound + request_output
                self.pool.reserved_tokens += reservation
                started = time.monotonic()
                turn = None
                record = {
                    "phase": "subagent",
                    "agent": self.number,
                    "input_tokens_bound": input_bound,
                    "max_output_tokens": request_output,
                }
                try:
                    async with session.api_slots:
                        self._turn_usage, self._turn_output_tokens = {}, 0
                        self._stream_diagnostics = {}
                        turn = await asyncio.wait_for(
                            self.request(tools, request_output, input_bound),
                            max(0.001, deadline - time.monotonic()),
                        )
                    self.gemini_provider = bind_gemini_provider(
                        session.model_id, self.gemini_provider, turn
                    )
                    record.update(
                        usage=turn.usage,
                        model=turn.model,
                        provider=turn.provider,
                        finish_reason=turn.finish_reason,
                        adjustments=turn.adjustments,
                    )
                    self.account(record, input_bound, turn.message)
                except BaseException as exc:
                    if getattr(exc, "request_sent", True):
                        record.update(
                            usage=getattr(exc, "usage", None) or self._turn_usage,
                            error=str(exc) or type(exc).__name__,
                            failure=failure_record(
                                exc, phase="subagent", stream=self._stream_diagnostics
                            ),
                            stream=getattr(exc, "diagnostics", None) or self._stream_diagnostics,
                        )
                        self.account(record, input_bound, None)
                    raise
                finally:
                    self.pool.reserved_tokens -= reservation
                    elapsed = time.monotonic() - started
                    self.api_seconds += elapsed
                    session.api_seconds += elapsed
                    self.publish()
                explicit_cache = (turn.adjustments.get("cache") or {}).get("breakpoints")
                measured = context_usage(
                    turn.usage, self.cache_policy.family if explicit_cache else "automatic"
                )
                self.estimate.observe(local_input, measured)
                self.messages.append(turn.message)
                if session.budget_tokens > limits.total_tokens:
                    self.status = "budget_exhausted"
                    self.error = (
                        f"total token budget exhausted ({session.budget_tokens:,} / "
                        f"{limits.total_tokens:,} tokens used); tool calls skipped"
                    )
                    return
                calls = turn.message.get("tool_calls") or []
                if not calls:
                    self.report = turn.message.get("content") or ""
                    self.status = "completed"
                    return
                if final:
                    self.report = turn.message.get("content") or ""
                    self.pending_calls = len(calls)
                    self.status = "turn_limit"
                    self.error = (
                        f"{max_turns} model requests used; {len(calls)} pending tool call(s) "
                        "were not run"
                    )
                    return
                native = [
                    {
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "arguments": json.loads(call["function"]["arguments"]),
                    }
                    for call in calls
                ]
                started = time.monotonic()
                try:
                    results = await asyncio.wait_for(
                        self.dispatcher.batch(native), max(0.001, deadline - time.monotonic())
                    )
                finally:
                    self.tool_seconds += time.monotonic() - started
                self.note_files(native, results)
                self.messages.extend(
                    {
                        "role": "tool",
                        "tool_call_id": result["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                    for result in results
                )
                self.save()
            self.status = "turn_limit"
        except asyncio.TimeoutError:
            self.status = "time_limit"
            self.error = f"subagent active time limit reached ({limits.subagent_seconds}s)"
        except asyncio.CancelledError as exc:
            self.status = "cancelled"
            self.error = "cancelled"
            self.failure = failure_record(exc, phase="subagent")
            raise
        except Exception as exc:
            self.status = "failed"
            self.error = str(exc) or type(exc).__name__
            self.failure = failure_record(exc, phase="subagent")
        finally:
            self.finished, self.finished_at = time.monotonic(), time.time()
            self.pool.note_progress(self.number, None)
            self.save()

    def result_for_lead(self) -> dict:
        """The only subagent output the lead sees: bounded report, files, status, usage."""
        usage = self.session.usage(self.turns)
        cap = self.limits.subagent_report_chars
        result = {
            "ok": self.status == "completed",
            "agent": self.label,
            "status": self.status,
            "report": self.report[:cap],
            "files": {key: sorted(paths) for key, paths in self.files.items()},
            "turns": len(self.turns),
            "tool_calls": self.dispatcher.tool_usage["calls"],
            "tool_failures": self.dispatcher.tool_usage["failures"],
            "usage": {key: usage.get(key) for key in ("total_tokens", "cost")},
            "time_s": round(self.elapsed, 1),
            "agents_left": self.pool.remaining,
        }
        if len(self.report) > cap:
            result.update(report_truncated=True, report_chars=len(self.report))
        if self.error:
            result["error"] = self.error
        return result

    def record(self) -> dict:
        return {
            "number": self.number,
            "name": self.name,
            "label": self.label,
            "read_only": self.read_only,
            "call_id": self.call_id,
            "status": self.status,
            "error": self.error,
            "failure": self.failure,
            "turns": len(self.turns),
            "pending_calls": self.pending_calls,
            "tool_usage": self.dispatcher.tool_usage.copy(),
            "files": {key: sorted(paths) for key, paths in self.files.items()},
            "report_chars": len(self.report),
            "usage": self.session.usage(self.turns),
            "lint": self.dispatcher.lint_results,
            "timing": {
                "time_s": self.elapsed,
                "api_s": self.api_seconds,
                "tool_s": self.tool_seconds,
            },
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "diagnostics": str(self.metadata),
        }

    def save(self) -> None:
        temp = self.metadata / "result.tmp"
        temp.write_text(
            json.dumps(
                {**self.record(), "task": self.task, "report": self.report},
                indent=2,
                ensure_ascii=False,
            )
        )
        temp.replace(self.metadata / "result.json")
        (self.metadata / "conversation.json").write_text(
            json.dumps(self.messages, indent=2, ensure_ascii=False)
        )
