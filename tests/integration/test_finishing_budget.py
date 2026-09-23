"""Controller and real wb dispatch coverage; sandbox startup is tested separately."""

from __future__ import annotations

import ast
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from wavebench.harness import session as module
from wavebench.harness.budget import finish_reserve, input_growth
from wavebench.harness.config import Limits
from wavebench.harness.session import HarnessSession
from wavebench.harness.transport import Turn
from wavebench.harness.workspace import allocate_run
from wavebench.tokens import prompt_tokens


@pytest.fixture
async def session_factory(tmp_path, monkeypatch):
    async def capable(*args):
        return True

    async def preflight(self):
        pass

    monkeypatch.setattr(module, "capability", capable)
    monkeypatch.setattr(module.Runtime, "preflight", preflight)
    run = allocate_run(tmp_path, "finishing", "test")
    sessions = []

    def create(**limits):
        session = HarnessSession(
            run,
            len(sessions) + 1,
            "model",
            "vendor/model",
            "Build a Python program that prints 42 and validate it before submitting.",
            None,
            "offline",
            Limits(
                total_tokens=limits.pop("total_tokens", 40_000),
                turn_tokens=limits.pop("turn_tokens", 4096),
                **limits,
            ),
            asyncio.Semaphore(1),
            asyncio.Semaphore(1),
            auto_open="off",
        )
        sessions.append(session)
        return session

    yield create
    for session in sessions:
        await session.close()


def response(messages, tools, commands, index=0, usage=None):
    message = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": f"{index}-{i}",
                "type": "function",
                "function": {"name": "wb", "arguments": json.dumps(command)},
            }
            for i, command in enumerate(commands)
        ],
    }
    incoming, outgoing = prompt_tokens(messages, tools), prompt_tokens([message], [])
    return Turn(
        message,
        usage
        if usage is not None
        else {
            "prompt_tokens": incoming,
            "completion_tokens": outgoing,
            "total_tokens": incoming + outgoing,
        },
        "vendor/model",
        "offline",
        "tool_calls",
        {},
    )


def warnings(session):
    return [r for r in session.budget_decisions if r["kind"] == "warning"]


async def test_warning_leaves_room_for_real_write_validation_and_standalone_done(
    session_factory, monkeypatch
):
    session = session_factory()
    requests = []

    async def lint():
        # Real syntax validation of the file produced through wb. No generated
        # program is executed outside the production sandbox.
        ast.parse(session.workspace.read("main.py"))
        return {"exit_code": 0, "diagnostics": "Python syntax OK"}

    async def model(client, key, model_id, messages, tools, **kwargs):
        requests.append((json.loads(json.dumps(messages)), kwargs))
        assert "[WaveBench budget warning]" in json.dumps(messages)
        assert kwargs["max_tokens"] <= 4096
        if len(requests) == 1:
            assert warnings(session)[0]["reserve_affordable"]
            commands = [
                {"command": "write", "path": "main.py", "content": "print(42)\n"},
                {"command": "lint"},
            ]
        else:
            assert json.loads(messages[-1]["content"])["diagnostics"] == "Python syntax OK"
            commands = [{"command": "done", "runtime": "python", "entry": "main.py"}]
        return response(messages, tools, commands, len(requests))

    monkeypatch.setattr(session.runtime, "lint", lint)
    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "submitted", session.error
    assert session.descriptor["entry"] == "main.py"
    assert session.dispatcher.tool_usage == {"calls": 3, "failures": 0}
    assert len(session.dispatcher.lint_results) == 1
    assert len(requests) == 2 and len(warnings(session)) == 1
    assert warnings(session)[0]["status"] == "received_response"
    assert warnings(session)[0]["warning_input_tokens"] > 0
    assert session.budget_tokens == sum(t["usage"]["total_tokens"] for t in session.turns)
    assert session.budget_tokens < session.limits.total_tokens
    assert session.limits.total_tokens == 40_000
    saved = json.loads((session.metadata / "result.json").read_text())
    assert saved["harness"]["finishing_budget"]["records"] == session.budget_decisions


async def test_ample_budget_does_not_inject_warning(session_factory, monkeypatch):
    session = session_factory(total_tokens=500_000)
    session.workspace.write("main.py", "print(42)\n")

    async def model(client, key, model_id, messages, tools, **kwargs):
        assert "[WaveBench budget warning]" not in json.dumps(messages)
        assert kwargs["max_tokens"] == session.limits.turn_tokens
        return response(
            messages, tools, [{"command": "done", "runtime": "python", "entry": "main.py"}]
        )

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "submitted"
    assert not warnings(session)


async def test_full_ordinary_response_cannot_make_the_warning_arrive_too_late(
    session_factory, monkeypatch
):
    session = session_factory(total_tokens=60_000, turn_tokens=16_384)
    requests = []

    async def lint():
        ast.parse(session.workspace.read("main.py"))
        return {"exit_code": 0, "diagnostics": "Python syntax OK"}

    async def model(client, key, model_id, messages, tools, **kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            commands = [
                {"command": "write", "path": "main.py", "content": "print(42)\n"},
                {"command": "lint"},
            ]
            turn = response(messages, tools, commands, len(requests))
            # Before the growth-aware trigger, the first request admits almost
            # 16k output. Its repeated input makes the second request's warning
            # arrive with 43k left against a 55k finishing reserve. Neither the
            # input nor the output exceeds the admitted estimate.
            turn.message["content"] = "work " * 16_000
            output = prompt_tokens([turn.message], [])
            turn.usage["completion_tokens"] = output
            turn.usage["total_tokens"] = turn.usage["prompt_tokens"] + output
            assert output <= kwargs["max_tokens"]
        else:
            turn = response(
                messages,
                tools,
                [{"command": "done", "runtime": "python", "entry": "main.py"}],
                len(requests),
            )
        assert turn.usage["prompt_tokens"] <= kwargs["input_tokens_bound"]
        return turn

    monkeypatch.setattr(session.runtime, "lint", lint)
    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "submitted", session.error
    assert len(requests) == 2
    assert requests[0]["max_tokens"] == 16_384
    assert warnings(session)[0]["turn"] == 1
    assert warnings(session)[0]["reserve_affordable"]
    assert not any(r["kind"] == "estimate_exceeded" for r in session.budget_decisions)


async def test_active_finishing_uses_its_actual_output_cap_for_context_admission(
    session_factory, monkeypatch
):
    session = session_factory(total_tokens=300_000)
    session.workspace.write("main.py", "print(42)\n")
    session.finishing = True
    session.messages.extend(
        [
            response(session.messages, session.tools, [{"command": "lint"}]).message,
            {"role": "tool", "tool_call_id": "0-0", "content": '{"ok":true}'},
        ]
    )
    # There is no older removable history. The configured 4096-token request fits
    # a 128k context, while incorrectly reserving the default 64000 tokens would
    # force an impossible compaction before the model can call done.
    local = prompt_tokens(session.messages, session.tools)
    session.prompt_estimate.observe(local, {"prompt_tokens": 115_000})
    monkeypatch.setitem(module.api._MODEL_CONTEXT_CACHE, session.model_id, 128_000)

    async def model(client, key, model_id, messages, tools, **kwargs):
        assert model_id == session.model_id
        assert kwargs["input_tokens_bound"] + kwargs["max_tokens"] < 128_000
        return response(
            messages,
            tools,
            [{"command": "done", "runtime": "python", "entry": "main.py"}],
            usage={"prompt_tokens": 115_000, "completion_tokens": 100, "total_tokens": 115_100},
        )

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "submitted", session.error
    assert not any(t["phase"] == "compacting" for t in session.turns)


@pytest.mark.parametrize("actual_input", [2_000, 45_000])
async def test_underestimated_request_cost_is_charged_and_recorded(
    session_factory, monkeypatch, actual_input
):
    session = session_factory()
    session.workspace.write("main.py", "print(42)\n")

    async def model(client, key, model_id, messages, tools, **kwargs):
        assert actual_input > kwargs["input_tokens_bound"]
        return response(
            messages,
            tools,
            [{"command": "done", "runtime": "python", "entry": "main.py"}],
            usage={
                "prompt_tokens": actual_input,
                "completion_tokens": 100,
                "total_tokens": actual_input + 100,
            },
        )

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.budget_tokens == actual_input + 100
    assert any(
        r["kind"] == "estimate_exceeded" and r["source"] == "request_usage"
        for r in session.budget_decisions
    )
    if actual_input > session.limits.total_tokens:
        assert session.generation == "budget_exhausted"
        assert not session.dispatcher.submission and not session.attempts
        assert session.dispatcher.tool_usage["calls"] == 0
    else:
        assert session.generation == "submitted", session.error


async def test_insufficient_reserve_stays_explicit_and_never_submits_automatically(
    session_factory, monkeypatch
):
    session = session_factory(total_tokens=3_000, build_turns=2)

    async def model(client, key, model_id, messages, tools, **kwargs):
        assert "full finishing sequence no longer fits" in json.dumps(messages)
        assert (
            kwargs["input_tokens_bound"] + kwargs["max_tokens"]
            <= session.limits.total_tokens - session.budget_tokens
        )
        return response(messages, tools, [{"command": "ls"}], len(session.turns))

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert not warnings(session)[0]["reserve_affordable"]
    assert any(r.get("outcome") == "insufficient_reserve" for r in session.budget_decisions)
    assert session.generation == "budget_exhausted"
    assert session.budget_tokens <= session.limits.total_tokens
    assert not session.dispatcher.submission and not session.attempts
    assert len(warnings(session)) == 1


async def test_warning_that_cannot_fit_is_recorded_without_changing_messages(
    session_factory, monkeypatch
):
    session = session_factory(total_tokens=1)
    before = json.loads(json.dumps(session.messages))

    async def model(*args, **kwargs):
        pytest.fail("unaffordable request must not be sent")

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "budget_exhausted"
    assert session.messages == before and session.budget_tokens == 0
    assert any(r["kind"] == "warning_not_deliverable" for r in session.budget_decisions)


@pytest.mark.parametrize("reported", [False, True])
async def test_cancellation_after_warning_charges_usage_and_stops_tools(
    session_factory, monkeypatch, reported
):
    session = session_factory()
    request_bound = 0

    async def model(*args, **kwargs):
        nonlocal request_bound
        request_bound = kwargs["input_tokens_bound"]
        kwargs["on_usage"](
            {"prompt_tokens": 1100, "completion_tokens": 25, "total_tokens": 1125}
            if reported
            else {},
            25,
        )
        raise asyncio.CancelledError()

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "cancelled"
    assert session.budget_tokens == (1125 if reported else request_bound + 25)
    assert session.usage()["total_tokens"] == (1125 if reported else None)
    assert len(warnings(session)) == 1
    assert warnings(session)[0]["status"] == "interrupted"
    assert any(
        r["kind"] == "finishing_request" and r["status"] == "cancelled"
        for r in session.budget_decisions
    )
    assert not session.dispatcher.submission and not session.attempts


async def test_repair_uses_same_warning_and_budget_after_runtime_failure(
    session_factory, monkeypatch
):
    session = session_factory()
    request_count = 0

    async def model(client, key, model_id, messages, tools, **kwargs):
        nonlocal request_count
        request_count += 1
        assert json.dumps(messages).count("[WaveBench budget warning]") == 1
        assert kwargs["max_tokens"] <= 4096
        if request_count in (1, 3):
            commands = [{"command": "write", "path": "main.py", "content": "print(42)\n"}]
        else:
            commands = [{"command": "done", "runtime": "python", "entry": "main.py"}]
        return response(messages, tools, commands, request_count)

    async def setup(descriptor):
        pass

    async def execute(descriptor, attempt):
        attempt.update(outcome="success" if attempt["number"] == 2 else "failed", exit_code=1)

    monkeypatch.setattr(module, "call_conversation", model)
    monkeypatch.setattr(session.runtime, "setup", setup)
    monkeypatch.setattr(session.runtime, "execute", execute)
    await session.build()
    await session.execute()
    assert session.status == "success", session.error
    assert session.repair == "submitted" and len(session.attempts) == 2
    assert len(warnings(session)) == 1 and request_count == 4
    assert any(r["kind"] == "repair" for r in session.budget_decisions)
    assert session.budget_tokens == sum(t["usage"]["total_tokens"] for t in session.turns)
    assert session.limits.total_tokens == 40_000


async def test_large_tool_result_preserves_evidence_and_records_estimate_overrun(
    session_factory, monkeypatch
):
    session = session_factory(build_turns=2)
    large = "0123456789 " * 1800
    session.workspace.write("large.txt", large)

    async def model(client, key, model_id, messages, tools, **kwargs):
        return response(
            messages, tools, [{"command": "read", "path": "large.txt"}], len(session.turns)
        )

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert any(
        r["kind"] == "estimate_exceeded" and r["source"] == "tool_results"
        for r in session.budget_decisions
    )
    saved = json.loads((session.metadata / "tool-0001.json").read_text())
    assert large.rstrip() in json.dumps(saved)
    assert len(warnings(session)) == 1 and not session.dispatcher.submission


async def test_repair_cannot_replenish_budget_consumed_by_initial_submission(
    session_factory, monkeypatch
):
    session = session_factory()
    session.workspace.write("main.py", "print(42)\n")
    requests = 0

    async def model(client, key, model_id, messages, tools, **kwargs):
        nonlocal requests
        requests += 1
        return response(
            messages,
            tools,
            [{"command": "done", "runtime": "python", "entry": "main.py"}],
            usage={"prompt_tokens": 37_900, "completion_tokens": 100, "total_tokens": 38_000},
        )

    async def setup(descriptor):
        pass

    async def execute(descriptor, attempt):
        attempt.update(outcome="failed", exit_code=1)

    monkeypatch.setattr(module, "call_conversation", model)
    monkeypatch.setattr(session.runtime, "setup", setup)
    monkeypatch.setattr(session.runtime, "execute", execute)
    await session.build()
    await session.execute()
    assert session.status == "failed" and session.repair == "budget_exhausted"
    assert session.budget_tokens == 38_000 and session.limits.total_tokens == 40_000
    assert requests == 1 and len(session.attempts) == 1
    assert len(warnings(session)) == 1
    assert any(
        r["kind"] == "request_blocked" and r["phase"] == "repairing"
        for r in session.budget_decisions
    )


def test_reserve_includes_repeated_input_warning_response_and_tool_result():
    assert finish_reserve(80_000, 16_384) == 2 * (80_000 + 512) + 2 * 16_384 + input_growth(16_384)
    assert finish_reserve(80_000, 1024, 2000) == (
        2 * (80_000 + 512) + 2 * 1024 + input_growth(1024, 2000)
    )


def fake_clock(monkeypatch, durations):
    """Advance the controller's active-time clock by each model response's duration."""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        module, "time", SimpleNamespace(monotonic=lambda: clock.now, time=time.time)
    )

    def respond(requests):
        clock.now += durations[len(requests) - 1]

    return respond


async def test_slow_responses_get_a_time_warning_before_the_phase_deadline(
    session_factory, monkeypatch
):
    # MiMo V2.6 Pro: 303, 360 and 704 second responses used the 1,800 second build
    # phase without a warning, and its streaming fourth response was discarded.
    session = session_factory(total_tokens=1_000_000)
    session.workspace.write("main.py", "print(42)\n")
    elapse = fake_clock(monkeypatch, [303, 360, 704, 40])
    requests = []

    async def model(client, key, model_id, messages, tools, **kwargs):
        requests.append(json.loads(json.dumps(messages)))
        elapse(requests)
        if len(requests) < 4:
            commands = [{"command": "write", "path": f"part{len(requests)}.py", "content": "X=1\n"}]
        else:
            commands = [{"command": "done", "runtime": "python", "entry": "main.py"}]
        return response(messages, tools, commands, len(requests))

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "submitted", session.error
    assert "at most 32 model requests and 1800 active seconds" in requests[0][0]["content"]
    assert all("[WaveBench budget" not in json.dumps(r) for r in requests[:3])
    warning = requests[3][-1]["content"]
    assert warning.startswith("[WaveBench budget warning]")
    assert (
        "About 433 active seconds remain in this phase; recent responses took up to "
        "704 seconds, and a response still streaming at the limit is discarded." in warning
    )
    assert "The full finishing sequence no longer fits the estimate" in warning
    [record] = warnings(session)
    assert record["triggers"] == ["time"]
    assert record["seconds_left"] == 433 and record["time_reserve_seconds"] == 1438
    assert record["time_affordable"] is False


async def test_time_running_low_after_a_token_warning_gets_one_reminder_per_phase(
    session_factory, monkeypatch
):
    session = session_factory()
    session.workspace.write("main.py", "print(42)\n")
    elapse = fake_clock(monkeypatch, [500, 500, 20, 20])
    requests = []

    async def model(client, key, model_id, messages, tools, **kwargs):
        requests.append(json.loads(json.dumps(messages)))
        elapse(requests)
        if len(requests) < 4:
            commands = [{"command": "read", "path": "main.py"}]
        else:
            commands = [{"command": "done", "runtime": "python", "entry": "main.py"}]
        return response(messages, tools, commands, len(requests))

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "submitted", session.error
    assert [r["triggers"] for r in warnings(session)] == [["tokens"]]
    assert requests[0][-1]["content"].startswith("[WaveBench budget warning]")
    assert requests[1][-1]["role"] == "tool"
    assert requests[2][-1]["content"].startswith(
        "[WaveBench budget reminder] Active time is running low. 30 model requests remain "
        "in this phase. About 800 active seconds remain in this phase"
    )
    assert "The finishing warning still applies" in requests[2][-1]["content"]
    assert requests[3][-1]["role"] == "tool"
    assert json.dumps(requests[3]).count("[WaveBench budget reminder]") == 1
    notices = [r for r in session.budget_decisions if r["kind"] == "budget_notice"]
    assert [(r["reason"], r["outcome"]) for r in notices] == [("time", "delivered")]


async def test_final_request_repeats_the_submission_instruction(session_factory, monkeypatch):
    # Gemini 3.8 Flash kept re-reading files after its warning until the turn limit.
    session = session_factory(total_tokens=1_000_000, build_turns=3)
    session.workspace.write("main.py", "print(42)\n")
    requests = []

    async def model(client, key, model_id, messages, tools, **kwargs):
        requests.append(json.loads(json.dumps(messages)))
        final = (messages[-1].get("content") or "").startswith("[WaveBench budget reminder]")
        commands = (
            [{"command": "done", "runtime": "python", "entry": "main.py"}]
            if final
            else [{"command": "read", "path": "main.py"}]
        )
        return response(messages, tools, commands, len(requests))

    monkeypatch.setattr(module, "call_conversation", model)
    await session.build()
    assert session.generation == "submitted", session.error
    assert len(requests) == 3
    assert [r["triggers"] for r in warnings(session)] == [["turns"]]
    assert requests[1][-1]["content"].startswith("[WaveBench budget warning]")
    assert requests[2][-1]["content"].startswith(
        "[WaveBench budget reminder] This is the final model request in this phase; "
        "no response follows its tool results."
    )
    assert "Call wb done alone with runtime and entry now" in requests[2][-1]["content"]
