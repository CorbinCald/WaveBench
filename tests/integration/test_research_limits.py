"""Research stays brief: it closes at half the requests, a third of the time, or finishing."""

from __future__ import annotations

import ast
import asyncio
import json
import time

import pytest

from wavebench.harness import session as module
from wavebench.harness.config import Limits
from wavebench.harness.session import HarnessSession
from wavebench.harness.transport import Turn
from wavebench.harness.workspace import allocate_run
from wavebench.web_search import BraveSearch

from ..harness_calls import native


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

        async def fetch(url, start=0, max_chars=8000):
            return {"url": url, "final_url": url, "fetched_at": "now", "content": "Value: 53.9"}

        async def search(query, count=5):
            return {"results": [{"title": query, "url": "https://example.com", "description": ""}]}

        async def lint():
            ast.parse(session.workspace.read("main.py"))
            return {"exit_code": 0, "diagnostics": "Checked 1 files; 0 error(s)."}

        session.dispatcher.web_fetch.fetch = fetch
        session.dispatcher.web_search.search = search
        session.runtime.lint = lint
        return session

    yield create
    for session in sessions:
        await session.close()


def turn(index, *calls):
    tool_calls = []
    for position, call in enumerate(calls):
        name, arguments = call if isinstance(call, tuple) else native(call)
        tool_calls.append(
            {
                "id": f"{index}-{position}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        )
    usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    message = {"role": "assistant", "tool_calls": tool_calls}
    return Turn(message, usage, "vendor/model", "offline", "tool_calls", {})


def names(tools):
    return {tool["function"]["name"] for tool in tools}


FETCH = ("web_fetch", {"url": "https://example.com/value"})
BUILD = [{"command": "write", "path": "main.py", "content": "print(53.9)\n"}, {"command": "lint"}]
SUBMIT = {"command": "submit", "runtime": "python", "entry": "main.py"}


@pytest.mark.parametrize("build_turns,expected_reads", [(10, 5), (7, 3)])
async def test_research_closes_at_half_the_requests_with_one_notice(
    factory, monkeypatch, build_turns, expected_reads
):
    session = factory(build_turns=build_turns)
    reads = 0
    seen = []

    async def model(client, key, model_id, messages, tools, **kwargs):
        nonlocal reads
        seen.append(names(tools))
        if "web_fetch" in names(tools):
            reads += 1
            return turn(len(seen), FETCH)
        if not session.dispatcher.lint_results:
            notes = [
                m["content"] for m in messages if "Research is now closed" in m.get("content", "")
            ]
            assert len(notes) == 1 and "53.9" in json.dumps(messages)
            return turn(len(seen), *BUILD)
        return turn(len(seen), SUBMIT)

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "submitted", session.error
    assert reads == expected_reads
    assert session.dispatcher.research_closed == "half the requests or a third of the time was used"
    assert [n["kind"] for n in session.notices][:1] == ["research_closed"]
    saved = json.loads((session.metadata / "result.json").read_text())
    assert saved["harness"]["research"]["closed"]
    assert saved["harness"]["web_fetch"]["calls"] == expected_reads


async def test_research_closes_after_a_third_of_the_phase_time(factory, monkeypatch):
    session = factory(build_seconds=6, lint_seconds=1)
    closed_at = []

    async def model(client, key, model_id, messages, tools, **kwargs):
        index = len(messages)
        if "web_search" in names(tools):
            await asyncio.sleep(0.45)
            return turn(index, ("web_search", {"query": "value"}))
        closed_at.append(session.active)
        if not session.dispatcher.lint_results:
            return turn(index, *BUILD)
        return turn(index, SUBMIT)

    monkeypatch.setattr(module, "call_conversation", model)
    started = time.monotonic()
    await session.build()
    assert session.generation == "submitted", session.error
    # 0.45 s responses: four searches use 1.8 s; the fifth crosses a third of 6 s.
    assert closed_at[0] >= 2.0 and time.monotonic() - started < 6
    assert session.dispatcher.web_search_usage["calls"] == 5


async def test_call_limits_withdraw_each_tool_and_errors_report_what_is_left(factory, monkeypatch):
    session = factory(web_search_calls=1, web_fetch_calls=2)
    requests = []

    async def model(client, key, model_id, messages, tools, **kwargs):
        requests.append(names(tools))
        if len(requests) == 1:
            return turn(1, ("web_search", {"query": "a"}), ("web_search", {"query": "b"}), FETCH)
        if len(requests) == 2:
            last = [m["content"] for m in messages if m["role"] == "tool"][-3:]
            assert "(0 searches and " in last[0]
            assert last[1].startswith("Error: web_search call limit reached (1 per model)")
            assert last[2].endswith("(0 searches and 1 page reads left)")
            assert "web_search" not in requests[-1] and "web_fetch" in requests[-1]
            return turn(2, FETCH)
        if len(requests) == 3:
            assert not {"web_search", "web_fetch"} & requests[-1]
            return turn(3, *BUILD)
        return turn(4, SUBMIT)

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "submitted", session.error
    assert session.dispatcher.web_search_usage == {"calls": 1, "failures": 0}
    assert session.dispatcher.web_fetch_usage == {"calls": 2, "failures": 0}


async def test_finishing_closes_research_and_it_stays_closed_in_repair(factory, monkeypatch):
    session = factory(build_turns=3, repair_turns=4)
    requests = []

    async def model(client, key, model_id, messages, tools, **kwargs):
        requests.append((session.phase_name, names(tools), messages[-1]["content"] or ""))
        phase, offered, last = requests[-1]
        if phase == "building":
            # The finishing notice arrives with three requests left and withdraws research.
            assert "web_search" not in offered and "Finish now" in json.dumps(messages)
            if len(requests) == 1:
                return turn(
                    1, {"command": "write", "path": "main.py", "content": "raise SystemExit(1)\n"}
                )
            return turn(2, SUBMIT)
        assert "web_search" not in offered and "Run 1 failed" in json.dumps(messages)
        if "print" not in session.workspace.read("main.py"):
            return turn(3, {"command": "write", "path": "main.py", "content": "print(1)\n"})
        return turn(4, SUBMIT)

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    await session.execute()
    assert session.status == "success", session.error
    assert session.dispatcher.research_closed == "the phase is finishing"
    assert session.repair == "submitted"


async def test_research_call_after_closure_is_rejected_without_network(factory, monkeypatch):
    session = factory()
    session.dispatcher.close_research("half the requests or a third of the time was used")
    results = await session.dispatcher.batch(
        [{"id": "late", "name": "web_search", "arguments": {"query": "x"}}]
    )
    assert results[0]["text"] == "Error: web_search is not enabled for this benchmark" or (
        "research is closed" in results[0]["text"]
    )
    assert session.dispatcher.web_search_usage == {"calls": 0, "failures": 0}
