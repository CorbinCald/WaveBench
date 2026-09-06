"""Controller-owned build → run → optional repair → retry state machine."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

from wavebench.api import call_model_conversation as call_conversation

from . import HARNESS_VERSION
from .commands import TOOL_SCHEMA, Dispatcher
from .config import Limits
from .runtime import Runtime, SetupError
from .transport import TurnError, capability
from .workspace import allocate_project

_BROWSER_LOCK = threading.Lock()


def open_preview(url: str) -> bool:
    # webbrowser initializes a process-global registry lazily; it is not thread-safe.
    with _BROWSER_LOCK:
        return webbrowser.open(url)


def system_prompt(auto_install: str) -> str:
    dependencies = (
        "PyPI wheels from requirements.txt are installed in isolation."
        if auto_install == "on"
        else "Dependencies are disabled; use runtime standard libraries."
    )
    return (
        "Build the requested project in your workspace. Use wb file tools and lint as needed; "
        "batch independent operations. Submit with done when ready. WaveBench controls execution "
        "and allows one repair after a failed first run. Available: Python 3, Node, static HTML, "
        "and HTTP servers listening on PORT; no GUI or development reloaders. " + dependencies
    )


class BudgetError(RuntimeError):
    pass


class HarnessSession:
    def __init__(
        self,
        run: Path,
        slot: int,
        name: str,
        model_id: str,
        prompt: str,
        client,
        api_key: str,
        limits: Limits,
        api_slots: asyncio.Semaphore,
        process_slots: asyncio.Semaphore,
        *,
        auto_install="off",
        auto_open="incremental",
        reasoning_effort="high",
        tracker=None,
    ):
        self.name, self.model_id = name, model_id
        self.client, self.api_key = client, api_key
        self.limits, self.api_slots, self.process_slots = limits, api_slots, process_slots
        self.auto_open, self.auto_install, self.reasoning_effort = (
            auto_open,
            auto_install,
            reasoning_effort,
        )
        self.workspace, self.metadata = allocate_project(run, slot, name)
        try:
            self.runtime = Runtime(self.workspace, self.metadata, limits, auto_install)
        except BaseException:
            self.workspace.close()
            raise
        self.runtime.process_slots = process_slots
        self.dispatcher = Dispatcher(
            self.workspace, self.runtime, self.metadata, limits, self.phase
        )
        self.tracker = tracker
        self.messages = [
            {"role": "system", "content": system_prompt(auto_install)},
            {"role": "user", "content": prompt},
        ]
        self.turns: list[dict] = []
        self.attempts: list[dict] = []
        self.retries: list[dict] = []
        self.events: list[dict] = []
        self.generation = "pending"
        self.repair = "not_needed"
        self.status = "failed"
        self.error = None
        self.phase_name = "pending"
        self.submitted_at: float | None = None
        self.descriptor = None
        self.preview = None
        self.budget_tokens = 0
        self.api_seconds = 0.0
        self.tool_seconds = 0.0
        self.build_seconds = 0.0
        self.repair_seconds = 0.0
        self.queue_seconds = 0.0
        self.setup_seconds = 0.0
        self.started = time.monotonic()
        self._execution_lock = asyncio.Lock()
        self._execution_started = False
        self._closed = False
        self.tool_capability = None

    def phase(self, phase: str) -> None:
        self.phase_name = phase
        self.events.append({"phase": phase, "timestamp": time.time()})
        if self.tracker and self.tracker.is_running:
            self.tracker.set_phase(self.name, phase)
        else:
            print(f"  {self.name}: {phase}", flush=True)
        self.save()

    def on_retry(self, status, attempt, max_attempts, wait_s):
        self.retries.append(
            {
                "status": status,
                "attempt": attempt,
                "wait_s": wait_s,
                "phase": self.phase_name,
                "turn": len(self.turns) + 1,
            }
        )
        if self.tracker and self.tracker.is_running:
            self.tracker.note_retry(self.name, status, attempt, max_attempts, wait_s)

    def usage(self) -> dict:
        aggregate = {"api_turns": len(self.turns), "usage_complete": bool(self.turns)}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost"):
            values = [turn["usage"].get(key) for turn in self.turns]
            aggregate[key] = (
                sum(values) if values and all(isinstance(v, (int, float)) for v in values) else None
            )
        aggregate["usage_complete"] = all(
            aggregate[key] is not None
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        )
        return aggregate

    def result(self) -> dict:
        active = self.build_seconds + self.repair_seconds
        return {
            "status": self.status,
            "time_s": active,
            "file": str(self.workspace.root.relative_to(self.metadata.parent.parent)),
            "workspace": str(self.workspace.root),
            "entry_point": str(self.workspace.root / self.descriptor["entry"])
            if self.descriptor
            else None,
            "error": self.error,
            "usage": self.usage(),
            "retries": self.retries,
            "harness": {
                "version": HARNESS_VERSION,
                "config": self.limits.record(),
                "auto_open": self.auto_open,
                "dependency_policy": self.auto_install,
                "model_id": self.model_id,
                "tool_capability": self.tool_capability,
                "generation": self.generation,
                "repair": self.repair,
                "phase": self.phase_name,
                "launch": self.descriptor,
                "attempts": self.attempts,
                "lint": self.dispatcher.lint_results,
                "setup": self.runtime.setup_results,
                "turns": self.turns,
                "events": self.events,
                "validation": "runtime/startup only; project quality is not scored",
                "timing": {
                    "generation_s": self.build_seconds,
                    "api_s": self.api_seconds,
                    "tool_s": self.tool_seconds,
                    "queued_s": self.queue_seconds,
                    "repair_s": self.repair_seconds,
                    "runtime_s": sum(a.get("time_s", 0) for a in self.attempts),
                    "setup_s": self.setup_seconds,
                },
                "budget_tokens": self.budget_tokens,
                "diagnostics": str(self.metadata),
                "prompt_schema_bytes": len(
                    json.dumps(
                        {"system": self.messages[0]["content"], "tools": TOOL_SCHEMA},
                        ensure_ascii=False,
                    ).encode()
                ),
            },
        }

    def save(self) -> None:
        temp = self.metadata / "result.tmp"
        temp.write_text(json.dumps(self.result(), indent=2, ensure_ascii=False))
        temp.replace(self.metadata / "result.json")
        (self.metadata / "conversation.json").write_text(
            json.dumps(self.messages, indent=2, ensure_ascii=False)
        )

    async def conversation(self, repair: bool = False) -> None:
        max_turns = self.limits.repair_turns if repair else self.limits.build_turns
        max_seconds = self.limits.repair_seconds if repair else self.limits.build_seconds
        active = 0.0
        phase = "repairing" if repair else "building"
        try:
            for _ in range(max_turns):
                self.phase(phase)
                input_bound = len(
                    json.dumps(
                        {"messages": self.messages, "tools": TOOL_SCHEMA}, ensure_ascii=False
                    ).encode()
                )
                remaining_tokens = self.limits.total_tokens - self.budget_tokens - input_bound
                if remaining_tokens <= 0 or active >= max_seconds:
                    raise BudgetError("active time or total token budget exhausted")
                turn = None
                started = None
                try:
                    async with self.api_slots:
                        started = time.monotonic()
                        turn = await asyncio.wait_for(
                            call_conversation(
                                self.client,
                                self.api_key,
                                self.model_id,
                                self.messages,
                                TOOL_SCHEMA,
                                max_tokens=min(self.limits.turn_tokens, remaining_tokens),
                                reasoning_effort=self.reasoning_effort,
                                on_progress=(lambda chars: self.tracker.update(self.name, chars))
                                if self.tracker and self.tracker.is_running
                                else None,
                                on_retry=self.on_retry,
                            ),
                            max_seconds - active,
                        )
                except BaseException as exc:
                    if started is not None:
                        usage = getattr(exc, "usage", {})
                        self.turns.append(
                            {
                                "phase": phase,
                                "usage": usage,
                                "error": str(exc) or type(exc).__name__,
                            }
                        )
                        self.budget_tokens += usage.get("total_tokens") or input_bound
                    raise
                finally:
                    if started is not None:
                        elapsed = time.monotonic() - started
                        active += elapsed
                        self.api_seconds += elapsed
                self.turns.append(
                    {
                        "phase": phase,
                        "usage": turn.usage,
                        "model": turn.model,
                        "provider": turn.provider,
                        "finish_reason": turn.finish_reason,
                        "adjustments": turn.adjustments,
                    }
                )
                self.budget_tokens += turn.usage.get("total_tokens") or (
                    input_bound + len(json.dumps(turn.message).encode())
                )
                self.messages.append(turn.message)
                if self.budget_tokens > self.limits.total_tokens:
                    raise BudgetError("total token budget exhausted; tool calls skipped")
                calls = turn.message.get("tool_calls") or []
                if not calls:
                    raise TurnError("project abandoned: model ended without wb done")
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
                        self.dispatcher.batch(native), max(0.001, max_seconds - active)
                    )
                finally:
                    elapsed = time.monotonic() - started
                    active += elapsed
                    self.tool_seconds += elapsed
                self.messages.extend(
                    {
                        "role": "tool",
                        "tool_call_id": result["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                    for result in results
                )
                self.save()
                if self.dispatcher.submission:
                    self.descriptor = self.dispatcher.submission
                    return
            raise BudgetError(f"{phase} exceeded {max_turns} model turns")
        finally:
            if repair:
                self.repair_seconds += active
            else:
                self.build_seconds += active

    async def build(self) -> None:
        try:
            self.phase("preflight")
            try:
                self.tool_capability = await asyncio.wait_for(
                    capability(self.client, self.api_key, self.model_id), 20
                )
            except asyncio.TimeoutError:
                self.tool_capability = None
            if self.tool_capability is False:
                self.generation = "unsupported"
                raise SetupError("unsupported tool calling: model catalog does not advertise tools")
            await self.runtime.preflight()
            await self.conversation()
            self.generation = "submitted"
            self.submitted_at = time.monotonic()
            self.phase("queued")
        except asyncio.CancelledError:
            self.status = self.generation = "cancelled"
            self.error = "cancelled during initial generation"
            await self.runtime.close()
            self.phase("finished")
        except Exception as exc:
            if self.generation != "unsupported":
                self.generation = (
                    "budget_exhausted"
                    if isinstance(exc, (BudgetError, asyncio.TimeoutError))
                    else "failed"
                )
            self.error = str(exc) or type(exc).__name__
            self.phase("finished")

    async def execute(self) -> None:
        async with self._execution_lock:
            if (
                self._execution_started
                or self.generation != "submitted"
                or self.status == "cancelled"
            ):
                return
            self._execution_started = True
            self.queue_seconds = time.monotonic() - self.submitted_at
            try:
                for number in (1, 2):
                    self.phase("setting up")
                    started = time.monotonic()
                    try:
                        await self.runtime.setup(self.descriptor)
                    finally:
                        self.setup_seconds += time.monotonic() - started
                    async with self.process_slots:
                        # The only launch admission site. No model operation can reach it.
                        attempt = {
                            "number": number,
                            "started_at": time.time(),
                            "outcome": "running",
                            "launch": dict(self.descriptor),
                        }
                        self.attempts.append(attempt)
                        self.phase("running")
                        self.preview = await self.runtime.execute(self.descriptor, attempt)
                    if attempt["outcome"] == "success":
                        self.status = "success"
                        self.error = None
                        if self.preview:
                            if self.auto_open == "off":
                                await self.preview.stop()
                            else:
                                url = await self.runtime.present(
                                    self.preview, self.descriptor["preview"]
                                )
                                attempt["preview_url"] = url
                                opened = await asyncio.to_thread(open_preview, url)
                                if not opened:
                                    attempt["presentation_error"] = (
                                        f"browser unavailable; open {url}"
                                    )
                        elif self.auto_open != "off" and attempt.get("diagnostics"):
                            # Show output from the completed managed run; no terminal relaunch.
                            import re

                            display = re.sub(
                                r"[\x00-\x08\x0b-\x1f\x7f]", "", attempt["diagnostics"][-2000:]
                            )
                            print(f"  {self.name} runtime output:\n{display}", flush=True)
                        break
                    self.error = (
                        attempt.get("error")
                        or f"runtime exited with code {attempt.get('exit_code')}"
                    )
                    if number == 2:
                        break
                    # Failure cleanup has completed. Same conversation/model gets one repair phase.
                    self.dispatcher.reopen()
                    self.repair = "repairing"
                    self.messages.append(
                        {
                            "role": "user",
                            "content": "WaveBench run 1 failed. Repair the project with wb, then submit done for the final run.\n"
                            + json.dumps(attempt, ensure_ascii=False)[-self.limits.output_chars :],
                        }
                    )
                    await self.conversation(repair=True)
                    self.repair = "submitted"
            except asyncio.CancelledError:
                self.status = "cancelled"
                if self.repair == "repairing":
                    self.repair = "cancelled"
                self.error = "cancelled; no pending launch will restart"
                await self.runtime.close()
            except Exception as exc:
                if self.repair == "repairing":
                    self.repair = (
                        "budget_exhausted"
                        if isinstance(exc, (BudgetError, asyncio.TimeoutError))
                        else "abandoned"
                    )
                self.error = (
                    f"{self.error + '; ' if self.error else ''}{str(exc) or type(exc).__name__}"
                )
            finally:
                self.phase("finished")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.runtime.close()
            self.save()
        finally:
            self.workspace.close()


class HarnessBatch:
    def __init__(self, sessions: list[HarnessSession], auto_open: str, results: dict):
        self.sessions, self.auto_open, self.results = sessions, auto_open, results

    async def run(self) -> None:
        async def pipeline(session):
            try:
                await session.build()
                if self.auto_open != "after_all":
                    await session.execute()
            finally:
                if self.auto_open != "after_all":
                    self.results[session.name] = session.result()

        tasks = [asyncio.create_task(pipeline(session)) for session in self.sessions]
        try:
            await asyncio.gather(*tasks)
            if self.auto_open == "after_all":
                # Initial generation completion is the barrier, never execution success.
                tasks = [asyncio.create_task(session.execute()) for session in self.sessions]
                await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for session in self.sessions:
                if session.phase_name != "finished":
                    session.status = "cancelled"
                    session.error = "cancelled while queued"
                    session.phase("finished")
                await session.runtime.close()
            raise
        finally:
            for session in self.sessions:
                self.results[session.name] = session.result()

    async def review(self) -> None:
        previews = [
            session for session in self.sessions if session.preview and not session.preview._stopped
        ]
        try:
            if not previews:
                return
            seconds = self.sessions[0].limits.review_seconds
            for session in previews:
                print(f"  {session.name} preview: {session.preview.url}")
            print(
                f"  Managed previews remain open for up to {seconds}s. Press Enter or Ctrl-C to stop.",
                flush=True,
            )
            if sys.stdin.isatty():
                loop = asyncio.get_running_loop()
                done = loop.create_future()

                def entered():
                    os.read(sys.stdin.fileno(), 1024)
                    if not done.done():
                        done.set_result(None)

                loop.add_reader(sys.stdin.fileno(), entered)
                try:
                    await asyncio.wait_for(done, seconds)
                except asyncio.TimeoutError:
                    pass
                finally:
                    loop.remove_reader(sys.stdin.fileno())
            else:
                await asyncio.sleep(seconds)
        finally:
            await asyncio.gather(*(session.close() for session in self.sessions))
