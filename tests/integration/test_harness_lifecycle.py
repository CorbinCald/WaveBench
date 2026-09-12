from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time

import pytest

from wavebench.harness import handoff
from wavebench.harness import session as module
from wavebench.harness.commands import launch_descriptor
from wavebench.harness.config import Limits
from wavebench.harness.context import COMPACTION_MODEL
from wavebench.harness.session import HarnessBatch, HarnessSession
from wavebench.harness.transport import Turn, TurnError
from wavebench.harness.workspace import allocate_run
from wavebench.tokens import prompt_tokens
from wavebench.tui.progress import ProgressTracker


@pytest.mark.parametrize("enabled", [False, True])
async def test_search_results_reach_agent_and_generated_program(factory, monkeypatch, enabled):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from wavebench import web_search

    requests = []

    async def search(request):
        requests.append(dict(request.query))
        assert request.headers["X-Subscription-Token"] == "test-private-brave-key"
        return web.json_response(
            {
                "web": {
                    "results": [
                        {
                            "title": "Reference",
                            "url": "https://example.com/reference",
                            "description": "Use 42.",
                        }
                    ]
                }
            }
        )

    app = web.Application()
    app.router.add_get("/search", search)
    turns = 0
    tracker = ProgressTracker(1, {})
    tracker._running = True

    async def model(client, key, model_id, messages, tools, **kwargs):
        nonlocal turns
        assert ("web_search" in [tool["function"]["name"] for tool in tools]) == enabled
        assert "test-private-brave-key" not in json.dumps({"messages": messages, "tools": tools})
        name = "wb"
        if enabled and turns == 0:
            assert tracker._harness_metrics(session.name)["web_searches"] == 0
            name, args = "web_search", {"query": "example reference"}
        elif turns == int(enabled):
            if enabled:
                assert tracker._harness_metrics(session.name)["web_searches"] == 1
                result = json.loads(messages[-1]["content"])
                assert result["results"][0]["url"] == "https://example.com/reference"
            args = {
                "command": "write",
                "path": "main.py",
                "content": (
                    "import os\nassert 'BRAVE_SEARCH_API_KEY' not in os.environ\nprint(42)\n"
                ),
            }
        else:
            args = {"command": "done", "runtime": "python", "entry": "main.py"}
        turns += 1
        return Turn(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": str(turns),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            },
            {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            model_id,
            "offline",
            "tool_calls",
            {},
        )

    monkeypatch.setattr(module, "call_conversation", model)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-private-brave-key")
    async with TestServer(app) as server:
        monkeypatch.setattr(web_search, "BRAVE_URL", str(server.make_url("/search")))
        session = factory(
            auto_open="off",
            tracker=tracker,
            web_search=(web_search.BraveSearch("test-private-brave-key") if enabled else None),
        )
        await session.build()
        await session.execute()
    assert session.status == "success", session.error
    assert len(requests) == int(enabled)
    assert tracker._harness_metrics(session.name)["web_searches"] == (1 if enabled else None)
    assert session.result()["harness"]["web_search"] == {
        "enabled": enabled,
        "provider": "brave" if enabled else None,
        "calls": int(enabled),
        "failures": 0,
    }
    assert "test-private-brave-key" not in "".join(
        path.read_text() for path in session.metadata.glob("*.json")
    )


async def test_live_search_counts_include_failures_and_survive_replay_and_repair(factory):
    from wavebench.web_search import SearchError

    class Search:
        async def search(self, query, count):
            if query == "fail":
                raise SearchError("Brave rate limit or quota reached; try again later.")
            return {"results": []}

    tracker = ProgressTracker(1, {})
    tracker._running = True
    session = factory(web_search=Search(), tracker=tracker)
    session.phase("building")
    assert "web searches 0" in tracker._format_harness_tool_metrics(session.name)
    call = {"name": "web_search", "id": "first", "arguments": {"query": "docs"}}
    await session.dispatcher.batch([call])
    assert "web searches 1" in tracker._format_harness_tool_metrics(session.name)
    await session.dispatcher.batch([call])
    assert "web searches 1" in tracker._format_harness_tool_metrics(session.name)
    await session.dispatcher.batch([{**call, "id": "second", "arguments": {"query": "fail"}}])
    assert "web searches 2" in tracker._format_harness_tool_metrics(session.name)
    session.phase("repairing")
    session.dispatcher.reopen()
    await session.dispatcher.batch([{**call, "id": "third"}])
    session.phase("finished")
    assert "web searches 3" in tracker._format_harness_tool_metrics(session.name, session.result())
    assert tracker._harness[session.name]["web_search"] == {
        "enabled": True,
        "calls": 3,
        "failures": 1,
    }


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
            preview_destination=kwargs.pop("preview_destination", "host"),
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
        elif index == 3 or (abandon and index > 3):
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
    tracker = ProgressTracker(1, {})
    tracker._running = True
    session = factory(auto_open="off", tracker=tracker)
    original = module.call_conversation
    live_turns = []

    async def streaming(*args, **kwargs):
        kwargs["on_progress"](400)
        live_turns.append(tracker._format_harness_metrics(session.name))
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, "call_conversation", streaming)
    await session.build()
    assert len(session.attempts) == 0 and session.generation == "submitted"
    await asyncio.gather(session.execute(), session.execute(), session.execute())
    assert len(session.attempts) == expected
    assert session.status == ("failed" if fail_second else "success")
    assert calls[session.model_id] == (6 if fail_first else 3)
    assert session.usage()["total_tokens"] == calls[session.model_id] * 15
    assert session.usage()["cost"] == pytest.approx(calls[session.model_id] * 0.001)
    count = calls[session.model_id]
    assert all(f"{i} turn" in row for i, row in enumerate(live_turns, 1))
    metrics = tracker._format_harness_metrics(session.name)
    assert f"{count * 5} tk" in metrics and f"{count} turns" in metrics
    assert f"$0.00{count}" in metrics
    assert tracker._harness[session.name]["api_s"] == session.api_seconds
    tool_usage = {"calls": 7 if fail_first else 4, "failures": 0}
    assert tracker._harness[session.name]["tool_usage"] == tool_usage
    assert "tool fail 0.0%" in tracker._format_harness_tool_metrics(session.name)
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
    assert stored["harness"]["tool_usage"] == tool_usage


async def test_abandoned_repair_keeps_failure_without_fabricated_retry(factory, monkeypatch):
    scripted(monkeypatch, fail_first=True, abandon=True)
    session = factory()
    await session.build()
    await session.execute()
    assert session.status == "failed" and len(session.attempts) == 1
    assert session.repair == "abandoned" and "abandoned" in session.error
    assert session.usage()["total_tokens"] is None


@pytest.mark.parametrize("policy", ["off", "incremental", "after_all"])
@pytest.mark.parametrize("opened", [True, False])
async def test_preview_log_path_and_launch_failure_preserve_runtime_success(
    factory, monkeypatch, capsys, policy, opened
):
    launches = []

    def open_browser(url, log_path):
        launches.append(url)
        log_path.write_text("browser diagnostic\n")
        return opened

    monkeypatch.setattr(module, "open_preview", open_browser)
    session = factory(auto_open=policy)
    session.workspace.write("index.html", "<!doctype html><h1>Preview ready</h1>")
    session.descriptor = launch_descriptor(
        {"runtime": "static", "entry": "index.html"}, session.workspace
    )
    session.generation = "submitted"
    session.submitted_at = time.monotonic()
    await session.execute()

    stored = json.loads((session.metadata / "result.json").read_text())
    assert stored["status"] == "success"
    attempt = stored["harness"]["attempts"][0]
    assert attempt["outcome"] == "success"
    if policy == "off":
        assert not launches
        assert "browser_log" not in attempt
        assert not (session.metadata / "browser.log").exists()
    else:
        assert launches == [attempt["preview_url"]]
        assert attempt["browser_log"] == str(session.metadata / "browser.log")
        assert ("presentation_error" in attempt) == (not opened)
        await HarnessBatch([session], policy, {}).review()
        output = capsys.readouterr().out
        assert "browser diagnostic" not in output
        assert attempt["preview_url"] in output
        if not opened:
            assert "browser unavailable" in output
            assert attempt["browser_log"] in output


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


async def test_timeout_preserves_usage_already_received_from_stream(factory, monkeypatch):
    async def model(*args, **kwargs):
        kwargs["on_usage"](
            {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cost": 0.5}, 10
        )
        raise asyncio.TimeoutError()

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory()
    await session.build()
    assert session.usage()["api_turns"] == 1
    assert session.usage()["total_tokens"] == 30
    assert session.usage()["cost"] == 0.5


async def test_local_request_rejection_does_not_add_a_turn(factory, monkeypatch):
    async def model(*args, **kwargs):
        raise TurnError("no request sent", request_sent=False)

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory()
    await session.build()
    assert session.usage()["api_turns"] == 0
    assert session.budget_tokens == 0


async def test_reported_zero_usage_does_not_consume_an_estimated_budget(factory, monkeypatch):
    scripted(monkeypatch)
    original = module.call_conversation

    async def model(*args, **kwargs):
        turn = await original(*args, **kwargs)
        turn.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0}
        return turn

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory()
    await session.build()
    assert session.generation == "submitted"
    assert session.usage()["api_turns"] == 3
    assert session.usage()["total_tokens"] == session.budget_tokens == 0
    assert session.usage()["cost"] == 0


async def test_failure_before_next_stream_does_not_reuse_previous_turn_usage(factory, monkeypatch):
    scripted(monkeypatch)
    original = module.call_conversation
    called = False

    async def model(*args, **kwargs):
        nonlocal called
        if called:
            raise asyncio.TimeoutError()
        called = True
        turn = await original(*args, **kwargs)
        kwargs["on_usage"](turn.usage, 5)
        return turn

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory()
    await session.build()
    usage = session.usage()
    assert usage["api_turns"] == 2
    assert usage["cost"] is None and usage["known_cost"] == 0.001
    assert usage["total_tokens"] is None and usage["known_total_tokens"] == 15
    assert session.turns[-1]["usage"] == {}


async def test_measured_prompt_usage_keeps_large_projects_within_budget(factory, monkeypatch):
    requests = []

    async def model(client, api_key, model_id, messages, tools, **kwargs):
        index = len(requests)
        requests.append(kwargs)
        if index == 0:
            command = {"command": "write", "path": "main.py", "content": "value = 'hello'\n" * 3000}
        elif index < 5:
            command = {"command": "ls"}
        else:
            command = {"command": "done", "runtime": "python", "entry": "main.py"}
        message = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": f"call-{index}",
                    "type": "function",
                    "function": {"name": "wb", "arguments": json.dumps(command)},
                }
            ],
        }
        usage = {
            "prompt_tokens": 1000 if index == 0 else 14000,
            "completion_tokens": 12000 if index == 0 else 100,
        }
        usage["total_tokens"] = sum(usage.values())
        return Turn(message, usage, model_id, "offline-provider", "tool_calls", {})

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(limits=Limits(total_tokens=100_000))
    await session.build()
    assert session.generation == "submitted", session.error
    assert len(requests) == 6
    assert session.budget_tokens == 83_500
    assert session.turns[-1]["input_tokens_bound"] < 16_000
    assert 0 < requests[-1]["max_tokens"] <= session.limits.turn_tokens
    used_before_last = sum(turn["usage"]["total_tokens"] for turn in session.turns[:-1])
    assert (
        used_before_last + requests[-1]["input_tokens_bound"] + requests[-1]["max_tokens"]
        <= session.limits.total_tokens
    )
    assert not session.attempts


async def test_token_limit_identifies_usage_and_next_request_reserve(factory, monkeypatch):
    calls, _ = scripted(monkeypatch)
    original = module.call_conversation

    async def model(*args, **kwargs):
        turn = await original(*args, **kwargs)
        turn.usage = {"prompt_tokens": 8500, "completion_tokens": 500, "total_tokens": 9000}
        return turn

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(limits=Limits(total_tokens=10_000))
    await session.build()
    assert session.generation == "budget_exhausted"
    assert calls[session.model_id] == 1
    assert "9,000 / 10,000 tokens used" in session.error
    assert "next input estimate" in session.error and "time" not in session.error
    assert not session.attempts


@pytest.mark.parametrize("repair", [False, True])
async def test_active_deadline_names_the_phase_and_time_limit(factory, monkeypatch, repair):
    scripted(monkeypatch, fail_first=repair)
    original = module.call_conversation

    async def model(*args, **kwargs):
        last = args[3][-1]
        if not repair or (last.get("role") == "user" and "run 1 failed" in last.get("content", "")):
            await asyncio.sleep(2)
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(limits=Limits(build_seconds=1, repair_seconds=1))
    await session.build()
    await session.execute()
    phase = "repairing" if repair else "building"
    assert f"{phase} active time budget exhausted" in session.error
    assert "/ 1s)" in session.error
    assert len(session.attempts) == (1 if repair else 0)


def seed_context(session):
    session.messages.extend(
        [
            {"role": "assistant", "content": "Earlier implementation notes. " * 1000},
            {"role": "user", "content": "Keep the helper module and print 42."},
            {
                "role": "assistant",
                "content": "Latest full answer",
                "tool_calls": [
                    {
                        "id": "old-ls",
                        "type": "function",
                        "function": {"name": "wb", "arguments": '{"command":"ls"}'},
                    }
                ],
                "reasoning_details": [{"type": "reasoning.encrypted", "data": "retain-exactly"}],
            },
            {"role": "tool", "tool_call_id": "old-ls", "content": "[]"},
        ]
    )
    # Provider-calibrated context exercises the automatic threshold without a
    # giant fixture in each test. Unit/live tests also cover actual token sizes.
    session.prompt_estimate.observe(
        prompt_tokens(session.messages, module.TOOL_SCHEMA), {"prompt_tokens": 240_001}
    )
    return json.loads(json.dumps(session.messages))


async def test_compaction_then_build_and_repair_preserves_history_budget_and_two_run_rule(
    factory, monkeypatch
):
    calls, conversations = scripted(monkeypatch, fail_first=True)
    original = module.call_conversation
    requests = []

    async def model(client, key, model_id, messages, tools, **kwargs):
        if model_id != COMPACTION_MODEL:
            return await original(client, key, model_id, messages, tools, **kwargs)
        requests.append(kwargs)
        assert tools == []
        return Turn(
            {"role": "assistant", "content": "Keep lib/helper.py. Main must print 42."},
            {
                "prompt_tokens": 5000,
                "completion_tokens": 500,
                "total_tokens": 5500,
                "cost": 0.002,
                "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            },
            COMPACTION_MODEL,
            "OpenAI",
            "stop",
            {},
        )

    monkeypatch.setattr(module, "call_conversation", model)
    tracker = ProgressTracker(1, {})
    tracker._running = True
    session = factory(limits=Limits(total_tokens=900_000), auto_open="off", tracker=tracker)
    before = seed_context(session)
    key = session.cache_policy.key
    await session.build()
    await session.execute()
    assert session.status == "success", session.error
    assert len(session.attempts) == 2 and calls[session.model_id] == 6
    assert len(requests) == 1
    assert requests[0]["reasoning_effort"] == "high" and requests[0]["strict_reasoning"]
    assert requests[0]["cache_reuse"] is False
    resumed = conversations[session.model_id][0]
    assert resumed[:2] == before[:2] and resumed[-2:] == before[-2:]
    assert session.cache_policy.key == key
    assert session.budget_tokens == 5500 + 6 * 15
    result = session.result()
    assert result["usage"]["cost"] == pytest.approx(0.008)
    assert result["harness"]["model_usage"]["total_tokens"] == 90
    assert result["harness"]["compaction"]["usage"]["total_tokens"] == 5500
    metrics = tracker._format_harness_metrics(session.name)
    assert "530 tk" in metrics and "$0.008" in metrics and "7 turns" in metrics
    assert tracker._harness[session.name]["api_s"] == session.api_seconds
    assert session.compaction_seconds > 0
    assert session.build_seconds >= session.compaction_seconds
    record = session.compactions[0]
    assert record["status"] == "completed" and record["after_tokens"] < 240_000
    assert json.loads((session.metadata / record["archive"]).read_text()) == before
    saved = json.loads((session.metadata / "compaction-001.json").read_text())
    assert saved["response"]["content"].startswith("Keep lib")


@pytest.mark.parametrize(
    "failure", ["empty", "truncated", "tools", "wrong_model", "timeout", "cancelled"]
)
async def test_failed_compaction_never_replaces_or_executes_original_context(
    factory, monkeypatch, failure
):
    async def model(*args, **kwargs):
        if failure == "timeout":
            await asyncio.sleep(2)
        if failure == "cancelled":
            raise asyncio.CancelledError()
        return Turn(
            {
                "role": "assistant",
                "content": "" if failure == "empty" else "Summary",
                **({"tool_calls": [{"id": "malicious"}]} if failure == "tools" else {}),
            },
            {"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020},
            "other/model" if failure == "wrong_model" else COMPACTION_MODEL,
            "OpenAI",
            "length" if failure == "truncated" else "stop",
            {},
        )

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(limits=Limits(total_tokens=900_000, build_seconds=1))
    before = seed_context(session)
    await session.build()
    if failure == "cancelled":
        assert session.generation == "cancelled"
    assert session.messages == before
    assert session.compactions[0]["status"] == "failed"
    assert session.budget_tokens > 0
    assert not session.attempts and not session.workspace.ls()
    if failure not in {"timeout", "cancelled"}:
        assert session.usage()["total_tokens"] == 1020
    if failure == "timeout":
        assert "active time budget exhausted" in session.error


async def test_compaction_cannot_bypass_total_budget(factory, monkeypatch):
    calls, _ = scripted(monkeypatch)
    session = factory(limits=Limits(total_tokens=2000))
    before = seed_context(session)
    await session.build()
    assert session.generation == "budget_exhausted"
    assert "cannot fit Luna context compaction" in session.error
    assert not calls and not session.compactions and session.messages == before


async def test_cache_usage_totals_use_actual_reports_and_costs(factory):
    session = factory()
    session.turns = [
        {
            "phase": "building",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "prompt_tokens_details": {"cached_tokens": cached, "cache_write_tokens": writes},
                "cost": 0.01,
            },
        }
        for cached, writes in [(0, 100), (100, 0)]
    ]
    usage = session.usage()
    assert usage["cache_read_ratio"] == 0.5
    assert usage["prompt_tokens_details"] == {"cached_tokens": 100, "cache_write_tokens": 100}
    assert usage["cost"] == 0.02
    session.turns.append({"phase": "compacting", "usage": {}})
    usage = session.usage()
    assert usage["cache_read_ratio"] is None and usage["cost"] is None
    assert usage["known_total_tokens"] == 220
    assert usage["known_cost"] == 0.02
    from wavebench.tui.analytics.cost import compute_cost

    assert compute_cost(usage, {"prompt": "0.01", "completion": "0.03"}) is None


async def test_repeated_compaction_carries_previous_summary_and_newest_tool_tail(
    factory, monkeypatch
):
    requests = []

    async def model(client, key, model_id, messages, tools, **kwargs):
        requests.append(json.loads(messages[1]["content"]))
        return Turn(
            {
                "role": "assistant",
                "content": "Persistent fact: keep helper.py; implement newest correction.",
            },
            {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050},
            COMPACTION_MODEL,
            "OpenAI",
            "stop",
            {},
        )

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(limits=Limits(total_tokens=900_000))
    original = seed_context(session)
    await session.compact("test threshold", 240_001, 10)
    session.messages.extend(
        [
            {"role": "user", "content": "Newest correction: print 43"},
            {
                "role": "assistant",
                "content": "New final response",
                "tool_calls": [
                    {
                        "id": "latest",
                        "type": "function",
                        "function": {"name": "wb", "arguments": '{"command":"ls"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "latest", "content": "[]"},
        ]
    )
    before_second = json.loads(json.dumps(session.messages))
    await session.compact("test threshold", 240_001, 10)
    assert session.messages[:2] == original[:2]
    assert session.messages[-2:] == before_second[-2:]
    assert "Persistent fact" in json.dumps(requests[1]["history_to_summarize"])
    assert "Newest correction" in json.dumps(requests[1]["history_to_summarize"])
    assert len(session.compactions) == 2 and session.budget_tokens == 2100
    assert (
        json.loads((session.metadata / "conversation-before-compaction-002.json").read_text())
        == before_second
    )


async def test_ineffective_compaction_retains_oversized_protected_message(factory, monkeypatch):
    async def model(*args, **kwargs):
        return Turn(
            {"role": "assistant", "content": "Summary"}, {}, COMPACTION_MODEL, "OpenAI", "stop", {}
        )

    monkeypatch.setattr(module, "call_conversation", model)
    session = factory(limits=Limits(total_tokens=900_000))
    seed_context(session)
    session.messages[1]["content"] = "preserve " * 241_000
    before = json.loads(json.dumps(session.messages))
    await session.build()
    assert session.generation == "budget_exhausted"
    assert "cannot fit preserved messages" in session.error
    assert session.messages == before and not session.attempts


@pytest.fixture
def review_helper(tmp_path, monkeypatch):
    """Use the real companion when supplied locally, a pipe-protocol fixture in CI."""
    bin_dir = tmp_path / "review-bin"
    bin_dir.mkdir()
    helper = bin_dir / "herdr-review"
    external = os.environ.get("WAVEBENCH_TEST_REVIEW_HELPER")
    if external:
        helper.symlink_to(external)
    else:
        helper.write_text("""#!/usr/bin/env python3
import argparse,json,os,pathlib,select,sys,time,uuid
parser=argparse.ArgumentParser()
parser.add_argument('action', choices=['offer','status'])
parser.add_argument('--session')
parser.add_argument('--ttl',type=int,default=900)
parser.add_argument('--url')
args=parser.parse_args()
if args.action=='status':
    print(json.dumps({'client':{}}))
    sys.exit(0)
root=pathlib.Path(os.environ['XDG_STATE_HOME'])/'herdr-review/remote'/args.session
root.mkdir(parents=True,exist_ok=True)
token=uuid.uuid4().hex
record={'id':'app-'+token,'token':token,'pid':os.getpid(),'expires':time.time()+args.ttl,
        'urls':[args.url],'ports':[int(args.url.split(':')[2].split('/')[0])]}
path=root/(record['id']+'.json')
try:
    path.write_text(json.dumps(record))
    print(json.dumps(record),flush=True)
    if select.select([sys.stdin],[],[],args.ttl)[0]:
        sys.stdin.read()
finally:
    path.unlink(missing_ok=True)
""")
        helper.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "review-state"))
    monkeypatch.setenv("HERDR_SESSION", "fixture")
    return tmp_path / "review-state/herdr-review/remote/fixture"


def ready_static(session):
    session.workspace.write("index.html", "<!doctype html><h1>Ready on laptop</h1>")
    session.descriptor = launch_descriptor(
        {"runtime": "static", "entry": "index.html"}, session.workspace
    )
    session.generation = "submitted"
    session.submitted_at = time.monotonic()


@pytest.mark.parametrize("policy", ["incremental", "after_all", "off"])
async def test_remote_previews_use_existing_processes_and_cleanup(
    factory, review_helper, monkeypatch, policy
):
    import aiohttp

    def forbidden_browser(*args):
        pytest.fail("Remote previews must never open a host browser")

    monkeypatch.setattr(module, "open_preview", forbidden_browser)
    first = factory("first", auto_open=policy, preview_destination="laptop")
    second = factory("second", auto_open=policy, preview_destination="laptop")
    for session in (first, second):
        ready_static(session)
        await session.execute()
        assert session.status == "success"
        assert len(session.attempts) == 1
    if policy == "off":
        assert not first.remote_preview and not second.remote_preview
        assert not list(review_helper.glob("*.json"))
        return
    paths = [review_helper / (s.remote_preview.record["id"] + ".json") for s in (first, second)]
    assert all(path.exists() for path in paths)
    async with aiohttp.ClientSession() as client:
        for session in (first, second):
            async with client.get(session.preview.url) as response:
                assert "Ready on laptop" in await response.text()
    await first.close()
    assert not paths[0].exists() and paths[1].exists()
    assert second.preview.process.returncode is None
    await HarnessBatch([second], policy, {}).review()
    assert not paths[1].exists()
    assert second.preview._stopped
    assert second.remote_preview.process.returncode == 0


async def test_remote_helper_expiry_stops_generated_app(factory, review_helper, monkeypatch):
    monkeypatch.setattr(handoff, "MAX_PREVIEW_SECONDS", 1)
    session = factory(preview_destination="laptop")
    ready_static(session)
    await session.execute()
    path = review_helper / (session.remote_preview.record["id"] + ".json")
    await asyncio.wait_for(session.preview_watch, 5)
    assert not path.exists()
    assert session.preview._stopped
    assert session.preview.process.returncode is not None
    assert session.status == "success"


async def test_cancelling_review_withdraws_remote_preview(factory, review_helper, capsys):
    session = factory(preview_destination="laptop", limits=Limits(review_seconds=30))
    ready_static(session)
    await session.execute()
    path = review_helper / (session.remote_preview.record["id"] + ".json")
    task = asyncio.create_task(HarnessBatch([session], "incremental", {}).review())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not path.exists()
    assert session.preview._stopped
    assert "Waiting for laptop connection" in capsys.readouterr().out


async def test_missing_helper_does_not_change_execution_success_or_open_host(factory, monkeypatch):
    session = factory(preview_destination="laptop")
    ready_static(session)
    monkeypatch.setattr(module, "open_preview", lambda *args: pytest.fail("Wrong browser"))
    # Keep bwrap and the rest of the runtime available.
    original = handoff.shutil.which
    monkeypatch.setattr(
        handoff.shutil, "which", lambda name: None if name == "herdr-review" else original(name)
    )
    await session.execute()
    assert session.status == "success"
    assert "install herdr-review" in session.attempts[-1]["presentation_error"]
    assert not session.remote_preview
    await session.close()
    assert session.preview._stopped
