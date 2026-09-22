"""Subagents share the lead's workspace and budget; only the lead submits."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys

import pytest

from wavebench.harness import session as module
from wavebench.harness.config import Limits
from wavebench.harness.session import HarnessSession
from wavebench.harness.subagents import FINAL_NOTICE
from wavebench.harness.transport import Turn, TurnError
from wavebench.harness.workspace import allocate_run
from wavebench.tui.progress import ProgressTracker

USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.001}


@pytest.fixture
async def factory(tmp_path, monkeypatch):
    if sys.platform != "linux" or not shutil.which("bwrap"):
        if os.getenv("WAVEBENCH_REQUIRE_SANDBOX_TESTS"):
            pytest.fail("bwrap is required")
        pytest.skip("requires Linux and bwrap")

    async def capable(*args):
        return True

    monkeypatch.setattr(module, "capability", capable)
    monkeypatch.setattr(module, "open_preview", lambda url, log_path: True)
    run = allocate_run(tmp_path, "subagents", "test")
    sessions = []
    api_slots = asyncio.Semaphore(4)
    process_slots = asyncio.Semaphore(2)

    def create(name="lead", **kwargs):
        instance = HarnessSession(
            run,
            len(sessions) + 1,
            name,
            f"vendor/{name}",
            "Build a small project",
            None,
            "offline",
            kwargs.pop("limits", Limits(review_seconds=1)),
            api_slots,
            process_slots,
            auto_open=kwargs.pop("auto_open", "off"),
            subagents=kwargs.pop("subagents", True),
            **kwargs,
        )
        sessions.append(instance)
        return instance

    yield create
    for session in sessions:
        await session.close()


def role(messages) -> tuple[str, str]:
    """Identify the conversation: ("lead", "") or ("sub", agent name)."""
    if messages[0]["content"].startswith("You are a subagent"):
        match = re.search(r"\(agent \d+, ([^)]+)\)", messages[1]["content"])
        return "sub", match.group(1)
    return "lead", ""


def tool_names(tools) -> list[str]:
    return [tool["function"]["name"] for tool in tools]


def calls(index: int, *specs) -> Turn:
    """An assistant turn with tool calls; each spec is (name, arguments) or arguments."""
    tool_calls = []
    for position, spec in enumerate(specs):
        name, arguments = spec if isinstance(spec, tuple) else ("wb", spec)
        tool_calls.append(
            {
                "id": f"call-{index}-{position}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        )
    message = {"role": "assistant", "tool_calls": tool_calls}
    return Turn(message, dict(USAGE), "vendor/lead", "offline", "tool_calls", {})


def text(content: str, *specs, usage=None) -> Turn:
    turn = (
        calls(0, *specs)
        if specs
        else Turn(
            {"role": "assistant", "content": content},
            dict(USAGE),
            "vendor/lead",
            "offline",
            "stop",
            {},
        )
    )
    turn.message["content"] = content
    if usage is not None:
        turn.usage = usage
    return turn


def results(messages, count: int) -> list[dict]:
    """The latest tool results; budget and research notices may follow them."""
    tool_messages = [message for message in messages if message["role"] == "tool"]
    return [json.loads(message["content"]) for message in tool_messages[-count:]]


def spawn(name: str, task: str, **extra) -> tuple[str, dict]:
    return ("spawn_agent", {"name": name, "task": task, **extra})


def write(path: str, content: str) -> dict:
    return {"command": "write", "path": path, "content": content}


DONE = {"command": "done", "runtime": "python", "entry": "main.py"}
LINT = {"command": "lint"}


async def test_parallel_subagents_share_workspace_and_budget_then_lead_submits(
    factory, monkeypatch
):
    active = peak = 0
    conversations: dict[str, list[list[dict]]] = {}
    tracker = ProgressTracker(1, {})
    tracker._running = True

    async def model(client, key, model_id, messages, tools, **kwargs):
        nonlocal active, peak
        kind, agent = role(messages)
        history = conversations.setdefault(kind + agent, [])
        history.append(json.loads(json.dumps(messages)))
        index = len(history) - 1
        if kind == "lead":
            if index == 0:
                assert "spawn_agent" in tool_names(tools)
                assert "up to 2 run at once and 3 in total" in messages[0]["content"]
                return calls(
                    index,
                    *(
                        spawn(name, f"Write lib/{name}.py defining VALUE = {value}")
                        for name, value in (("alpha", 1), ("beta", 2), ("gamma", 3))
                    ),
                )
            if index == 1:
                reports = results(messages, 3)
                assert [r["status"] for r in reports] == ["completed"] * 3
                assert all(r["ok"] for r in reports)
                assert [r["files"]["written"] for r in reports] == [
                    ["lib/alpha.py"],
                    ["lib/beta.py"],
                    ["lib/gamma.py"],
                ]
                assert [r["agent"] for r in reports] == ["01-alpha", "02-beta", "03-gamma"]
                assert {r["agents_left"] for r in reports} == {0}
                assert all(r["turns"] == 2 and r["tool_calls"] == 1 for r in reports)
                assert all(r["usage"] == {"total_tokens": 30, "cost": 0.002} for r in reports)
                assert "wrote lib/alpha.py" in reports[0]["report"]
                # The cap is reached, so the tool is withdrawn from this request.
                assert "spawn_agent" not in tool_names(tools)
                return calls(
                    index,
                    write(
                        "main.py",
                        "from lib.alpha import VALUE as a\nfrom lib.beta import VALUE as b\n"
                        "from lib.gamma import VALUE as c\nprint(a + b + c)\n",
                    ),
                    LINT,
                )
            return calls(index, DONE)
        assert "spawn_agent" not in tool_names(tools)
        assert "done" not in tools[0]["function"]["parameters"]["properties"]["command"]["enum"]
        assert "Build a small project" in messages[1]["content"]
        assert f"Write lib/{agent}.py" in messages[1]["content"]
        if index == 0:
            active += 1
            peak = max(peak, active)
            kwargs["on_progress"](120)
            kwargs["on_usage"](dict(USAGE), 5)
            await asyncio.sleep(0.2)
            active -= 1
            value = {"alpha": 1, "beta": 2, "gamma": 3}[agent]
            return calls(index, write(f"lib/{agent}.py", f"VALUE = {value}\n"))
        assert results(messages, 1)[0]["ok"]
        return text(f"wrote lib/{agent}.py exposing VALUE; lint not run")

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(
        limits=Limits(subagent_parallel=2, subagent_cap=3, subagent_turns=4, review_seconds=1),
        tracker=tracker,
    )
    await session.build()
    await session.execute()
    assert session.status == "success", session.error
    assert peak == 2
    assert any("6" in path.read_text() for path in session.metadata.glob("run-*.log"))
    phases = [turn["phase"] for turn in session.turns]
    assert phases.count("building") == 3 and phases.count("subagent") == 6
    assert sorted({turn.get("agent") for turn in session.turns if "agent" in turn}) == [1, 2, 3]
    assert session.usage()["api_turns"] == 9
    assert session.budget_tokens == 9 * 15
    assert session.usage()["cost"] == pytest.approx(0.009)
    assert session.dispatcher.tool_usage == {"calls": 9, "failures": 0}
    result = session.result()
    agents = result["harness"]["subagents"]
    assert agents["enabled"] and agents["parallel"] == 2 and agents["cap"] == 3
    assert agents["spawned"] == agents["completed"] == 3
    assert agents["failed"] == agents["rejected"] == agents["active"] == 0
    assert agents["usage"]["total_tokens"] == 90 and agents["usage"]["api_turns"] == 6
    assert [run["status"] for run in agents["runs"]] == ["completed"] * 3
    assert agents["runs"][1]["files"]["written"] == ["lib/beta.py"]
    assert agents["runs"][1]["tool_usage"] == {"calls": 1, "failures": 0}
    assert result["harness"]["model_usage"]["total_tokens"] == 135
    assert result["harness"]["timing"]["subagent_s"] > 0
    assert "delegating" in [event["phase"] for event in session.events]
    for run in agents["runs"]:
        folder = session.metadata / "subagents" / run["label"]
        saved = json.loads((folder / "result.json").read_text())
        assert saved["status"] == "completed" and saved["task"].startswith("Write lib/")
        assert len(json.loads((folder / "conversation.json").read_text())) == 5
        assert (folder / "tool-0001.json").exists()
    assert tracker._harness_metrics(session.name)["subagents"] == 3
    assert "agents 3" in tracker._format_harness_tool_metrics(session.name)
    assert tracker._harness[session.name]["subagents"]["completed"] == 3
    stored = json.loads((session.metadata / "result.json").read_text())
    assert stored["harness"]["subagents"]["spawned"] == 3


async def test_cap_rejections_no_nesting_no_submission_and_read_only(factory, monkeypatch):
    conversations: dict[str, int] = {}

    async def model(client, key, model_id, messages, tools, **kwargs):
        kind, agent = role(messages)
        index = conversations.get(kind + agent, 0)
        conversations[kind + agent] = index + 1
        if kind == "lead":
            if index == 0:
                return calls(
                    index,
                    spawn("worker", "Write lib/value.py with VALUE = 4"),
                    spawn("reviewer", "Read the project and report", read_only=True),
                )
            if index == 1:
                worker, reviewer = results(messages, 2)
                assert worker["ok"] and worker["files"]["written"] == ["lib/value.py"]
                assert worker["tool_failures"] == 2
                assert reviewer["ok"] and reviewer["files"] == {
                    "written": [],
                    "edited": [],
                    "deleted": [],
                }
                assert "spawn_agent" not in tool_names(tools)
                return calls(index, spawn("extra", "Anything"))
            if index == 2:
                rejected = results(messages, 1)[0]
                assert not rejected["ok"] and "agent cap reached" in rejected["error"]
                return calls(
                    index, write("main.py", "from lib.value import VALUE\nprint(VALUE)\n"), LINT
                )
            return calls(index, DONE)
        if agent == "worker":
            if index == 0:
                return calls(
                    index,
                    spawn("nested", "Nesting is not allowed"),
                    {"command": "done", "runtime": "python", "entry": "main.py"},
                    write("lib/value.py", "VALUE = 4\n"),
                )
            nested, done, written = results(messages, 3)
            assert "cannot spawn" in nested["error"] and "cannot submit" in done["error"]
            assert written["ok"]
            return text("wrote lib/value.py")
        if index == 0:
            assert "read-only" in messages[0]["content"]
            return calls(index, write("notes.md", "must fail"), {"command": "ls"})
        blocked, listing = results(messages, 2)
        assert "read-only" in blocked["error"] and listing["ok"]
        return text("reviewed: lib present")

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(limits=Limits(subagent_cap=2, review_seconds=1))
    await session.build()
    await session.execute()
    assert session.status == "success", session.error
    assert not session.workspace.ls("notes.md") if False else True
    assert "notes.md" not in [entry["name"] for entry in session.workspace.ls()]
    agents = session.result()["harness"]["subagents"]
    assert agents["spawned"] == 2 and agents["rejected"] == 1 and agents["completed"] == 2
    assert agents["runs"][1]["read_only"] is True
    assert agents["runs"][0]["tool_usage"] == {"calls": 3, "failures": 2}
    assert agents["runs"][1]["tool_usage"] == {"calls": 2, "failures": 1}
    # Lead: 2 spawns + 1 rejected spawn + write + lint + done; agents: 3 + 2 calls.
    assert session.dispatcher.tool_usage == {"calls": 11, "failures": 4}


async def test_turn_limit_injects_final_notice_and_skips_pending_tools(factory, monkeypatch):
    seen: dict[str, int] = {}

    async def model(client, key, model_id, messages, tools, **kwargs):
        kind, agent = role(messages)
        index = seen.get(kind + agent, 0)
        seen[kind + agent] = index + 1
        if kind == "lead":
            if index == 0:
                return calls(index, spawn("slow", "Write lib/a.py and lib/b.py"))
            if index == 1:
                report = results(messages, 1)[0]
                assert not report["ok"] and report["status"] == "turn_limit"
                assert "1 pending tool call(s) were not run" in report["error"]
                assert report["report"] == "half done" and report["files"]["written"] == [
                    "lib/a.py"
                ]
                return calls(index, write("main.py", "from lib.a import A\nprint(A)\n"), LINT)
            return calls(index, DONE)
        if index == 0:
            assert messages[-1]["content"] != FINAL_NOTICE
            return calls(index, write("lib/a.py", "A = 1\n"))
        assert messages[-1] == {"role": "user", "content": FINAL_NOTICE}
        return text("half done", write("lib/b.py", "B = 2\n"))

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(limits=Limits(subagent_turns=2, review_seconds=1))
    await session.build()
    await session.execute()
    assert session.status == "success", session.error
    names = [entry["name"] for entry in session.workspace.ls("lib")]
    assert "a.py" in names and "b.py" not in names
    run = session.result()["harness"]["subagents"]["runs"][0]
    assert run["status"] == "turn_limit" and run["turns"] == 2 and run["pending_calls"] == 1


async def test_subagent_recovers_within_its_turn_limit_and_keeps_reasoning_capacity(
    factory, monkeypatch
):
    seen = {}

    async def model(client, key, model_id, messages, tools, **kwargs):
        kind, agent = role(messages)
        index = seen.get(kind + agent, 0)
        seen[kind + agent] = index + 1
        if kind == "lead":
            if index == 0:
                return calls(index, spawn("writer", "Write main.py to print 42 and report."))
            report = results(messages, 1)[0]
            assert report["status"] == "completed" and report["turns"] == 3
            return calls(index, DONE)
        assert kwargs["max_tokens"] == 64_000
        if index == 0:
            raise TurnError("truncated", dict(USAGE), failure_code="output_truncated")
        assert "[WaveBench response recovery]" in json.dumps(messages)
        if index == 1:
            return calls(index, write("main.py", "print(42)\n"))
        assert messages[-1]["content"] == FINAL_NOTICE
        return text("main.py prints 42.")

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(limits=Limits(subagent_turns=3, review_seconds=1))
    await session.build()
    await session.execute()
    assert session.status == "success", session.error
    assert len(session.attempts) == 1
    assert session.budget_tokens == 5 * USAGE["total_tokens"]
    assert session.recoveries == [
        {
            "kind": "response_retry",
            "phase": "subagent",
            "agent": 1,
            "turn": 1,
            "failure_code": "output_truncated",
        }
    ]


async def test_subagent_failure_and_budget_exhaustion_keep_the_lead_working(factory, monkeypatch):
    seen: dict[str, int] = {}
    warned = []

    async def model(client, key, model_id, messages, tools, **kwargs):
        kind, agent = role(messages)
        index = seen.get(kind + agent, 0)
        seen[kind + agent] = index + 1
        if kind == "lead":
            if index == 0:
                return calls(
                    index,
                    spawn("crash", "Write lib/crash.py"),
                    spawn("hungry", "Write lib/hungry.py"),
                )
            if index == 1:
                crash, hungry = results(messages, 2)
                assert crash["status"] == "failed" and "provider failed" in crash["error"]
                assert crash["usage"] == {"total_tokens": 7, "cost": None}
                assert hungry["status"] == "budget_exhausted"
                assert "finishing reserve" in hungry["error"]
                assert hungry["files"]["written"] == ["lib/hungry.py"] and hungry["turns"] == 1
                warned.append(any("budget warning" in m.get("content", "") for m in messages))
                return calls(index, write("main.py", "from lib.hungry import H\nprint(H)\n"), LINT)
            return calls(index, DONE)
        if agent == "crash":
            raise TurnError("provider failed", {"total_tokens": 7})
        if index == 0:
            big = {"prompt_tokens": 940_000, "completion_tokens": 50_000, "total_tokens": 990_000}
            return text("", write("lib/hungry.py", "H = 9\n"), usage=big)
        pytest.fail("a subagent must stop before consuming the lead's finishing reserve")

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(limits=Limits(review_seconds=1))
    await session.build()
    await session.execute()
    assert session.status == "success", session.error
    assert warned == [True]
    assert session.budget_tokens == 990_000 + 7 + 3 * 15
    agents = session.result()["harness"]["subagents"]
    assert [run["status"] for run in agents["runs"]] == ["failed", "budget_exhausted"]
    assert agents["failed"] == 2 and agents["completed"] == 0
    assert agents["runs"][0]["failure"]["category"] == "model_protocol"
    assert agents["usage"]["known_total_tokens"] == 990_007
    assert session.usage()["total_tokens"] == 990_052
    assert session.usage()["cost"] is None  # The crash reported no cost.


@pytest.mark.parametrize("scope", ["phase", "subagent"])
async def test_time_limits_cancel_or_end_running_subagents(factory, monkeypatch, scope):
    seen: dict[str, int] = {}

    async def model(client, key, model_id, messages, tools, **kwargs):
        kind, agent = role(messages)
        index = seen.get(kind + agent, 0)
        seen[kind + agent] = index + 1
        if kind == "lead":
            if index == 0:
                return calls(index, spawn("slow", "Write lib/slow.py"))
            if index == 1:
                report = results(messages, 1)[0]
                assert report["status"] == "time_limit" and "time limit" in report["error"]
                return calls(index, write("main.py", "print(1)\n"), LINT)
            return calls(index, DONE)
        await asyncio.sleep(2)
        return calls(index, write("lib/slow.py", "S = 1\n"))

    monkeypatch.setattr(module, "call_conversation", model)
    limits = (
        Limits(build_seconds=1, review_seconds=1)
        if scope == "phase"
        else Limits(subagent_seconds=1, review_seconds=1)
    )
    session = factory(limits=limits)
    await session.build()
    await session.execute()
    run = session.subagents.runs[0]
    saved = json.loads((run.metadata / "result.json").read_text())
    if scope == "phase":
        assert session.generation == "budget_exhausted"
        assert "building active time budget exhausted" in session.error
        assert run.status == saved["status"] == "cancelled"
        assert not session.attempts
    else:
        assert session.status == "success", session.error
        assert run.status == saved["status"] == "time_limit"
        # The interrupted request is still a recorded, charged turn, as for the lead.
        assert len(run.turns) == 1 and run.turns[0]["error"]
        assert "lib" not in [e["name"] for e in session.workspace.ls()]


async def test_subagent_research_shares_the_lead_quotas(factory, monkeypatch):
    seen: dict[str, int] = {}
    queries = []

    class Search:
        async def search(self, query, count):
            queries.append(query)
            return {"results": [{"title": query, "url": "https://example.test", "snippet": ""}]}

    async def model(client, key, model_id, messages, tools, **kwargs):
        kind, agent = role(messages)
        index = seen.get(kind + agent, 0)
        seen[kind + agent] = index + 1
        if kind == "lead":
            if index == 0:
                assert {"web_search", "web_fetch", "spawn_agent"} <= set(tool_names(tools))
                return calls(index, spawn("researcher", "Search twice and report", read_only=True))
            if index == 1:
                assert "web_search" not in tool_names(tools)
                assert results(messages, 1)[0]["ok"]
                return calls(index, write("main.py", "print('ok')\n"))
            return calls(index, DONE)
        if index == 0:
            assert {"web_search", "web_fetch"} <= set(tool_names(tools))
            assert "web_search" in messages[0]["content"]
            return calls(
                index,
                ("web_search", {"query": "first"}),
                ("web_search", {"query": "second"}),
                ("web_search", {"query": "third"}),
            )
        first, second, third = results(messages, 3)
        assert first["ok"] and second["ok"]
        assert not third["ok"] and "budget exhausted" in third["error"]
        assert third["research_budget"]["search_calls_left"] == 0
        assert "web_search" not in tool_names(tools) and "web_fetch" in tool_names(tools)
        return text("found two results")

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(limits=Limits(web_search_calls=2, review_seconds=1), web_search=Search())
    await session.build()
    await session.execute()
    assert session.status == "success", session.error
    assert queries == ["first", "second"]
    harness = session.result()["harness"]
    # A call rejected for quota is not a search attempt; it is the agent's tool failure.
    assert harness["web_search"] == {
        "enabled": True,
        "provider": "brave",
        "calls": 2,
        "failures": 0,
    }
    assert harness["subagents"]["runs"][0]["tool_usage"] == {"calls": 3, "failures": 1}


async def test_repair_phase_can_still_spawn_within_the_shared_cap(factory, monkeypatch):
    seen: dict[str, int] = {}

    async def model(client, key, model_id, messages, tools, **kwargs):
        kind, agent = role(messages)
        index = seen.get(kind + agent, 0)
        seen[kind + agent] = index + 1
        if kind == "lead":
            if index == 0:
                return calls(index, spawn("builder", "Write main.py that raises"))
            if index == 1:
                return calls(index, DONE)
            if index == 2:
                assert "run 1 failed" in messages[-1]["content"]
                assert "spawn_agent" in tool_names(tools)
                return calls(index, spawn("fixer", "Make main.py print 42"))
            if index == 3:
                assert results(messages, 1)[0]["files"]["edited"] == ["main.py"]
                assert "spawn_agent" not in tool_names(tools)
                return calls(index, LINT)
            return calls(index, DONE)
        if index == 0:
            if agent == "builder":
                return calls(index, write("main.py", "raise RuntimeError('first')\n"))
            return calls(
                index,
                {
                    "command": "edit",
                    "path": "main.py",
                    "old": "raise RuntimeError('first')",
                    "new": "print(42)",
                },
            )
        return text(f"{agent} finished")

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(limits=Limits(subagent_cap=2, review_seconds=1))
    await session.build()
    await session.execute()
    assert session.status == "success", session.error
    assert len(session.attempts) == 2 and session.repair == "submitted"
    agents = session.result()["harness"]["subagents"]
    assert [run["name"] for run in agents["runs"]] == ["builder", "fixer"]
    assert agents["spawned"] == 2 and agents["completed"] == 2


async def test_disabled_subagents_expose_no_tool_and_reject_calls(factory, monkeypatch):
    seen = []

    async def model(client, key, model_id, messages, tools, **kwargs):
        seen.append(tool_names(tools))
        if len(seen) == 1:
            assert "Subagents:" not in messages[0]["content"]
            return calls(0, spawn("nobody", "Should be rejected"), write("main.py", "print(2)\n"))
        if len(seen) == 2:
            rejected, written = results(messages, 2)
            assert "not enabled" in rejected["error"] and written["ok"]
            return calls(1, DONE)
        pytest.fail("unexpected request")

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(subagents=False)
    await session.build()
    await session.execute()
    assert session.status == "success", session.error
    assert seen == [["wb"], ["wb"]]
    assert session.result()["harness"]["subagents"] == {"enabled": False}
    assert session.result()["harness"]["timing"]["subagent_s"] == 0.0


async def test_lead_writing_files_alone_is_reminded_once_to_delegate(factory, monkeypatch):
    seen: dict[str, int] = {}
    reminders = []

    async def model(client, key, model_id, messages, tools, **kwargs):
        kind, agent = role(messages)
        index = seen.get(kind + agent, 0)
        seen[kind + agent] = index + 1
        if kind == "lead":
            reminders.append(
                [m["content"] for m in messages if "[WaveBench subagents]" in m.get("content", "")]
            )
            if index == 0:
                return calls(index, write("index.html", "<h1>hi</h1>"))
            if index == 1:
                return calls(index, write("lib/a.py", "A = 1\n"))
            if index == 2:
                assert "written 2 files yourself" in messages[-1]["content"]
                assert (
                    "Up to 4 subagents can run in parallel (8 left in total)"
                    in messages[-1]["content"]
                )
                return calls(index, spawn("b", "Write lib/b.py with B = 2"))
            if index == 3:
                return calls(index, write("main.py", "from lib.a import A\nprint(A)\n"), LINT)
            return calls(index, DONE)
        if index == 0:
            return calls(index, write("lib/b.py", "B = 2\n"))
        return text("done")

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(limits=Limits(review_seconds=1))
    await session.build()
    await session.execute()
    assert session.status == "success", session.error
    # The reminder appears once, after the second file, and never again after spawning.
    assert [len(found) for found in reminders] == [0, 0, 1, 1, 1]
    assert session.recoveries == [{"kind": "subagent_reminder", "phase": "building", "turn": 2}]
    assert session.result()["harness"]["subagents"]["reminded"] is True


async def test_no_reminder_when_the_lead_delegates_first_or_subagents_are_off(factory, monkeypatch):
    seen: dict[str, int] = {}

    async def model(client, key, model_id, messages, tools, **kwargs):
        kind, agent = role(messages)
        index = seen.get(kind + agent, 0)
        seen[kind + agent] = index + 1
        assert not any("[WaveBench subagents]" in m.get("content", "") for m in messages)
        if kind == "lead":
            if index == 0:
                return calls(index, spawn("a", "Write lib/a.py with A = 1"))
            if index == 1:
                return calls(index, write("x.txt", "1"), write("y.txt", "2"), write("z.txt", "3"))
            if index == 2:
                return calls(index, write("main.py", "from lib.a import A\nprint(A)\n"), LINT)
            return calls(index, DONE)
        if index == 0:
            return calls(index, write("lib/a.py", "A = 1\n"))
        return text("done")

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(limits=Limits(review_seconds=1))
    await session.build()
    await session.execute()
    assert session.status == "success", session.error
    assert session.recoveries == []
    seen.clear()
    quiet = factory("quiet", subagents=False)

    async def solo(client, key, model_id, messages, tools, **kwargs):
        assert not any("[WaveBench subagents]" in m.get("content", "") for m in messages)
        index = seen.get("solo", 0)
        seen["solo"] = index + 1
        if index < 3:
            return calls(index, write(f"f{index}.py", "x = 1\n"))
        if index == 3:
            return calls(index, write("main.py", "print(1)\n"))
        return calls(index, DONE)

    monkeypatch.setattr(module, "call_conversation", solo)
    await quiet.build()
    await quiet.execute()
    assert quiet.status == "success", quiet.error
    assert quiet.recoveries == []
