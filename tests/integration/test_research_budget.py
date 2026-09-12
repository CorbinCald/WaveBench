"""Research must leave capacity for real workspace writes, validation and submission."""

from __future__ import annotations

import ast
import asyncio
import json
import time

import pytest

from wavebench.harness import session as module
from wavebench.harness.commands import Dispatcher
from wavebench.harness.config import Limits
from wavebench.harness.session import HarnessSession
from wavebench.harness.transport import Turn
from wavebench.harness.workspace import allocate_run
from wavebench.tokens import prompt_tokens
from wavebench.web_search import BraveSearch


@pytest.fixture
async def factory(tmp_path, monkeypatch):
    async def capable(*args):
        return True

    async def preflight(self):
        pass

    monkeypatch.setattr(module, "capability", capable)
    monkeypatch.setattr(module.Runtime, "preflight", preflight)
    sessions = []
    run = allocate_run(tmp_path, "research", "offline")

    def create(**limits):
        session = HarnessSession(
            run,
            len(sessions) + 1,
            "researcher",
            "vendor/model",
            "Research the current value, build a program using it, validate and submit.",
            None,
            "offline",
            Limits(**limits),
            asyncio.Semaphore(1),
            asyncio.Semaphore(1),
            auto_open="off",
            web_search=BraveSearch("offline"),
        )
        sessions.append(session)
        return session

    yield create
    for session in sessions:
        await session.close()


def turn(messages, tools, calls, index, charged=None):
    message = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": f"{index}-{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, (name, args) in enumerate(calls)
        ],
    }
    incoming = prompt_tokens(messages, tools)
    outgoing = prompt_tokens([message], [])
    return Turn(
        message,
        {
            "prompt_tokens": incoming,
            "completion_tokens": outgoing,
            "total_tokens": charged or incoming + outgoing,
        },
        "vendor/model",
        "offline",
        "tool_calls",
        {},
    )


@pytest.mark.parametrize("build_turns,research_turns,expected", [(32, 8, 8), (6, 8, 3), (12, 2, 2)])
async def test_researching_agent_is_guided_to_build_validate_and_submit(
    factory, monkeypatch, build_turns, research_turns, expected
):
    session = factory(build_turns=build_turns, research_turns=research_turns)
    requests = 0
    reads = 0

    async def fetch(*args):
        nonlocal reads
        reads += 1
        return {"content": "Current measured value: 53.9"}

    async def lint():
        ast.parse(session.workspace.read("main.py"))
        return {"exit_code": 0, "diagnostics": "Python syntax OK"}

    async def model(client, key, model_id, messages, tools, **kwargs):
        nonlocal requests
        requests += 1
        names = {tool["function"]["name"] for tool in tools}
        if "web_fetch" in names:
            calls = [("web_fetch", {"url": "https://example.com/value"})]
        elif session.dispatcher.lint_results:
            last_tool = next(m for m in reversed(messages) if m["role"] == "tool")
            assert json.loads(last_tool["content"])["diagnostics"] == "Python syntax OK"
            calls = [("wb", {"command": "done", "runtime": "python", "entry": "main.py"})]
        else:
            assert "Research is closed" in json.dumps(messages)
            assert "53.9" in json.dumps(messages)
            assert kwargs["max_tokens"] > 4096  # Research cutoff leaves room to implement.
            calls = [
                ("wb", {"command": "write", "path": "main.py", "content": "print(53.9)\n"}),
                ("wb", {"command": "lint"}),
            ]
        return turn(messages, tools, calls, requests)

    monkeypatch.setattr(session.dispatcher.web_fetch, "fetch", fetch)
    monkeypatch.setattr(session.runtime, "lint", lint)
    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "submitted", session.error
    assert reads == expected and requests == expected + 2
    assert session.dispatcher.tool_usage["failures"] == 0
    assert session.workspace.read("main.py") == "print(53.9)\n"
    saved = json.loads((session.metadata / "result.json").read_text())
    assert saved["harness"]["research"]["turns"] == expected
    assert saved["harness"]["research"]["closed"]


async def test_exhausted_read_tool_is_withdrawn_before_another_request(factory, monkeypatch):
    session = factory(web_fetch_calls=2)
    requests = 0

    async def fetch(*args):
        return {"content": "evidence"}

    async def model(client, key, model_id, messages, tools, **kwargs):
        nonlocal requests
        requests += 1
        names = {tool["function"]["name"] for tool in tools}
        if requests == 1:
            calls = [
                ("web_fetch", {"url": "https://example.com/one"}),
                ("web_fetch", {"url": "https://example.com/two"}),
            ]
        elif requests == 2:
            assert names == {"wb", "web_search"}
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            assert min(r["research_budget"]["read_calls_left"] for r in results) == 0
            assert "page reads left: 0" in messages[-1]["content"]
            calls = [("wb", {"command": "write", "path": "main.py", "content": "print(42)"})]
        else:
            calls = [("wb", {"command": "done", "runtime": "python", "entry": "main.py"})]
        return turn(messages, tools, calls, requests)

    monkeypatch.setattr(session.dispatcher.web_fetch, "fetch", fetch)
    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "submitted", session.error
    assert session.research_usage["turns"] == 1  # A batch consumes one research turn.
    assert session.dispatcher.web_fetch_usage == {"calls": 2, "failures": 0}


@pytest.mark.parametrize(
    "kind", ["time", "tokens", "late_turn", "phase_time", "overall_tokens", "finishing"]
)
async def test_reserves_are_enforced_and_do_not_reset_in_repair(factory, kind):
    session = factory()
    active, index = 0, 0
    if kind == "time":
        session.research_usage["seconds"] = 300
    elif kind == "tokens":
        session.research_usage["tokens"] = 200_000
    elif kind == "late_turn":
        index = 16
    elif kind == "phase_time":
        active = 600
    elif kind == "overall_tokens":
        session.budget_tokens = 340_000
    else:
        session.finishing = True
    session.prepare_research(32, index, 1800, active)
    assert [t["function"]["name"] for t in session.tools] == ["wb"]
    reason = session.dispatcher.research_closed
    session.dispatcher.reopen()
    session.prepare_research(12, 0, 300, 0)
    assert session.dispatcher.research_closed == reason
    result = (
        await session.dispatcher.batch(
            [
                {
                    "id": "ignored-warning",
                    "name": "web_fetch",
                    "arguments": {"url": "https://example.com"},
                }
            ]
        )
    )[0]
    assert not result["ok"] and "Research is closed" in result["error"]
    assert session.dispatcher.web_fetch_usage["calls"] == 0


async def test_shared_deadline_cancels_research_without_cancelling_workspace_work(
    factory, monkeypatch
):
    session = factory(parallel_calls=1)
    dispatcher = session.dispatcher
    cancelled = []

    async def slow(*args):
        try:
            await asyncio.sleep(1)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(dispatcher.web_fetch, "fetch", slow)
    dispatcher.research_deadline = time.monotonic() + 0.02
    results = await dispatcher.batch(
        [
            {"id": "slow", "name": "web_fetch", "arguments": {"url": "https://example.com/slow"}},
            {
                "id": "queued",
                "name": "web_fetch",
                "arguments": {"url": "https://example.com/queued"},
            },
            {
                "id": "write",
                "name": "wb",
                "arguments": {"command": "write", "path": "main.py", "content": "print(42)"},
            },
        ]
    )
    assert [r["ok"] for r in results] == [False, False, True]
    assert len(cancelled) == 1
    assert dispatcher.web_fetch_usage == {"calls": 1, "failures": 1}
    assert session.workspace.read("main.py") == "print(42)"


async def test_failed_calls_have_remaining_budget_and_replay_does_not_charge(tmp_path):
    dispatcher = Dispatcher(
        None, None, tmp_path, Limits(web_fetch_calls=2), web_search=BraveSearch("offline")
    )
    call = {"id": "bad", "name": "web_fetch", "arguments": {"url": "file:///private"}}
    first = (await dispatcher.batch([call]))[0]
    assert not first["ok"] and first["research_budget"]["read_calls_left"] == 1
    assert (await dispatcher.batch([call]))[0] == first
    assert dispatcher.web_fetch_usage == {"calls": 1, "failures": 1}


@pytest.mark.parametrize("resource", ["time", "tokens"])
async def test_generation_cost_can_close_research_before_network_calls(
    factory, monkeypatch, resource
):
    session = factory(research_tokens=1000) if resource == "tokens" else factory()
    requests = 0
    network = []

    async def fetch(*args):
        network.append(True)
        return {"content": "unused"}

    async def model(client, key, model_id, messages, tools, **kwargs):
        nonlocal requests
        requests += 1
        if requests == 1:
            if resource == "time":
                # Prior research consumed nearly the full active allowance.
                session.research_usage["seconds"] = 299.999
                await asyncio.sleep(0.005)
            calls = [("web_fetch", {"url": "https://example.com"})]
        elif requests == 2:
            assert {t["function"]["name"] for t in tools} == {"wb"}
            calls = [
                (
                    "wb",
                    {
                        "command": "write",
                        "path": "main.py",
                        "content": "print('Evidence unavailable')",
                    },
                )
            ]
        else:
            calls = [("wb", {"command": "done", "runtime": "python", "entry": "main.py"})]
        return turn(messages, tools, calls, requests)

    monkeypatch.setattr(session.dispatcher.web_fetch, "fetch", fetch)
    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "submitted", session.error
    assert not network
    assert session.dispatcher.web_fetch_usage["calls"] == 0


async def test_finishing_warning_withdraws_research_in_the_same_request(factory, monkeypatch):
    session = factory(total_tokens=40_000)
    requests = 0

    async def model(client, key, model_id, messages, tools, **kwargs):
        nonlocal requests
        requests += 1
        assert {t["function"]["name"] for t in tools} == {"wb"}
        assert "[WaveBench budget warning]" in json.dumps(messages)
        calls = (
            [("wb", {"command": "write", "path": "main.py", "content": "print(42)"})]
            if requests == 1
            else [("wb", {"command": "done", "runtime": "python", "entry": "main.py"})]
        )
        return turn(messages, tools, calls, requests)

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "submitted", session.error
    assert session.research_usage["turns"] == 0
