"""Paid acceptance checks for provider caching and real 240K Luna compaction."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path

import aiohttp

from wavebench.api import load_api_key
from wavebench.harness.commands import TOOL_SCHEMA
from wavebench.harness.config import Limits
from wavebench.harness.context import COMPACTION_MODEL
from wavebench.harness.session import HarnessSession
from wavebench.harness.transport import call_conversation
from wavebench.harness.workspace import allocate_run
from wavebench.tokens import prompt_tokens


async def verify(output: Path, key: str, cache_only: bool):
    output.mkdir(parents=True, exist_ok=False)
    run = allocate_run(output, "cache-context-verification", "manual")
    results = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as client:

        async def cache_probe(slot, model):
            instance = HarnessSession(
                run,
                slot,
                model.split("/")[1],
                model,
                "This is a cache acceptance test. On every turn, call wb ls exactly once. "
                "Do not summarize the following static reference records.\n"
                + "\n".join(
                    f"Record {i}: project files persist between tool calls; inspect the directory."
                    for i in range(800)
                ),
                client,
                key,
                Limits(),
                asyncio.Semaphore(1),
                asyncio.Semaphore(1),
                auto_open="off",
            )
            rows = []
            try:
                for _ in range(3):
                    turn = await call_conversation(
                        client,
                        key,
                        model,
                        instance.messages,
                        TOOL_SCHEMA,
                        max_tokens=2048,
                        reasoning_effort=None,
                        input_tokens_bound=prompt_tokens(instance.messages, TOOL_SCHEMA) * 11 // 10,
                        cache_policy=instance.cache_policy,
                    )
                    instance.messages.append(turn.message)
                    calls = turn.message.get("tool_calls") or []
                    assert calls, f"{model} did not call wb"
                    native = [
                        {
                            "id": c["id"],
                            "name": c["function"]["name"],
                            "arguments": json.loads(c["function"]["arguments"]),
                        }
                        for c in calls
                    ]
                    assert all(c["arguments"]["command"] == "ls" for c in native)
                    records = await instance.dispatcher.batch(native)
                    instance.messages.extend(
                        {"role": "tool", "tool_call_id": r["id"], "content": json.dumps(r)}
                        for r in records
                    )
                    instance.messages.append(
                        {"role": "user", "content": "Next cache check: call wb ls once again."}
                    )
                    rows.append(
                        {
                            "usage": turn.usage,
                            "provider": turn.provider,
                            "adjustments": turn.adjustments,
                        }
                    )
                    print(
                        json.dumps({"model": model, "turn": len(rows), "usage": turn.usage}),
                        flush=True,
                    )
                results[model] = rows
                assert any(
                    (r["usage"].get("prompt_tokens_details") or {}).get("cached_tokens", 0) > 0
                    for r in rows[1:]
                ), f"No cache hit reported for {model}"
            finally:
                (output / f"cache-{slot}.json").write_text(json.dumps(rows, indent=2))
                await instance.close()

        outcomes = await asyncio.gather(
            *[
                cache_probe(i + 1, model)
                for i, model in enumerate(
                    [
                        COMPACTION_MODEL,
                        "anthropic/claude-haiku-4.5",
                        "google/gemini-2.5-flash",
                    ]
                )
            ],
            return_exceptions=True,
        )
        errors = [
            str(value) or type(value).__name__
            for value in outcomes
            if isinstance(value, BaseException)
        ]
        if errors:
            raise RuntimeError("Cache verification failed: " + "; ".join(errors))
        if not cache_only:
            instance = HarnessSession(
                run,
                4,
                "luna-compaction",
                COMPACTION_MODEL,
                "Create main.py that prints exactly 'cache and context verified' and exits. "
                "Use no dependencies. Lint it and submit it with wb done. Sentinel: first-user-preserved-7391.",
                client,
                key,
                Limits(total_tokens=1_000_000, build_seconds=360, turn_tokens=8192),
                asyncio.Semaphore(1),
                asyncio.Semaphore(1),
                auto_open="off",
                reasoning_effort="high",
            )
            instance.messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": "Earlier inspection log; no files have been written yet.\n"
                        + "\n".join(
                            f"Inspection {i}: directory was empty. No implementation or validation has happened. The next task is to create main.py using the original user's exact output string."
                            for i in range(8200)
                        ),
                    },
                    {
                        "role": "user",
                        "content": "Keep the original print string. Old inspection logs can be summarized.",
                    },
                    {
                        "role": "assistant",
                        "content": "I will now inspect the project before writing the requested program. Sentinel: latest-full-agent-9284.",
                        "tool_calls": [
                            {
                                "id": "seed-ls",
                                "type": "function",
                                "function": {"name": "wb", "arguments": '{"command":"ls"}'},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "seed-ls",
                        "content": '{"ok":true,"files":[]}',
                    },
                ]
            )
            before = copy.deepcopy(instance.messages)
            size = prompt_tokens(before, TOOL_SCHEMA)
            assert 240_000 < size < 700_000, size
            print(f"Compaction test: {size:,} locally counted input tokens", flush=True)
            try:
                await instance.build()
                await instance.execute()
                result = instance.result()
                results["compaction"] = result
                assert instance.status == "success", instance.error
                assert len(instance.attempts) == 1
                record = instance.compactions[0]
                assert record["status"] == "completed"
                assert record["model"] == COMPACTION_MODEL and record["reasoning_effort"] == "high"
                assert instance.messages[:2] == before[:2]
                assert instance.messages[3:5] == before[-2:]
                assert json.loads((instance.metadata / record["archive"]).read_text()) == before
                print(json.dumps({"compaction": record, "status": instance.status}), flush=True)
            finally:
                (output / "compaction-result.json").write_text(
                    json.dumps(instance.result(), indent=2)
                )
                await instance.close()
    (output / "results.json").write_text(json.dumps(results, indent=2))
    print(f"Verification passed. Evidence: {output}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required because this uses paid model requests")
    key = load_api_key()
    if not key:
        parser.error("OPENROUTER_API_KEY is missing")
    asyncio.run(verify(args.output.absolute(), key, args.cache_only))


if __name__ == "__main__":
    main()
