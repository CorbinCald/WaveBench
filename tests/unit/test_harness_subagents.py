"""Subagent limits, tool shapes, prompts, display columns, analytics, and CLI wiring."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from types import SimpleNamespace

import pytest

from wavebench.harness import subagents as module
from wavebench.harness.commands import SPAWN_AGENT, TOOL_SCHEMA, Dispatcher
from wavebench.harness.config import Limits
from wavebench.harness.session import system_prompt
from wavebench.harness.subagents import SubagentPool, subagent_prompt, subagents_status
from wavebench.tui.progress import ProgressTracker


def plain(text: str) -> str:
    return re.sub(r"\033\[[0-9;?]*[a-zA-Z]", "", text).replace("\r", "")


def test_limits_validate_the_parallel_window_and_positive_caps():
    defaults = Limits()
    assert (defaults.subagent_parallel, defaults.subagent_cap) == (4, 8)
    assert (defaults.subagent_turns, defaults.subagent_seconds) == (20, 600)
    assert Limits(subagent_parallel=2).subagent_parallel == 2
    assert Limits(subagent_parallel=5).subagent_parallel == 5
    for value in (1, 6):
        with pytest.raises(ValueError, match="between 2 and 5"):
            Limits(subagent_parallel=value)
    with pytest.raises(ValueError, match="subagent_cap must be a positive integer"):
        Limits(subagent_cap=0)
    assert Limits.from_config({"harness": {"subagent_cap": 3}}).subagent_cap == 3


def test_status_strings_follow_config_and_harness_caps():
    assert subagents_status({}) == "Off"
    assert subagents_status({"subagents": "off", "harness": {"subagent_cap": 2}}) == "Off"
    assert subagents_status({"subagents": "on"}) == "On (4 parallel, 8 total)"
    assert (
        subagents_status(
            {"subagents": "on", "harness": {"subagent_parallel": 2, "subagent_cap": 12}}
        )
        == "On (2 parallel, 12 total)"
    )


def names(tools):
    return [tool["function"]["name"] for tool in tools]


def test_tool_schemas_separate_lead_and_subagent_capabilities(tmp_path):
    function = SPAWN_AGENT["function"]
    assert function["name"] == "spawn_agent"
    assert function["parameters"]["required"] == ["name", "task"]
    assert set(function["parameters"]["properties"]) == {"name", "task", "read_only"}
    assert function["parameters"]["additionalProperties"] is False
    assert "run in parallel" in function["description"]
    pool = SimpleNamespace(available=lambda: True)
    lead = Dispatcher(None, None, tmp_path, Limits(), subagents=pool)
    assert names(lead.available_tools()) == [*names(TOOL_SCHEMA), "spawn_agent"]
    writer = Dispatcher(None, None, tmp_path, Limits(), parent=lead)
    assert names(writer.tools) == [
        "read_file",
        "write_file",
        "edit_file",
        "list_files",
        "delete_file",
        "lint",
    ]
    # A read-only agent is never offered tools it may not use.
    reader = Dispatcher(None, None, tmp_path, Limits(), parent=lead, read_only=True)
    assert names(reader.tools) == ["read_file", "list_files", "lint"]


def test_prompts_state_caps_ownership_and_the_report_contract():
    lead = system_prompt("off", False, {"parallel": 3, "cap": 7})
    assert "up to 3 run at once and 7 in total" in lead
    assert "in the same response so they run in parallel" in lead and "submit" in lead
    assert lead.startswith(system_prompt("off", False, None))
    assert "Subagents" not in system_prompt("on", True)
    limits = Limits(subagent_turns=5, subagent_seconds=90)
    writer = subagent_prompt("on", True, limits, read_only=False)
    assert "5 model requests and 90 seconds of active time" in writer
    assert "PyPI wheels" in writer and "web_search" in writer
    assert "cannot submit the project or spawn agents" in writer
    assert "The lead sees only that report" in writer
    reader = subagent_prompt("off", False, limits, read_only=True)
    assert "read-only" in reader and "web_search" not in reader


@pytest.mark.parametrize(
    "arguments,message",
    [
        ({"task": "x"}, "1-40 characters"),
        ({"name": "", "task": "x"}, "1-40 characters"),
        ({"name": "a" * 41, "task": "x"}, "1-40 characters"),
        ({"name": "a", "task": " "}, "complete brief"),
        ({"name": "a", "task": "x" * 24_001}, "exceeds 24,000"),
        ({"name": "a", "task": "x", "read_only": "yes"}, "read_only must be a boolean"),
        ({"name": "a", "task": "x", "model": "other"}, "accepts only"),
        ("not an object", "accepts only"),
    ],
)
def test_spawn_arguments_are_validated_before_any_request(arguments, message):
    with pytest.raises(ValueError, match=message):
        SubagentPool.validate(arguments)
    assert SubagentPool.validate({"name": " api ", "task": "brief"}) == ("api", "brief", False)


def test_finishing_withdraws_spawning():
    session = SimpleNamespace(model_id="vendor/model", finishing=False, turns=[])
    pool = SubagentPool(session, Limits())
    assert pool.available() is True
    pool.runs.append(SimpleNamespace(status="running"))
    session.finishing = True
    assert pool.available() is False
    assert pool.usage()["active"] == 1


@pytest.mark.parametrize("width", [32, 52, 56, 72, 76, 102, 112])
@pytest.mark.parametrize("status", [None, "success", "failed", "cancelled"])
def test_agent_counts_fit_live_and_final_rows(width, status):
    tracker = ProgressTracker(1, {}, model_names=["model"])
    tracker.update_harness("model", {"api_turns": 4, "completion_tokens": 10}, 5)
    agents = {"enabled": True, "spawned": 5, "completed": 4, "failed": 1}
    tracker.update_harness_tools("model", {"calls": 9, "failures": 1}, subagents=agents)
    tracker.set_phase("model", "delegating")
    result = None
    if status:
        result = {
            "status": status,
            "time_s": 10,
            "usage": {},
            "harness": {"subagents": agents, "tool_usage": {"calls": 9, "failures": 1}},
        }
        tracker._results["model"] = result
        tracker._harness.clear()
    header = plain(tracker._format_harness_header(width))
    row = plain(tracker._format_harness_row("model", width, result))
    assert ("AGENTS" if width >= 100 else "AGT") in header
    assert "5" in row.split()
    assert len(header) == len(row) == width
    assert "agents 5" in plain(tracker._format_harness_tool_metrics("model", result))


def test_agent_column_appears_beside_search_columns_and_not_for_disabled_or_old_results():
    tracker = ProgressTracker(1, {}, model_names=["model"])
    tracker.update_harness("model", {"api_turns": 1}, 1)
    tracker.update_harness_tools(
        "model",
        {"calls": 1, "failures": 0},
        web_search={"enabled": True, "calls": 2, "failures": 0},
        web_fetch={"enabled": True, "calls": 3, "failures": 0},
        subagents={"enabled": False},
    )
    tracker.set_phase("model", "building")
    assert "AGT" not in plain(tracker._format_harness_header(112))
    assert tracker._harness_metrics("model")["subagents"] is None
    tracker.update_harness_tools(
        "model", {"calls": 1, "failures": 0}, subagents={"enabled": True, "spawned": 0}
    )
    for width in (52, 72, 112):
        header = plain(tracker._format_harness_header(width))
        assert "AG" in header and "WEB" in header and ("GET" in header or "READS" in header)
        row = plain(tracker._format_harness_row("model", width))
        assert len(header) == len(row) == width
    assert "agents 0" in plain(tracker._format_harness_tool_metrics("model"))
    tracker._results["old"] = {"harness": {}, "usage": {}}
    tracker._harness.clear()
    assert "AG" not in plain(tracker._format_harness_header(112))


def test_streamed_subagent_output_moves_the_live_rate_without_a_lead_turn(monkeypatch):
    clock = {"now": 10.0}
    monkeypatch.setattr(
        "wavebench.tui.progress.tracker.time", SimpleNamespace(monotonic=lambda: clock["now"])
    )
    tracker = ProgressTracker(1, {}, model_names=["model"])
    tracker.update_harness("model", {"api_turns": 1, "completion_tokens": 40}, 2)
    tracker.set_phase("model", "delegating")
    tracker.note_harness_output("model", 30)
    tracker.note_harness_output("model", 0)
    tracker.note_harness_output("missing", 30)
    clock["now"] += 1
    values = tracker._harness_display_metrics("model")
    assert values["rate"] == pytest.approx(30) and values["rate_estimated"]
    assert values["tokens"].value == 40  # Counts settle when each subagent request completes.


def test_lifetime_analytics_total_spawned_agents_and_keep_old_records_unknown(capsys):
    from wavebench.tui.analytics import display_analytics
    from wavebench.tui.analytics.harness import aggregate_harness

    def result(agents):
        return {
            "status": "success",
            "time_s": 1,
            "usage": {"api_turns": 1, "total_tokens": 10, "cost": 0.1},
            "harness": {"tool_usage": {"calls": 1, "failures": 0}, **agents},
        }

    history = {
        "version": 1,
        "runs": [
            {
                "timestamp": "2026-09-21T00:00:00+00:00",
                "prompt": "p",
                "models": {
                    "new": result({"subagents": {"enabled": True, "spawned": 3}}),
                    "off": result({"subagents": {"enabled": False}}),
                },
            },
            {
                "timestamp": "2026-09-21T00:00:00+00:00",
                "prompt": "p",
                "models": {"old": result({})},
            },
        ],
    }
    models, total = aggregate_harness(history)
    assert models["new"].totals["subagents"].value == 3
    assert models["off"].totals["subagents"].value == 0
    assert models["old"].totals["subagents"].count == 0
    assert total.totals["subagents"].measurement(3).incomplete
    display_analytics(history, compact=False)
    output = plain(capsys.readouterr().out)
    assert "agents ≥3" in output and "agents 3" in output and "agents —" in output


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["--subagents"], (True, None)),
        (["--no-subagents", "--agent-cap", "9"], (False, 9)),
        (["--agent-cap", "3"], (None, 3)),
        ([], (None, None)),
    ],
)
def test_cli_flags_reach_the_run_arguments(monkeypatch, argv, expected):
    from wavebench import __main__ as main_mod

    seen = {}

    async def fake_main_async(args, api_key, model_mapping=None, config=None, pricing_lookup=None):
        seen["flags"] = (args.subagents, args.agent_cap)

    monkeypatch.setattr(sys, "argv", ["wavebench", "--mode", "harness", "--prompt", "x", *argv])
    monkeypatch.setattr(main_mod, "load_api_key", lambda: "test-key")
    monkeypatch.setattr(main_mod, "fetch_top_models", lambda *a, **k: ([], {}))
    monkeypatch.setattr(main_mod, "load_models", lambda: None)
    monkeypatch.setattr(main_mod, "load_config", lambda: {"theme": "default"})
    monkeypatch.setattr(main_mod, "apply_theme", lambda _theme: None)
    monkeypatch.setattr(main_mod, "main_async", fake_main_async)
    main_mod.main()
    assert seen["flags"] == expected


def test_cli_rejects_a_zero_agent_cap(monkeypatch, capsys):
    from wavebench import __main__ as main_mod

    monkeypatch.setattr(sys, "argv", ["wavebench", "--prompt", "x", "--agent-cap", "0"])
    with pytest.raises(SystemExit):
        main_mod.main()
    assert "--agent-cap must be at least 1" in capsys.readouterr().err


@pytest.mark.parametrize(
    "flags,config_state,expected",
    [
        ({"subagents": None, "agent_cap": None}, "off", (False, 8)),
        ({"subagents": None, "agent_cap": None}, "on", (True, 8)),
        ({"subagents": None, "agent_cap": 5}, "off", (True, 5)),
        ({"subagents": False, "agent_cap": 5}, "on", (False, 5)),
        ({"subagents": True, "agent_cap": None}, "off", (True, 8)),
    ],
)
async def test_orchestrator_wires_the_run_setting_and_cap_into_each_session(
    tmp_state_dir, monkeypatch, capsys, flags, config_state, expected
):
    from wavebench.core import orchestrator as orchestrator_mod
    from wavebench.harness import session as session_mod

    created = []

    async def fake_get_directory_name(*_args, **_kwargs):
        return "subagents_wiring"

    class FakeSession:
        def __init__(self, run, slot, name, model_id, prompt, client, key, limits, *slots, **kw):
            self.name, self.limits, self.kwargs = name, limits, kw
            created.append(self)

    class FakeBatch:
        def __init__(self, sessions, auto_open, results):
            self.sessions, self.results = sessions, results

        async def run(self):
            for session in self.sessions:
                self.results[session.name] = {
                    "status": "success",
                    "time_s": 1.0,
                    "file": None,
                    "usage": {},
                    "retries": [],
                    "harness": {"version": 1, "attempts": [], "subagents": {"enabled": False}},
                }

        async def review(self):
            pass

    monkeypatch.setattr(orchestrator_mod, "get_directory_name", fake_get_directory_name)
    monkeypatch.setattr(session_mod, "HarnessSession", FakeSession)
    monkeypatch.setattr(session_mod, "HarnessBatch", FakeBatch)
    args = SimpleNamespace(prompt="Build it", mode="harness", text=False, auto_open="off", **flags)
    await orchestrator_mod.main_async(
        args,
        api_key="test-key",
        model_mapping={"model": "vendor/model"},
        config={
            "reasoning_effort": "high",
            "auto_open": "off",
            "auto_install": "off",
            "directory_naming": "slug",
            "web_search": "off",
            "subagents": config_state,
            "harness": {"subagent_parallel": 3},
        },
        pricing_lookup={},
    )
    (session,) = created
    assert (session.kwargs["subagents"], session.limits.subagent_cap) == expected
    assert session.limits.subagent_parallel == 3
    output = plain(capsys.readouterr().out)
    label = f"On (3 parallel, {expected[1]} total)" if expected[0] else "Off"
    assert f"AGENTS  {label}" in output
    history = json.loads((tmp_state_dir / ".benchmark_history.json").read_text())
    assert history["runs"][0]["models"]["model"]["status"] == "success"


async def test_pool_rejections_count_without_spawning(tmp_path):
    session = SimpleNamespace(
        dispatcher=SimpleNamespace(submission={"runtime": "python"}),
        finishing=False,
        phase_name="building",
        model_id="vendor/model",
        turns=[],
    )
    pool = SubagentPool(session, Limits(subagent_cap=1))
    with pytest.raises(ValueError, match="already submitted"):
        await pool.spawn("call", {"name": "a", "task": "b"})
    session.dispatcher.submission = None
    session.finishing = True
    with pytest.raises(ValueError, match="the phase is finishing"):
        await pool.spawn("call", {"name": "a", "task": "b"})
    session.finishing = False
    pool.runs.append(SimpleNamespace(status="completed"))
    with pytest.raises(ValueError, match="agent cap reached"):
        await pool.spawn("call", {"name": "a", "task": "b"})
    assert pool.rejected == 3 and pool.remaining == 0
    assert pool.usage() == {
        "enabled": True,
        "parallel": 4,
        "cap": 1,
        "spawned": 1,
        "active": 0,
        "completed": 1,
        "failed": 0,
        "rejected": 3,
        "reminded": False,
    }
    assert isinstance(pool.semaphore, asyncio.Semaphore)
    assert module.MAX_TASK_CHARS == 24_000


def test_spawn_treats_null_read_only_as_false():
    assert SubagentPool.validate({"name": "a", "task": "b", "read_only": None}) == ("a", "b", False)
    assert SubagentPool.validate({"name": "a", "task": "b", "read_only": True})[2] is True


@pytest.mark.parametrize(
    "name,arguments,expected",
    [
        ("write_file", {"path": "a.py", "content": ""}, True),
        ("edit_file", {"path": "a.py", "old_text": "x", "new_text": "y"}, True),
        ("delete_file", {"path": "a.py"}, True),
        ("read_file", {"path": "a.py"}, False),
        ("list_files", {}, False),
        ("lint", {}, True),
        ("submit", {"runtime": "python", "entry": "a.py"}, True),
    ],
)
def test_spawn_calls_wait_for_file_changes_but_overlap_reads_and_spawns(name, arguments, expected):
    spawn = {"name": "spawn_agent", "arguments": {"name": "a", "task": "b"}}
    other = {"name": name, "arguments": arguments}
    assert Dispatcher._conflicts(other, spawn) is expected
    assert Dispatcher._conflicts(spawn, other) is expected
    assert Dispatcher._conflicts(spawn, spawn) is False
    search = {"name": "web_search", "arguments": {"query": "q"}}
    assert Dispatcher._conflicts(search, spawn) is False


@pytest.mark.parametrize(
    "name,arguments,message",
    [
        ("edit_file", {"old_text": "a", "new_text": "b"}, "edit_file requires path"),
        ("edit_file", {"path": "f.py"}, "edit_file requires old_text, new_text"),
        ("write_file", {"path": "f.py"}, "write_file requires content"),
        ("read_file", {}, "read_file requires path"),
        ("delete_file", {"path": None}, "delete_file requires path"),
        ("submit", {"entry": "main.py"}, "submit requires runtime"),
    ],
)
async def test_missing_required_fields_produce_readable_tool_errors(
    tmp_path, name, arguments, message
):
    from wavebench.harness.workspace import Workspace

    workspace = Workspace(tmp_path)
    try:
        dispatcher = Dispatcher(workspace, None, tmp_path, Limits())
        (result,) = await dispatcher.batch([{"id": "c1", "name": name, "arguments": arguments}])
    finally:
        workspace.close()
    assert not result["ok"] and result["error"] == message
