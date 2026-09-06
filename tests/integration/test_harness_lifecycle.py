from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys

import pytest

from wavebench.harness import session as module
from wavebench.harness.config import Limits
from wavebench.harness.session import HarnessBatch, HarnessSession
from wavebench.harness.transport import Turn, TurnError
from wavebench.harness.workspace import allocate_run


@pytest.fixture
async def factory(tmp_path, monkeypatch):
    if sys.platform != "linux" or not shutil.which("bwrap"):
        if os.getenv("WAVEBENCH_REQUIRE_SANDBOX_TESTS"):
            pytest.fail("bwrap is required")
        pytest.skip("requires Linux and bwrap")

    async def capable(*args):
        return True

    monkeypatch.setattr(module, "capability", capable)
    monkeypatch.setattr(module.webbrowser, "open", lambda url: True)
    run = allocate_run(tmp_path, "lifecycle", "test")
    sessions = []
    api_slots = asyncio.Semaphore(1)
    process_slots = asyncio.Semaphore(2)

    def create(name="model", **kwargs):
        instance = HarnessSession(
            run,
            len(sessions) + 1,
            name,
            f"vendor/{name}",
            "Build a two-file project",
            None,
            "offline",
            kwargs.pop("limits", Limits(review_seconds=1)),
            api_slots,
            process_slots,
            **kwargs,
        )
        sessions.append(instance)
        return instance

    yield create
    for session in sessions:
        await session.close()


def scripted(
    monkeypatch,
    *,
    fail_first=False,
    fail_second=False,
    abandon=False,
    delay=None,
    unknown_usage=False,
):
    calls = {}
    conversations = {}

    async def model(client, api_key, model_id, messages, tools, **kwargs):
        index = calls.get(model_id, 0)
        calls[model_id] = index + 1
        conversations.setdefault(model_id, []).append(json.loads(json.dumps(messages)))
        if delay and index == 0:
            await asyncio.sleep(delay.get(model_id, 0))
        if index == 0:
            command = [
                {
                    "command": "write",
                    "path": "lib/helper.py",
                    "content": "def value():\n  return 42\n",
                },
                {
                    "command": "write",
                    "path": "main.py",
                    "content": "raise RuntimeError('intentional first failure')"
                    if fail_first
                    else "from lib.helper import value\nprint(value())",
                },
            ]
        elif index == 1:
            command = [{"command": "lint"}]
        elif index in {2, 5}:
            command = [{"command": "done", "runtime": "python", "entry": "main.py"}]
        elif index == 3:
            if abandon:
                return Turn(
                    {"role": "assistant", "content": "I cannot fix this"},
                    {},
                    model_id,
                    "offline",
                    "stop",
                    {},
                )
            command = [
                {
                    "command": "write",
                    "path": "main.py",
                    "content": "raise RuntimeError('second failure')"
                    if fail_second
                    else "from lib.helper import value\nprint(value())",
                }
            ]
        else:
            command = [{"command": "lint"}]
        message = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": f"{model_id}-{index}-{i}",
                    "type": "function",
                    "function": {"name": "wb", "arguments": json.dumps(args)},
                }
                for i, args in enumerate(command)
            ],
        }
        usage = (
            {}
            if unknown_usage
            else {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.001}
        )
        return Turn(message, usage, model_id, "offline-provider", "tool_calls", {})

    monkeypatch.setattr(module, "call_conversation", model)
    return calls, conversations


@pytest.mark.parametrize(
    "fail_first,fail_second,expected", [(False, False, 1), (True, False, 2), (True, True, 2)]
)
async def test_attempt_invariant_and_usage_across_repair(
    factory, monkeypatch, fail_first, fail_second, expected
):
    calls, conversations = scripted(monkeypatch, fail_first=fail_first, fail_second=fail_second)
    session = factory(auto_open="off")
    await session.build()
    assert len(session.attempts) == 0 and session.generation == "submitted"
    await asyncio.gather(session.execute(), session.execute(), session.execute())
    assert len(session.attempts) == expected
    assert session.status == ("failed" if fail_second else "success")
    assert calls[session.model_id] == (6 if fail_first else 3)
    assert session.usage()["total_tokens"] == calls[session.model_id] * 15
    assert session.usage()["cost"] == pytest.approx(calls[session.model_id] * 0.001)
    if fail_first:
        repair_message = conversations[session.model_id][3][-1]
        assert (
            repair_message["role"] == "user"
            and "intentional first failure" in repair_message["content"]
        )
        assert len(conversations[session.model_id][3]) > len(conversations[session.model_id][2])
        assert session.attempts[0]["finished_at"] <= session.attempts[1]["started_at"]
    stored = json.loads((session.metadata / "result.json").read_text())
    assert len(stored["harness"]["attempts"]) == expected
    assert stored["workspace"] == str(session.workspace.root)


async def test_abandoned_repair_keeps_failure_without_fabricated_retry(factory, monkeypatch):
    scripted(monkeypatch, fail_first=True, abandon=True)
    session = factory()
    await session.build()
    await session.execute()
    assert session.status == "failed" and len(session.attempts) == 1
    assert session.repair == "abandoned" and "abandoned" in session.error
    assert session.usage()["total_tokens"] is None


@pytest.mark.parametrize("policy", ["off", "incremental", "after_all"])
async def test_initial_generation_barrier_releases_api_slots(factory, monkeypatch, policy):
    scripted(monkeypatch, delay={"vendor/slow": 0.35})
    fast, slow = factory("fast", auto_open=policy), factory("slow", auto_open=policy)
    fast.api_slots = slow.api_slots = asyncio.Semaphore(2)
    results = {}
    await HarnessBatch([fast, slow], policy, results).run()
    assert set(results) == {"fast", "slow"}
    assert all(result["status"] == "success" for result in results.values())
    slow_submitted = next(event["timestamp"] for event in slow.events if event["phase"] == "queued")
    if policy == "after_all":
        assert fast.attempts[0]["started_at"] >= slow_submitted
        assert fast.queue_seconds > 0
    else:
        assert fast.attempts[0]["started_at"] < slow_submitted


async def test_barrier_advances_with_unsupported_model_and_unknown_usage(factory, monkeypatch):
    scripted(monkeypatch, unknown_usage=True)

    async def capable(client, api_key, model_id):
        return model_id != "vendor/unsupported"

    monkeypatch.setattr(module, "capability", capable)
    fast, unsupported = factory("fast"), factory("unsupported")
    results = {}
    await HarnessBatch([fast, unsupported], "after_all", results).run()
    assert fast.status == "success" and fast.usage()["total_tokens"] is None
    assert unsupported.generation == "unsupported" and not unsupported.turns
    assert not unsupported.attempts


async def test_budget_limits_and_lint_failure_do_not_unlock_execution(factory, monkeypatch):
    scripted(monkeypatch)
    session = factory(limits=Limits(build_turns=1))
    await session.build()
    await session.execute()
    assert session.generation == "budget_exhausted" and session.attempts == []
    assert session.workspace.read("main.py")


async def test_failed_spawn_counts_one_admitted_attempt(factory, monkeypatch):
    scripted(monkeypatch, abandon=True)
    session = factory()
    await session.build()
    original = session.runtime.spawn

    async def spawn(command, label, **kwargs):
        if label.startswith("run-"):
            raise OSError("process startup failed")
        return await original(command, label, **kwargs)

    monkeypatch.setattr(session.runtime, "spawn", spawn)
    await session.execute()
    assert len(session.attempts) == 1 and session.attempts[0]["outcome"] == "failed"
    assert "startup failed" in session.attempts[0]["diagnostics"]


async def test_cancellation_during_repair_keeps_first_failure(factory, monkeypatch):
    scripted(monkeypatch, fail_first=True)
    model = module.call_conversation
    repairing = asyncio.Event()

    async def pause(*args, **kwargs):
        if args[3][-1].get("role") == "user" and "run 1 failed" in args[3][-1].get("content", ""):
            repairing.set()
            await asyncio.Event().wait()
        return await model(*args, **kwargs)

    monkeypatch.setattr(module, "call_conversation", pause)
    session = factory()
    await session.build()
    task = asyncio.create_task(session.execute())
    await asyncio.wait_for(repairing.wait(), 5)
    task.cancel()
    await task
    await session.execute()
    assert session.status == "cancelled" and len(session.attempts) == 1
    assert session.repair == "cancelled"


async def test_malformed_generation_preserves_usage_without_tool_execution(factory, monkeypatch):
    async def model(*args, **kwargs):
        raise TurnError("incomplete tool arguments", {"total_tokens": 7})

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory()
    await session.build()
    assert session.status == "failed" and not session.attempts
    assert session.usage()["total_tokens"] == 7
    assert session.workspace.ls() == []
