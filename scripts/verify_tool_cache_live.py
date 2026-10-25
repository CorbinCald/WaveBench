"""Paid harness regression: a short prompt grows only through real tool calls."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import aiohttp

from wavebench.api import load_api_key
from wavebench.harness.config import Limits
from wavebench.harness.session import HarnessSession
from wavebench.harness.workspace import allocate_run

PROMPT = (
    "Read all of reference.txt using read_file, start_line=1, end_line=161. It contains reference "
    "records and the exact output required on its last line. Next write main.py to print "
    "that output exactly. Then lint, then submit main.py as runtime python. Make each "
    "step a separate tool call in a separate turn. Do not rewrite reference.txt. "
    "Use no dependencies."
)
EXPECTED = "tool cache verified 7391"
REFERENCE = (
    "".join(
        f"Record {i:04}: panel_{i:04} color_{i:04} size_{i:04} x={i:04} y={i:04} revision={i:04}.\n"
        for i in range(160)
    )
    + f"Required output: {EXPECTED}\n"
)


def check_result(result: dict, messages: list[dict]) -> list[str]:
    """Check cache reads separately from runtime success; neither implies the other."""
    harness = result["harness"]
    turns = harness["turns"]
    assert result["status"] == "success", result.get("error")
    assert len(harness["attempts"]) == 1
    assert harness["attempts"][0]["diagnostics"].strip() == EXPECTED
    assert len(turns) >= 4, "The probe must exercise separate read/write/lint/submit turns"
    assert turns[1]["usage"]["prompt_tokens"] > turns[0]["usage"]["prompt_tokens"], (
        "Reading the reference must grow the conversation"
    )
    assert all(t["model"] == harness["model_id"] for t in turns)
    user_messages = [m["content"] for m in messages if m["role"] == "user"]
    assert user_messages[0] == PROMPT
    assert all(message.startswith("[WaveBench]") for message in user_messages[1:])
    commands = []
    for message in messages:
        for call in message.get("tool_calls", []):
            commands.append(call["function"]["name"])
        if message["role"] == "tool":
            assert not message["content"].startswith("Error:"), message["content"]
    assert commands[0] == "read_file" and commands[-1] == "submit", commands
    assert "write_file" in commands and "lint" in commands, commands
    # Providers do not all report cache writes. Subsequent cache reads prove reuse.
    for turn in turns[2:]:
        usage = turn["usage"]
        ratio = usage["prompt_tokens_details"]["cached_tokens"] / usage["prompt_tokens"]
        assert ratio > 0.8, f"Tool conversation did not reuse its prefix: {ratio:.1%}"
    return commands


async def verify(output: Path, key: str, models: list[str]) -> None:
    output.mkdir(parents=True, exist_ok=False)
    run = allocate_run(output, "tool-cache-regression", "manual")
    reports = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as client:
        for slot, model in enumerate(models, 1):
            session = HarnessSession(
                run,
                slot,
                model.split("/")[-1],
                model,
                PROMPT,
                client,
                key,
                Limits(build_turns=8, turn_tokens=2048, build_seconds=180),
                asyncio.Semaphore(1),
                asyncio.Semaphore(1),
                auto_open="off",
                reasoning_effort="low",
            )
            try:
                session.workspace.write("reference.txt", REFERENCE)
                await session.build()
                await session.execute()
                result = session.result()
                (output / f"result-{slot}.json").write_text(json.dumps(result, indent=2))
                commands = check_result(result, session.messages)
                report = {
                    "model": model,
                    "status": result["status"],
                    "commands": commands,
                    "usage": result["usage"],
                    "turns": result["harness"]["turns"],
                    "runtime_output": result["harness"]["attempts"][0]["diagnostics"],
                    "user_messages": sum(m["role"] == "user" for m in session.messages),
                }
                reports.append(report)
                (output / "results.json").write_text(json.dumps(reports, indent=2))
                print(json.dumps({"model": model, "usage": result["usage"]}), flush=True)
            finally:
                await session.close()
    print(f"Tool caching verified. Evidence: {output}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow paid model requests")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--models", nargs="+", default=["openai/gpt-6-astra", "openai/gpt-5.6-luna"]
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required because this uses paid model requests")
    key = load_api_key()
    if not key:
        parser.error("OPENROUTER_API_KEY is missing")
    asyncio.run(verify(args.output.absolute(), key, args.models))


if __name__ == "__main__":
    main()
