"""Phase limits stay in view: notices always arrive, and limits end a phase clearly."""

from __future__ import annotations

import asyncio
import json

import pytest

from wavebench.harness import session as module
from wavebench.harness.config import Limits
from wavebench.harness.session import HarnessSession, finish_seconds
from wavebench.harness.transport import Turn
from wavebench.harness.workspace import allocate_run

from ..harness_calls import native


@pytest.fixture
async def factory(tmp_path, monkeypatch):
    async def capable(*args):
        return True

    async def preflight(self):
        pass

    monkeypatch.setattr(module, "capability", capable)
    monkeypatch.setattr(module.Runtime, "preflight", preflight)
    run = allocate_run(tmp_path, "limits", "offline")
    sessions = []

    def create(**limits):
        session = HarnessSession(
            run,
            len(sessions) + 1,
            "model",
            "vendor/model",
            "Build a program.",
            None,
            "offline",
            Limits(**limits),
            asyncio.Semaphore(1),
            asyncio.Semaphore(1),
            auto_open="off",
        )
        sessions.append(session)
        return session

    yield create
    for session in sessions:
        await session.close()


def turn(index, *commands):
    calls = []
    for position, command in enumerate(commands):
        name, arguments = native(command)
        calls.append(
            {
                "id": f"{index}-{position}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        )
    usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    return Turn(
        {"role": "assistant", "tool_calls": calls}, usage, "vendor/model", "x", "tool_calls", {}
    )


WRITE = {"command": "write", "path": "main.py", "content": "print(1)\n"}
SUBMIT = {"command": "submit", "runtime": "python", "entry": "main.py"}


def notes(messages):
    return [m["content"] for m in messages if m["role"] == "user" and "[WaveBench]" in m["content"]]


async def test_finishing_and_final_request_notices_arrive_in_order(factory, monkeypatch):
    session = factory(build_turns=5)
    seen = []

    async def model(client, key, model_id, messages, tools, **kwargs):
        seen.append(notes(messages))
        if len(seen) < 5:
            return turn(len(seen), WRITE)
        return turn(len(seen), SUBMIT)

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "submitted", session.error
    assert [len(found) for found in seen] == [0, 0, 1, 1, 2]
    assert seen[2][0].startswith("[WaveBench] 3 requests and about ")
    assert seen[2][0].endswith("Finish now: make only essential fixes, run lint, and call submit.")
    assert seen[4][1] == (
        "[WaveBench] This is the last request of the building phase. Call submit now if the "
        "project can run; a text reply does not submit."
    )
    assert [n["kind"] for n in session.notices] == ["finishing", "final_request"]
    assert session.result()["harness"]["notices"] == session.notices


async def test_the_request_limit_ends_the_phase_with_its_name(factory, monkeypatch):
    session = factory(build_turns=4)

    async def model(client, key, model_id, messages, tools, **kwargs):
        return turn(len(messages), {"command": "ls"})

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "budget_exhausted"
    assert session.error == "building exceeded 4 model turns"
    assert session.failure["code"] == "time_or_turn_limit"


async def test_slow_responses_start_finishing_before_time_runs_out(factory, monkeypatch):
    session = factory(build_seconds=6, lint_seconds=1)
    seen = []

    async def model(client, key, model_id, messages, tools, **kwargs):
        seen.append((round(session.active, 1), notes(messages)))
        if not any("Finish now" in note for note in notes(messages)):
            await asyncio.sleep(0.9)
            return turn(len(seen), WRITE)
        return turn(len(seen), SUBMIT)

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "submitted", session.error
    # Twice the slowest response plus lint (2.8 s) must remain when finishing starts.
    active, found = seen[-1]
    assert 6 - active >= 2.8 and "seconds remain in this phase" in found[-1]


def test_finish_time_keeps_room_for_two_responses_and_lint():
    assert finish_seconds([], 1800, 30) == 360
    assert finish_seconds([10, 400, 20], 1800, 30) == 830
    assert finish_seconds([10, 400, 20, 5, 5, 5], 1800, 30) == 360
