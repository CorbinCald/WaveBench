"""Real-tokenizer budget planning and controller/tool round trips, without paid APIs."""

import asyncio
import copy
import json

import pytest

from wavebench.harness import session as module
from wavebench.harness.budget import finish_reserve
from wavebench.harness.config import Limits
from wavebench.harness.context import (
    BUDGET_COMPACTION_REASON,
    COMPACTION_MODEL,
    admit_compaction,
    compaction_reason,
    plan_compaction,
)
from wavebench.harness.session import BudgetError, HarnessSession
from wavebench.harness.transport import Turn
from wavebench.harness.workspace import allocate_run
from wavebench.tokens import prompt_tokens


def tool_message(call_id, command, **arguments):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "wb",
                    "arguments": json.dumps({"command": command, **arguments}),
                },
            }
        ],
    }


def history(session, words=78_000):
    session.messages.extend(
        [
            tool_message("read-old", "read", path="main.py"),
            {"role": "tool", "tool_call_id": "read-old", "content": "work " * words},
            tool_message("last-read", "read", path="main.py"),
            {"role": "tool", "tool_call_id": "last-read", "content": "print(42)\n"},
        ]
    )
    session.workspace.write("main.py", "print(42)\n")
    return copy.deepcopy(session.messages)


@pytest.fixture
async def session(tmp_path):
    instance = HarnessSession(
        allocate_run(tmp_path, "budget-compaction", "test"),
        1,
        "Gemini",
        "google/gemini-3.8-flash",
        "Keep main.py and print 42. Validate and submit the project.",
        None,
        "offline",
        Limits(total_tokens=1_000_000, build_turns=6),
        asyncio.Semaphore(1),
        asyncio.Semaphore(1),
        auto_open="off",
    )
    yield instance
    await instance.close()


def test_regression_triggers_before_the_gemini_failure_frontier():
    # The old policy only checked the 240k context threshold and model window.
    args = (80_000, 80_687, 1_000_000, 16_384)
    assert compaction_reason(*args) is None
    assert compaction_reason(*args, remaining_tokens=285_000) == BUDGET_COMPACTION_REASON
    assert compaction_reason(*args, remaining_tokens=400_000) is None
    assert compaction_reason(*args, remaining_tokens=65_312) == BUDGET_COMPACTION_REASON


@pytest.mark.parametrize("context", [100, 8_000, 16_383])
def test_already_small_context_does_not_trigger(context):
    assert compaction_reason(context, context, 128_000, 4096, remaining_tokens=1000) is None


def test_finishing_frontier_triggers_before_three_ordinary_requests():
    reserve = finish_reserve(23_000, 4096)
    assert (
        compaction_reason(
            23_000,
            23_000,
            128_000,
            4096,
            remaining_tokens=90_000,
            finishing_reserve_tokens=reserve,
        )
        == BUDGET_COMPACTION_REASON
    )


def test_compactor_output_shrinks_to_protect_two_followups():
    plan = admit_compaction(
        remaining_tokens=100_000,
        input_bound=60_000,
        before_bound=60_000,
        after_bound=5000,
        reserve_tokens=30_000,
        followup_output_tokens=4096,
        require_savings=True,
    )
    assert plan.skip_reason is None
    assert plan.output_tokens == 10_000
    assert plan.followup_turns == 2
    assert 60_000 + plan.output_tokens + plan.reserve_tokens == 100_000


@pytest.mark.parametrize("model_id", ["google/gemini-3.8-flash", "other/model"])
@pytest.mark.parametrize(
    "words,initial,total", [(78_000, 715_000, 1_000_000), (23_000, 30_000, 120_000)]
)
async def test_budget_compaction_pays_cached_input_and_allows_real_tools(
    session, monkeypatch, model_id, words, initial, total
):
    session.model_id = model_id
    session.limits = Limits(total_tokens=total, build_turns=6)
    before = history(session, words)
    session.budget_tokens = initial
    session.turns.append(
        {
            "phase": "building",
            "usage": {
                "prompt_tokens": initial - 100,
                "completion_tokens": 100,
                "total_tokens": initial,
            },
        }
    )
    calls = []

    async def model(client, key, model_id, messages, tools, **kwargs):
        calls.append(model_id)
        assert kwargs["input_tokens_bound"] + kwargs["max_tokens"] <= (
            session.limits.total_tokens - session.budget_tokens
        )
        if model_id == COMPACTION_MODEL:
            assert tools == []
            request = json.loads(messages[1]["content"])
            assert request["preserved_prefix"] == before[:2]
            assert request["preserved_tail"] == before[-2:]
            message = {
                "role": "assistant",
                "content": "main.py prints 42. Keep it, validate and submit.",
            }
            finish = "stop"
        elif len(calls) == 2:
            assert messages[:2] == before[:2]
            # A small remaining budget may inject the finishing warning after
            # compaction. The protected interaction must still be unchanged.
            resumed = messages
            if (messages[-1].get("content") or "").startswith("[WaveBench budget warning]"):
                assert messages[-1]["role"] == "user"
                resumed = messages[:-1]
            assert resumed[-2:] == before[-2:]
            message = tool_message("write-current", "write", path="main.py", content="print(42)\n")
            finish = "tool_calls"
        else:
            assert json.loads(messages[-1]["content"])["ok"]
            message = tool_message("submit", "done", runtime="python", entry="main.py")
            finish = "tool_calls"
        measured = prompt_tokens(messages, tools)
        return Turn(
            message,
            {
                "prompt_tokens": measured,
                "completion_tokens": 100,
                "total_tokens": measured + 100,
                "prompt_tokens_details": {"cached_tokens": measured - 1},
            },
            model_id,
            "offline",
            finish,
            {},
        )

    monkeypatch.setattr(module, "call_conversation", model)
    # Conversation uses the actual write/done dispatcher; it does not launch a
    # process, so this controller regression needs no host sandbox privileges.
    await session.conversation()
    assert session.descriptor["entry"] == "main.py"
    assert calls == [COMPACTION_MODEL, model_id, model_id]
    record = session.compactions[0]
    assert record["status"] == "completed"
    assert words < record["before_tokens"] < words + 7000
    assert record["before_tokens"] < 240_000
    assert record["after_tokens"] < 3000
    assert record["followup_turns"] == 3
    assert record["reserve_outcome"] == "preserved"
    assert record["remaining_after_tokens"] >= record["actual_finishing_reserve_tokens"]
    assert record["charged_tokens"] == record["usage"]["total_tokens"] > words
    assert session.budget_tokens == session.usage()["total_tokens"] < total
    assert session.limits.total_tokens == total
    assert session.workspace.read("main.py") == "print(42)\n"


async def test_unaffordable_compaction_records_one_skip_and_keeps_context(session, monkeypatch):
    before = history(session)
    session.budget_tokens = 934_688

    async def forbidden(*args, **kwargs):
        pytest.fail("unaffordable compaction must not send a paid request")

    monkeypatch.setattr(module, "call_conversation", forbidden)
    estimate = prompt_tokens(session.messages, session.tools)
    assert not await session.compact(BUDGET_COMPACTION_REASON, estimate, 10)
    assert not await session.compact(BUDGET_COMPACTION_REASON, estimate, 10)
    assert session.messages == before
    assert session.budget_tokens == 934_688
    assert len(session.compactions) == 1
    assert "unaffordable" in session.compactions[0]["skip_reason"]
    assert session.compactions[0]["charged_tokens"] == 0


async def test_preserved_large_task_makes_compaction_savings_insufficient(session, monkeypatch):
    session.messages[1]["content"] = "original " * 18_000
    before = history(session, words=2000)

    async def forbidden(*args, **kwargs):
        pytest.fail("the compactor cost cannot be recovered from the small removable history")

    monkeypatch.setattr(module, "call_conversation", forbidden)
    estimate = prompt_tokens(session.messages, session.tools)
    assert not await session.compact(BUDGET_COMPACTION_REASON, estimate, 10)
    assert "repay its token cost" in session.compactions[0]["skip_reason"]
    assert session.messages == before


async def test_compaction_cannot_spend_an_active_finishing_reserve(session, monkeypatch):
    before = history(session)
    session.finishing = True

    async def forbidden(*args, **kwargs):
        pytest.fail("optional compaction must not interrupt finishing")

    monkeypatch.setattr(module, "call_conversation", forbidden)
    assert not await session.compact(BUDGET_COMPACTION_REASON, 80_000, 10)
    assert "finishing reserve is already active" in session.compactions[0]["skip_reason"]
    assert session.messages == before


async def test_provider_compaction_overrun_is_charged_without_increasing_budget(
    session, monkeypatch
):
    before = history(session)
    session.budget_tokens = 715_000

    async def model(*args, **kwargs):
        return Turn(
            {"role": "assistant", "content": "Summary"},
            {"prompt_tokens": 290_000, "completion_tokens": 100, "total_tokens": 290_100},
            COMPACTION_MODEL,
            "offline",
            "stop",
            {},
        )

    monkeypatch.setattr(module, "call_conversation", model)
    with pytest.raises(BudgetError, match="exhausted during context compaction"):
        await session.compact(BUDGET_COMPACTION_REASON, 80_000, 10)
    assert session.messages == before
    assert session.budget_tokens == 1_005_100
    assert session.limits.total_tokens == 1_000_000
    assert session.compactions[0]["charged_tokens"] == 290_100


async def test_interrupted_compactor_counts_observed_output_without_reported_usage(
    session, monkeypatch
):
    before = history(session)

    async def model(*args, **kwargs):
        kwargs["on_usage"]({}, 7654)
        raise asyncio.CancelledError()

    monkeypatch.setattr(module, "call_conversation", model)
    with pytest.raises(asyncio.CancelledError):
        await session.compact(BUDGET_COMPACTION_REASON, 80_000, 10)
    record = session.compactions[0]
    assert session.budget_tokens == record["input_tokens_bound"] + 7654
    assert record["status"] == "failed"
    assert record["charged_tokens"] == session.budget_tokens
    assert record["usage"] == {}
    assert session.messages == before


def test_latest_parallel_tool_calls_keep_exact_result_pairing():
    messages = [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "older work"},
        tool_message("a", "ls"),
        {"role": "tool", "tool_call_id": "b", "content": "second result"},
        {"role": "tool", "tool_call_id": "a", "content": "first result"},
    ]
    messages[3]["tool_calls"].extend(tool_message("b", "ls")["tool_calls"])
    assert plan_compaction(messages).apply("Earlier work")[-3:] == messages[-3:]
    with pytest.raises(ValueError, match="interaction is incomplete"):
        plan_compaction(messages[:-1])


def test_summary_respects_the_admitted_size():
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "older work"},
        {"role": "assistant", "content": "latest work"},
    ]
    plan = plan_compaction(messages)
    assert "at most 32 tokens" in plan.request(32)[0]["content"]
    with pytest.raises(ValueError, match="exceeds 32 tokens"):
        plan.apply("word " * 100, 32)
