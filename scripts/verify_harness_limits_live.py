"""Opt-in, bounded live acceptance probes for Harness issues #31–#33.

Run with --live --output /tmp/wavebench-limits-live. All generated projects,
transcripts and raw results stay in that new directory. summary.json contains
only counters, tool names and outcomes; inspect it before sharing.
"""

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

CASES = {
    "stream": {
        "model": "deepseek/deepseek-v4.1-flash",
        "total_tokens": 320_000,
        "turn_tokens": 64_000,
        "prompt": (
            "Build a small static website with index.html, styles.css, app.js and README.md. "
            "It should show 'Stream acceptance', a counter initially zero and a button that "
            "increments it. Use only local assets and no dependencies. Use actual wb file "
            "tools to create the files, then wb lint in a separate turn. Inspect that result "
            "and submit using wb done with runtime static and entry index.html. Keep the "
            "implementation compact; this test allows large output but does not require it."
        ),
    },
    "compaction": {
        "model": "google/gemini-3.8-flash",
        "total_tokens": 160_000,
        "turn_tokens": 4096,
        "prompt": (
            "Create main.py that prints 'budget compaction verified' and exits. First inspect "
            "reference-1.txt through reference-8.txt in numeric order, reading each complete "
            "file with wb read start=1 end=161, one file per model turn. These synthetic "
            "reference records are safe repetitive background, so do not repeat them in your "
            "answer. Only the newest file's final marker needs remembering. Files remain "
            "available if earlier conversation is summarized. Then write main.py, lint it "
            "in a separate turn, inspect the lint result and submit with wb done, runtime "
            "python. If the controller signals a finishing reserve, prioritize writing, "
            "validation and submission over remaining background reads. No dependencies."
            " Model turns advance automatically after tool results. Keep calling the next "
            "tool after every result or compaction summary; never end with a progress note "
            "or wait for another user message. Only wb done completes this task."
        ),
    },
    "reserve": {
        "model": "google/gemini-3.8-flash",
        "total_tokens": 40_000,
        "turn_tokens": 4096,
        "prompt": (
            "Read all of reference-1.txt with wb read start=1 end=161. Then create main.py "
            "and helpers.py, where main imports a helper returning 42 and prints it. Write "
            "both files in one turn, then call wb lint in a separate turn. Inspect the lint "
            "result and call wb done with runtime python and entry main.py. Use no "
            "dependencies. Follow any controller budget warning by completing this "
            "validation and submission promptly."
        ),
    },
}


def reference(number: int) -> str:
    return (
        "".join(
            f"Record {i:04}: panel_{i:04} color_{i:04} size_{i:04} "
            f"x={i:04} y={i:04} revision={i:04}.\n"
            for i in range(160)
        )
        + f"Reference marker: file-{number:02}.\n"
    )


def summarize(case: str, result: dict, messages: list[dict]) -> dict:
    harness = result["harness"]
    commands = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            try:
                arguments = json.loads(call["function"]["arguments"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            commands.append(arguments.get("command"))
    compactions = [
        {
            key: record.get(key)
            for key in ("reason", "status", "before_tokens", "after_tokens", "usage", "time_s")
        }
        for record in harness["compaction"]["records"]
    ]
    # Share only controller-authored counters and labels.
    warning_events = [
        {
            key: event.get(key)
            for key in ("kind", "phase", "remaining_tokens", "reserve_tokens", "outcome", "status")
            if key in event
        }
        for event in harness.get("finishing_budget", {}).get("records", [])
    ]
    warning_received = any(
        event.get("kind") == "warning" and event.get("status") == "received_response"
        for event in warning_events
    )
    checks = {
        "runtime_success": result["status"] == "success",
        "actual_tools": harness["tool_usage"]["calls"] > 0,
        "lint": bool(harness["lint"]),
        "submitted": harness["generation"] == "submitted",
        "fixed_budget": harness["budget_tokens"] <= harness["config"]["total_tokens"],
    }
    if case == "compaction":
        checks["budget_compaction_completed"] = any(
            record["status"] == "completed" and "budget" in record["reason"]
            for record in compactions
        )
    if case == "reserve":
        checks["warning_received"] = warning_received
    return {
        "case": case,
        "model": harness["model_id"],
        "status": result["status"],
        "generation": harness["generation"],
        "failure": result.get("failure"),
        "limits": harness["config"],
        "budget_tokens": harness["budget_tokens"],
        "usage": result["usage"],
        "tool_usage": harness["tool_usage"],
        "commands_in_retained_context": commands,
        "compactions": compactions,
        "budget_events": warning_events,
        "warning_received": warning_received,
        "streams": [(turn.get("adjustments") or {}).get("stream") for turn in harness["turns"]],
        "checks": checks,
        "passed": all(checks.values()),
    }


async def verify(output: Path, key: str, cases: list[str]) -> bool:
    output.mkdir(parents=True, exist_ok=False)
    run = allocate_run(output, "harness-limits-live", "manual")
    reports = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240)) as client:
        for slot, case in enumerate(cases, 1):
            spec = CASES[case]
            instance = HarnessSession(
                run,
                slot,
                case,
                spec["model"],
                spec["prompt"],
                client,
                key,
                Limits(
                    total_tokens=spec["total_tokens"],
                    turn_tokens=spec["turn_tokens"],
                    build_turns=14,
                    repair_turns=2,
                    build_seconds=240,
                    repair_seconds=30,
                    review_seconds=1,
                    process_concurrency=1,
                ),
                asyncio.Semaphore(1),
                asyncio.Semaphore(1),
                auto_open="off",
                auto_install="off",
                reasoning_effort="low",
            )
            try:
                if case != "stream":
                    for number in range(1, 9 if case == "compaction" else 2):
                        instance.workspace.write(f"reference-{number}.txt", reference(number))
                await instance.build()
                await instance.execute()
                report = summarize(case, instance.result(), instance.messages)
                reports.append(report)
                (output / "summary.json").write_text(json.dumps(reports, indent=2))
                print(
                    json.dumps(
                        {"case": case, "checks": report["checks"], "usage": report["usage"]}
                    ),
                    flush=True,
                )
                if not report["passed"]:
                    return False
            finally:
                await instance.close()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow paid API requests")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--case", action="append", choices=CASES)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required because these checks spend API credits")
    key = load_api_key()
    if not key:
        parser.error("OPENROUTER_API_KEY is missing; no paid requests were sent")

    async def bounded():
        return await asyncio.wait_for(
            verify(args.output.absolute(), key, args.case or list(CASES)), timeout=850
        )

    return 0 if asyncio.run(bounded()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
