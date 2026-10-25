"""Keep opt-in verification usable without making paid API requests in CI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import verify_context_live, verify_harness_live, verify_tool_cache_live
from wavebench.harness import session as session_module
from wavebench.harness.transport import Turn

from ..harness_calls import tool_call


@pytest.mark.parametrize(
    "script",
    [
        "effort_audit.py",
        "effort_audit_tight.py",
        "verify_context_live.py",
        "verify_harness_live.py",
        "verify_tool_cache_live.py",
    ],
)
def test_paid_helpers_require_opt_in_even_with_a_configured_key(tmp_path, monkeypatch, script):
    repo = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(repo))
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-test-key")
    command = [sys.executable, str(repo / "scripts" / script)]
    help_result = subprocess.run(
        [*command, "--help"], capture_output=True, text=True, check=False, timeout=10
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--live" in help_result.stdout
    arguments = [] if script == "effort_audit.py" else ["--output", str(tmp_path / "results")]
    if script == "verify_harness_live.py":
        arguments += ["--model", "first/model", "--model", "second/model"]
    result = subprocess.run(
        [*command, *arguments], capture_output=True, text=True, check=False, timeout=10
    )
    assert result.returncode == 2
    assert "--live is required" in result.stderr
    assert not (tmp_path / "results").exists()


async def test_cache_probe_uses_current_file_tools_and_plain_results(tmp_path, monkeypatch):
    counts = {}

    async def model(client, key, model_id, messages, tools, **kwargs):
        index = counts.get(model_id, 0)
        counts[model_id] = index + 1
        assert "list_files" in {tool["function"]["name"] for tool in tools}
        if index:
            replies = [message["content"] for message in messages if message["role"] == "tool"]
            assert replies and all(reply == "(no files)" for reply in replies)
            if model_id.startswith("google/"):
                assert kwargs["gemini_provider"] == "Google"
        return Turn(
            {"role": "assistant", "tool_calls": [tool_call(str(index), {"command": "ls"})]},
            {"prompt_tokens": 5000, "prompt_tokens_details": {"cached_tokens": 4500}},
            model_id,
            "Google" if model_id.startswith("google/") else "test",
            "tool_calls",
            {},
        )

    monkeypatch.setattr(verify_context_live, "call_conversation", model)
    output = tmp_path / "cache"
    await verify_context_live.verify(output, "offline", cache_only=True)
    results = json.loads((output / "results.json").read_text())
    assert len(results) == 3
    assert all(count == 3 for count in counts.values())


async def test_tool_cache_probe_builds_and_runs_with_current_harness(tmp_path, monkeypatch):
    if sys.platform != "linux" or not shutil.which("bwrap"):
        if os.getenv("WAVEBENCH_REQUIRE_SANDBOX_TESTS"):
            pytest.fail("Linux and bwrap required")
        pytest.skip("Linux and bwrap required")

    async def capable(*args):
        return True

    commands = iter(
        [
            {"command": "read", "path": "reference.txt", "start": 1, "end": 161},
            {
                "command": "write",
                "path": "main.py",
                "content": f"print({verify_tool_cache_live.EXPECTED!r})\n",
            },
            {"command": "lint"},
            {"command": "submit", "runtime": "python", "entry": "main.py"},
        ]
    )
    calls = 0

    async def model(client, key, model_id, messages, tools, **kwargs):
        nonlocal calls
        calls += 1
        prompt = 1000 if calls == 1 else 10_000
        return Turn(
            {"role": "assistant", "tool_calls": [tool_call(str(calls), next(commands))]},
            {
                "prompt_tokens": prompt,
                "completion_tokens": 100,
                "total_tokens": prompt + 100,
                "cost": 0.01,
                "prompt_tokens_details": {"cached_tokens": 9000 if calls > 2 else 0},
            },
            model_id,
            "test",
            "tool_calls",
            {},
        )

    monkeypatch.setattr(session_module, "capability", capable)
    monkeypatch.setattr(session_module, "call_conversation", model)
    output = tmp_path / "tool-cache"
    await verify_tool_cache_live.verify(output, "offline", ["test/model"])
    result = json.loads((output / "results.json").read_text())[0]
    assert result["runtime_output"].strip() == verify_tool_cache_live.EXPECTED
    assert result["commands"] == ["read_file", "write_file", "lint", "submit"]


def test_cli_matrix_counts_preview_loads_from_browser_logs(tmp_path, monkeypatch):
    output = tmp_path / "matrix"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_harness_live.py",
            "--live",
            "--output",
            str(output),
            "--model",
            "first/model",
            "--model",
            "second/model",
            "--case",
            "web-incremental",
        ],
    )
    monkeypatch.setattr(verify_harness_live, "load_api_key", lambda: "offline")

    def run(command, *, cwd, **kwargs):
        models = {}
        for name in ("first", "second"):
            metadata = cwd / "benchmarkResults" / "harness" / "run" / "metadata" / name
            metadata.mkdir(parents=True)
            (metadata / "browser.log").write_text("PREVIEW_HTTP 200 1234\n")
            models[name] = {
                "status": "success",
                "usage": {"total_tokens": 100},
                "harness": {"attempts": [{"outcome": "success"}]},
            }
        (cwd / ".benchmark_history.json").write_text(json.dumps({"runs": [{"models": models}]}))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(verify_harness_live.subprocess, "run", run)
    assert verify_harness_live.main() == 0
    evidence = json.loads((output / "evidence.json").read_text())
    assert evidence[0]["passed"] and evidence[0]["preview_loads"] == 2
