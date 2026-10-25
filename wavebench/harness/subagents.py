"""Bounded, parallel subagents that share a lead agent's workspace and phase limits.

The lead model delegates with ``spawn_agent``. Each subagent is a fresh
conversation of the configured model with the same file tools, no submission, and no
nesting. Its requests count toward the lead's usage and run within the lead's
phase time. Only a bounded report and the files it changed return to the lead.
"""

from __future__ import annotations

import asyncio
import json
import time

from wavebench.prompt_cache import CachePolicy
from wavebench.tokens import PromptEstimate, context_usage, prompt_tokens

from .commands import FILE_CHANGES, SUBAGENT_EFFORTS, Dispatcher
from .config import Limits
from .failure import failure_record
from .transport import GEMINI_PROVIDER_ROUTES, RecoveryBudget, TurnError, wait_for_recovery
from .workspace import safe_name

MAX_NAME_CHARS = 40
MAX_TASK_CHARS = 24_000
REMINDER_WRITES = 2
MAX_RECOVERIES = 2
FINAL_NOTICE = (
    "[WaveBench] Your working requests are exhausted. This extra request is for your "
    "handoff only: call finish_task now. Report incomplete or blocked with remaining_work "
    "if anything is unfinished. Only finish_task will run; no more file operations."
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
        "Subagents: spawn_agent starts a fresh instance of the configured subagent model in this workspace; up "
        f"to {parallel} run at once and {cap} in total. Unless the project fits in one or two "
        "small files, delegate: decide the file layout and shared interfaces (entry file, module "
        "APIs, data formats, names), write that scaffolding yourself, spawn one agent per "
        "independent file or module in the same response so they run in parallel, then review "
        "their task_outcome, remaining_work, tool_errors and verification, integrate, lint, "
        "and submit. A returned report is not proof of success. Resolve incomplete, blocked, "
        "unknown, or failed tasks yourself or reassign a smaller task; do not silently treat "
        "them as done. Keep assignments small enough to deliver a working module, then add "
        "detail. Each brief must be complete, because an "
        "agent sees nothing else: objective, the files it owns, interfaces to follow, and the "
        "report you need."
    )


def subagent_prompt(auto_install: str, web_search: bool, limits: Limits, *, read_only: bool) -> str:
    from . import session  # The session module imports this one; resolve lazily.

    return (
        "You are a subagent working for a lead agent who integrates, lints, and submits the "
        "project. Complete only the task below, then report. "
        + (
            "You are read-only: use read_file, search_files, list_files, and lint; do not change files. "
            if read_only
            else "Change only the files your task assigns to you; other agents may be editing "
            "other files at the same time. Call independent tools together in one response. "
        )
        + "You cannot submit the project or spawn agents. "
        + session.dependency_notice(auto_install)
        + (" " + session.research_notice() if web_search else "")
        + f" Limits: {limits.subagent_turns} working model requests and {limits.subagent_seconds} seconds "
        "of active time, plus a report-only request and one correction attempt if needed within "
        "that same time limit. Remaining "
        "budget is shown each turn. Use search_files to locate interfaces instead of paging "
        "through entire files; batch independent reads. Build a complete minimal implementation "
        "early, then refine and lint. Finish by calling finish_task with outcome completed, "
        "incomplete, or blocked, a concise report, remaining_work (a list of unfinished parts "
        "of YOUR assigned task; [] if none), and verification describing "
        "checks actually run (or 'not run'). Completed is your claim, not independent verification. "
        "Never claim completed with unfinished work. The lead sees only that report and the "
        "structured outcome, remaining work, verification, changed files, and tool errors."
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
    """Per-model admission: total cap and parallel window."""

    def __init__(self, session, limits: Limits):
        self.session = session
        self.limits = limits
        self.parallel = limits.subagent_parallel
        self.cap = limits.subagent_cap
        self.semaphore = asyncio.Semaphore(limits.subagent_parallel)
        self.runs: list[SubagentRun] = []
        self.rejected = 0
        self.lead_writes = 0
        self.reminded = False
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
            "reported_complete": statuses.count("reported_complete"),
            "incomplete": statuses.count("incomplete"),
            "blocked": statuses.count("blocked"),
            "unknown": statuses.count("unknown"),
            "failed": sum(
                status in {"turn_limit", "time_limit", "failed", "cancelled"} for status in statuses
            ),
            "rejected": self.rejected,
            "reminded": self.reminded,
        }

    def record(self) -> dict:
        session = self.session
        return {
            **self.usage(),
            "model_id": session.subagent_model,
            "turn_limit": self.limits.subagent_turns,
            "report_turns": 2,
            "time_limit_s": self.limits.subagent_seconds,
            "report_chars": self.limits.subagent_report_chars,
            "usage": session.usage([t for t in session.turns if t["phase"] == "subagent"]),
            "runs": [run.record() for run in self.runs],
        }

    def reminder(self, calls: list[dict], results: list[dict]) -> str | None:
        """One nudge once a lead has written files itself without delegating anything."""
        if self.reminded or self.runs or not self.available():
            return None
        self.lead_writes += sum(
            1
            for call, result in zip(calls, results, strict=True)
            if call.get("name") == "write_file" and result.get("ok")
        )
        if self.lead_writes < REMINDER_WRITES:
            return None
        self.reminded = True
        return (
            f"[WaveBench subagents] You have written {self.lead_writes} files yourself and "
            f"spawned no agents. Up to {self.parallel} subagents can run in parallel "
            f"({self.remaining} left in total). If independent files or modules remain, spawn "
            "one agent per file now, each with a complete brief (objective, owned files, "
            "interfaces, constraints, report); otherwise continue and submit."
        )

    def note_progress(self, number: int, chars: int | None) -> None:
        if chars is None:
            self._chars.pop(number, None)
        else:
            self._chars[number] = chars
        tracker = self.session.tracker
        if tracker and tracker.is_running:
            tracker.update(self.session.name, sum(self._chars.values()))

    @staticmethod
    def validate(arguments) -> tuple[str, str, bool, str | None]:
        if not isinstance(arguments, dict) or set(arguments) - {
            "name",
            "task",
            "read_only",
            "reasoning_effort",
        }:
            raise ValueError("spawn_agent accepts only name, task, read_only, and reasoning_effort")
        name, task = arguments.get("name"), arguments.get("task")
        # Some providers send every optional schema field; null means unset.
        read_only = arguments.get("read_only")
        if read_only is None:
            read_only = False
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS:
            raise ValueError(f"name must be a label of 1-{MAX_NAME_CHARS} characters")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be a complete brief for the agent")
        if len(task) > MAX_TASK_CHARS:
            raise ValueError(f"task exceeds {MAX_TASK_CHARS:,} characters")
        if type(read_only) is not bool:
            raise ValueError("read_only must be a boolean")
        effort = arguments.get("reasoning_effort")
        if effort is not None and (not isinstance(effort, str) or effort not in SUBAGENT_EFFORTS):
            raise ValueError("reasoning_effort must be one of: " + ", ".join(SUBAGENT_EFFORTS))
        return name.strip(), task, read_only, effort

    async def spawn(self, call_id: str, arguments) -> dict:
        session = self.session
        try:
            name, task, read_only, effort = self.validate(arguments)
            if session.dispatcher.submission is not None:
                raise ValueError("phase already submitted; skipped")
            if session.finishing:
                raise ValueError("the phase is finishing; complete and submit the project yourself")
            if self.remaining <= 0:
                raise ValueError(
                    f"agent cap reached ({self.cap} per model); continue the work yourself"
                )
        except ValueError:
            self.rejected += 1
            raise
        run = SubagentRun(self, len(self.runs) + 1, name, task, read_only, call_id, effort)
        self.runs.append(run)
        if session.phase_name != "delegating":
            session.phase("delegating")
        run.publish(label=run.label, status="waiting", max_turns=self.limits.subagent_turns)
        session.on_tool_result(session.dispatcher.tool_usage)
        started = time.monotonic()
        try:
            async with self.semaphore:
                await run.run()
        except asyncio.CancelledError as exc:
            if run.started is None:
                run.status, run.stop_reason = "cancelled", "cancelled"
                run.error = "cancelled while waiting for a worker slot"
                run.failure = failure_record(exc, phase="subagent")
                run.publish(status=run.status, error=run.error)
                run.save()
            raise
        finally:
            self.seconds += time.monotonic() - started
            session.on_tool_result(session.dispatcher.tool_usage)
            session.save()
        return run.result_for_lead()


class SubagentRun:
    """One bounded conversation; its turns are charged to the lead's session."""

    def __init__(
        self,
        pool: SubagentPool,
        number: int,
        name: str,
        task: str,
        read_only: bool,
        call_id: str,
        reasoning_effort: str | None = None,
    ):
        session = pool.session
        self.pool, self.session, self.limits = pool, session, pool.limits
        self.model_id = session.subagent_model
        self.reasoning_effort = (
            session.reasoning_effort if reasoning_effort is None else reasoning_effort
        )
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
            lambda phase: self.publish(status=phase),
            session.on_tool_result,
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
        self.task_outcome = "unknown"
        self.stop_reason: str | None = None
        self.remaining_work: list[str] = []
        self.verification = "not reported"
        self.tool_errors: list[dict] = []
        self.report_requests = 0
        self.estimate = PromptEstimate()
        self.cache_policy = CachePolicy(self.model_id)
        self.gemini_provider: str | None = None
        self.api_seconds = 0.0
        self.retry_seconds = 0.0
        self.tool_seconds = 0.0
        self.output_tokens = 0
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
            self.publish(status="streaming", output_tokens=self._turn_output_tokens)

    def publish(self, **fields) -> None:
        """Feed this agent's live row in the lead's delegation HUD."""
        tracker = self.session.tracker
        if tracker and tracker.is_running:
            tracker.update_subagent(self.session.name, self.number, **fields)

    def on_diagnostics(self, diagnostics: dict) -> None:
        self._stream_diagnostics = diagnostics

    async def request(self, tools: list[dict], max_tokens: int, input_bound: int):
        from . import session as controller

        session = self.session
        live = session.tracker is not None and session.tracker.is_running
        return await controller.call_conversation(
            session.client,
            session.api_key,
            self.model_id,
            self.messages,
            tools,
            max_tokens=max_tokens,
            input_tokens_bound=input_bound,
            stream_limits=self.limits,
            cache_policy=self.cache_policy,
            gemini_provider=self.gemini_provider,
            reasoning_effort=self.reasoning_effort,
            fast_providers=session.fast_providers,
            on_progress=(lambda chars: self.pool.note_progress(self.number, chars))
            if live
            else None,
            on_retry=session.on_retry,
            on_usage=self.on_usage,
            on_diagnostics=self.on_diagnostics,
        )

    def account(self, record: dict) -> None:
        session = self.session
        session.turns.append(record)
        self.turns.append(record)
        completion = (record.get("usage") or {}).get("completion_tokens")
        self.output_tokens += (
            completion if type(completion) is int and completion >= 0 else self._turn_output_tokens
        )

    def publish_usage(self) -> None:
        session, tracker = self.session, self.session.tracker
        if tracker and tracker.is_running:
            tracker.update_harness(session.name, session.usage(), session.api_seconds)
            self.publish(settled_tokens=self.output_tokens, output_tokens=0)

    def note_files(self, calls: list[dict], results: list[dict]) -> None:
        kinds = {"write_file": "written", "edit_file": "edited", "delete_file": "deleted"}
        for call, result in zip(calls, results, strict=True):
            if call.get("name") not in FILE_CHANGES or not result.get("ok"):
                continue
            arguments = call.get("arguments")
            path = arguments.get("path") if isinstance(arguments, dict) else None
            if isinstance(path, str):
                self.files[kinds[call["name"]]].add(path)

    async def run(self) -> None:
        session, limits = self.session, self.limits
        self.status = "running"
        self.started, self.started_at = time.monotonic(), time.time()
        deadline = self.started + limits.subagent_seconds
        max_turns = limits.subagent_turns
        recoveries = RecoveryBudget(MAX_RECOVERIES)
        try:
            read_turns = 0
            unstructured_reports = 0
            for turn_index in range(max_turns + 2):
                if time.monotonic() >= deadline:
                    raise asyncio.TimeoutError
                final = turn_index >= max_turns
                if final:
                    self.messages.append({"role": "user", "content": FINAL_NOTICE})
                    self.report_requests += 1
                else:
                    remaining = max_turns - turn_index
                    notice = (
                        f"[WaveBench budget] {remaining} working requests left including this one; "
                        f"{max(0, int(deadline - time.monotonic()))} seconds left. "
                    )
                    if remaining <= 5:
                        notice += "Finish the working implementation and checks now; report anything unfinished explicitly. "
                    if read_turns >= 5 and not self.read_only:
                        notice += "You have made no file changes in five working turns. Use search_files for targeted discovery and write a minimal working implementation now."
                        read_turns = 0
                    self.messages.append({"role": "user", "content": notice})
                tools = self.dispatcher.available_tools()
                if final:
                    tools = [t for t in tools if t["function"]["name"] == "finish_task"]
                local_input = await asyncio.to_thread(prompt_tokens, self.messages, tools)
                input_bound = self.estimate.bound(local_input)
                request_output = session.output_tokens(self.model_id)
                started = time.monotonic()
                turn = None
                plan = None
                record = {
                    "phase": "subagent",
                    "agent": self.number,
                    "reasoning_effort": self.reasoning_effort,
                    "input_tokens_bound": input_bound,
                    "max_output_tokens": request_output,
                }
                try:
                    async with session.api_slots:
                        self._turn_usage, self._turn_output_tokens = {}, 0
                        self._stream_diagnostics = {}
                        self.publish(status="thinking", turn=turn_index + 1, output_tokens=0)
                        turn = await asyncio.wait_for(
                            self.request(tools, request_output, input_bound),
                            max(0.001, deadline - time.monotonic()),
                        )
                    self.gemini_provider = bind_gemini_provider(
                        self.model_id, self.gemini_provider, turn
                    )
                    record.update(
                        usage=turn.usage,
                        model=turn.model,
                        provider=turn.provider,
                        finish_reason=turn.finish_reason,
                        adjustments=turn.adjustments,
                    )
                    self.account(record)
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
                        self.account(record)
                    plan = recoveries.next(exc) if not final else None
                    if plan is None:
                        raise
                    if plan.notice:
                        self.messages.append({"role": "user", "content": plan.notice})
                    recovery_record = {
                        "kind": plan.kind,
                        "phase": "subagent",
                        "agent": self.number,
                        "turn": len(self.turns),
                        "failure_code": exc.failure_code,
                    }
                    if plan.kind == "provider_retry":
                        recovery_record.update(
                            attempt=plan.attempt,
                            wait_s=max(0, min(plan.wait_s, deadline - time.monotonic())),
                        )
                    session.recoveries.append(recovery_record)
                finally:
                    elapsed = time.monotonic() - started
                    self.api_seconds += elapsed
                    session.api_seconds += elapsed
                    self.publish_usage()
                if plan:
                    if plan.kind == "provider_retry":
                        self.publish(status="retrying")
                        self.save()
                        session.save()
                        started = time.monotonic()
                        try:
                            await wait_for_recovery(plan.wait_s, deadline - started)
                        finally:
                            elapsed = time.monotonic() - started
                            self.retry_seconds += elapsed
                            session.retry_seconds += elapsed
                    continue
                recoveries.succeeded()
                explicit_cache = (turn.adjustments.get("cache") or {}).get("breakpoints")
                measured = context_usage(
                    turn.usage, self.cache_policy.family if explicit_cache else "automatic"
                )
                self.estimate.observe(local_input, measured)
                self.messages.append(turn.message)
                calls = turn.message.get("tool_calls") or []
                if not calls:
                    self.report = turn.message.get("content") or ""
                    unstructured_reports += 1
                    if final and self.report_requests == 2:
                        self.stop_reason = "turn_limit"
                        self.status = "turn_limit"
                        self.error = (
                            "working request limit reached without an explicit task outcome"
                        )
                        return
                    if unstructured_reports >= 2 and not final:
                        self.status, self.stop_reason = "unknown", "invalid_report"
                        self.error = (
                            "worker ended without an explicit task outcome after a reminder"
                        )
                        return
                    self.messages.append(
                        {
                            "role": "user",
                            "content": "[WaveBench] Plain text does not establish task completion. Call finish_task with your actual outcome, remaining_work and verification; use incomplete if you cannot finish.",
                        }
                    )
                    continue
                if final and any(c["function"]["name"] != "finish_task" for c in calls):
                    self.report = turn.message.get("content") or ""
                    self.pending_calls = len(calls)
                    self.status = "turn_limit"
                    self.stop_reason = "turn_limit"
                    self.error = (
                        f"{max_turns} working requests used; {len(calls)} pending tool call(s) "
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
                self.publish(status="tools")
                try:
                    results = await asyncio.wait_for(
                        self.dispatcher.batch(native), max(0.001, deadline - time.monotonic())
                    )
                finally:
                    self.tool_seconds += time.monotonic() - started
                    self.publish(**self.tool_counts())
                self.note_files(native, results)
                changed = any(
                    c["name"] in FILE_CHANGES and r["ok"]
                    for c, r in zip(native, results, strict=True)
                )
                read_turns = 0 if changed else read_turns + 1
                for call, result in zip(native, results, strict=True):
                    if not result["ok"]:
                        self.tool_errors.append(
                            {
                                "tool": call["name"],
                                "error": (
                                    result.get("error")
                                    or result.get("diagnostics")
                                    or result["text"]
                                )[:500],
                            }
                        )
                self.tool_errors = self.tool_errors[-8:]
                self.messages.extend(
                    {"role": "tool", "tool_call_id": result["id"], "content": result["text"]}
                    for result in results
                )
                # Preserve a useful draft even if the outcome declaration was
                # malformed. It must never promote task_outcome to completed.
                for call in native:
                    if call["name"] == "finish_task" and isinstance(call["arguments"], dict):
                        draft = call["arguments"].get("report")
                        if isinstance(draft, str):
                            self.report = draft[:24_000]
                if self.dispatcher.handoff is not None:
                    handoff = self.dispatcher.handoff
                    self.task_outcome = handoff["outcome"]
                    self.report = handoff["report"]
                    self.remaining_work = handoff["remaining_work"]
                    self.verification = handoff["verification"]
                    self.status = (
                        "reported_complete"
                        if self.task_outcome == "completed"
                        else self.task_outcome
                    )
                    self.stop_reason = (
                        "turn_limit" if final and self.task_outcome != "completed" else "reported"
                    )
                    if self.task_outcome != "completed":
                        self.error = f"Worker reported {self.task_outcome}: " + "; ".join(
                            self.remaining_work
                        )
                    return
                self.save()
            self.status = "turn_limit"
            self.stop_reason = "turn_limit"
            self.error = "working request limit reached; final handoff was invalid"
        except asyncio.TimeoutError:
            self.status = "time_limit"
            self.stop_reason = "time_limit"
            self.error = f"subagent active time limit reached ({limits.subagent_seconds}s)"
        except asyncio.CancelledError as exc:
            self.status = "cancelled"
            self.stop_reason = "cancelled"
            self.error = "cancelled"
            self.failure = failure_record(exc, phase="subagent")
            raise
        except Exception as exc:
            self.status = "failed"
            self.stop_reason = "error"
            self.error = str(exc) or type(exc).__name__
            self.failure = failure_record(exc, phase="subagent")
        finally:
            # A deadline can cancel a batch after some tools have settled. Those
            # failures still belong in the handoff, even when batch() never returns.
            self.tool_errors = [
                {
                    "tool": json.loads(signature)[0],
                    "error": (
                        result.get("error")
                        or result.get("diagnostics")
                        or result.get("text")
                        or "tool failed"
                    )[:500],
                }
                for signature, result in self.dispatcher._calls.values()
                if not result["ok"]
            ][-8:]
            for signature, result in self.dispatcher._calls.values():
                name, arguments = json.loads(signature)
                self.note_files([{"name": name, "arguments": arguments}], [result])
            if self.error and self.failure is None:
                self.failure = {
                    "category": "harness_limit"
                    if self.stop_reason in {"turn_limit", "time_limit"}
                    else "task_outcome",
                    "code": "subagent_" + self.status,
                    "summary": self.error,
                    "phase": "subagent",
                }
            if self.error:
                self.messages.append(
                    {"role": "user", "content": "[WaveBench outcome] " + self.error}
                )
            self.finished, self.finished_at = time.monotonic(), time.time()
            self.pool.note_progress(self.number, None)
            self.publish(
                status=self.status,
                finished=self.finished,
                error=self.error,
                settled_tokens=self.output_tokens,
                output_tokens=0,
                **self.tool_counts(),
            )
            self.save()

    def tool_counts(self) -> dict:
        usage = self.dispatcher.tool_usage
        return {"tool_calls": usage["calls"], "tool_failures": usage["failures"]}

    def result_for_lead(self) -> dict:
        """The only subagent output the lead sees: bounded report, files, status, usage."""
        usage = self.session.usage(self.turns)
        cap = self.limits.subagent_report_chars
        result = {
            "ok": self.status == "reported_complete",
            "agent": self.label,
            "status": self.status,
            "task_outcome": self.task_outcome,
            "stop_reason": self.stop_reason,
            "remaining_work": self.remaining_work,
            "verification": self.verification,
            "failure": self.failure,
            "tool_errors": self.tool_errors,
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
            "task_outcome": self.task_outcome,
            "stop_reason": self.stop_reason,
            "remaining_work": self.remaining_work,
            "verification": self.verification,
            "tool_errors": self.tool_errors,
            "report_requests": self.report_requests,
            "error": self.error,
            "failure": self.failure,
            "turns": len(self.turns),
            "pending_calls": self.pending_calls,
            "tool_usage": self.dispatcher.tool_usage.copy(),
            "files": {key: sorted(paths) for key, paths in self.files.items()},
            "model_id": self.model_id,
            "reasoning_effort": self.reasoning_effort,
            "report_chars": len(self.report),
            "usage": self.session.usage(self.turns),
            "lint": self.dispatcher.lint_results,
            "timing": {
                "time_s": self.elapsed,
                "api_s": self.api_seconds,
                "retry_s": self.retry_seconds,
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
