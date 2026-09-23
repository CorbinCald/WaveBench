"""The delegation HUD shows running agents, their progress, and the lead's position."""

from __future__ import annotations

import os
import re
from types import SimpleNamespace

import pytest

from wavebench.tui.progress import ProgressTracker
from wavebench.tui.progress import tracker as module


def plain(text: str) -> str:
    return re.sub(r"\033\[[0-9;?]*[a-zA-Z]", "", text).replace("\r", "")


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 100.0}
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: state["now"]))
    return state


@pytest.fixture
def delegating(clock):
    tracker = ProgressTracker(1, {}, model_names=["lead"])
    tracker.update_harness(
        "lead",
        {"api_turns": 3, "completion_tokens": 900, "cost": 0.02, "total_tokens": 12_000},
        6.0,
        budget={"used_tokens": 12_000, "limit_tokens": 400_000, "remaining_tokens": 388_000},
    )
    tracker.update_harness_phase("lead", turn=4, max_turns=32, active_s=61.0, max_s=900)
    tracker.update_harness_tools(
        "lead",
        {"calls": 7, "failures": 0},
        subagents={"enabled": True, "spawned": 3, "active": 2, "completed": 1, "cap": 3},
    )
    tracker.set_phase("lead", "delegating")
    tracker.update_subagent("lead", 1, label="01-home-page", status="waiting", max_turns=10)
    tracker.update_subagent("lead", 2, label="02-about-page", status="waiting", max_turns=10)
    tracker.update_subagent("lead", 3, label="03-contact-page", status="waiting", max_turns=10)
    tracker.update_subagent("lead", 1, status="thinking", turn=2, settled_tokens=400)
    tracker.update_subagent("lead", 1, status="streaming", output_tokens=150)
    tracker.update_subagent("lead", 2, status="linting", turn=3, settled_tokens=610, tool_calls=2)
    clock["now"] += 9.5
    tracker.update_subagent(
        "lead", 3, status="completed", turn=3, settled_tokens=805, tool_calls=2, finished=109.5
    )
    return tracker


def test_agent_rows_show_status_turn_tokens_rate_tools_and_time(delegating, clock):
    clock["now"] += 0.5
    rows = [plain(row) for row in delegating._format_subagent_rows("lead", 112)]
    assert rows[0].startswith("    ↳ agents 2 running · 1 done · cap 3/3")
    assert "lead turn 4/32 · ~1m 11s/15m" in rows[0]
    assert "388k tk left" in rows[0]
    assert [row.split()[0] for row in rows[1:]] == [
        "01-home-page",
        "02-about-page",
        "03-contact-page",
    ]
    assert "streaming" in rows[1] and "turn 2/10" in rows[1] and "550 tk" in rows[1]
    assert "~15 tk/s" in rows[1]  # 150 streamed tokens over the 10 s since the sample began.
    assert "0 tools" in rows[1] and "10.0s" in rows[1]
    assert "linting" in rows[2] and "610 tk" in rows[2] and "2 tools" in rows[2]
    assert "done ✓" in rows[3] and "9.5s" in rows[3] and "tk/s" not in rows[3]
    assert all(len(row) <= 112 for row in rows)


@pytest.mark.parametrize("width", [32, 44, 52, 72, 90, 112])
def test_agent_rows_fit_every_width_and_keep_status_and_time(delegating, width):
    rows = [plain(row) for row in delegating._format_subagent_rows("lead", width)]
    assert len(rows) == 4
    assert all(len(row) <= width for row in rows)
    assert "↳ agents 2 running" in rows[0][:width]
    for row in rows[1:]:
        assert re.search(r"\d+(\.\d)?s$", row) or re.search(r"\d+m( \d+s)?$", row)
    assert "streaming" in rows[1] and "done" in rows[3]


def test_row_budget_truncates_agents_instead_of_hiding_the_model(delegating):
    assert delegating._format_subagent_rows("lead", 112, max_rows=0) == []
    two = [plain(row) for row in delegating._format_subagent_rows("lead", 112, max_rows=2)]
    assert two[0].startswith("    ↳ agents") and two[1].strip() == "+3 more agents…"
    three = [plain(row) for row in delegating._format_subagent_rows("lead", 112, max_rows=3)]
    assert "01-home-page" in three[1] and three[2].strip() == "+2 more agents…"
    assert len(delegating._format_subagent_rows("lead", 112, max_rows=4)) == 4
    assert len(delegating._format_subagent_rows("lead", 112, max_rows=9)) == 4


def test_live_agents_column_shows_running_over_spawned(delegating):
    for width in (52, 72, 112):
        row = plain(delegating._format_harness_row("lead", width))
        assert "2/3" in row.split(), row
        assert len(row) == width
    delegating.update_harness_tools(
        "lead", {"calls": 9, "failures": 0}, subagents={"enabled": True, "spawned": 3, "active": 0}
    )
    assert "3" in plain(delegating._format_harness_row("lead", 112)).split()
    assert "2/3" not in plain(delegating._format_harness_row("lead", 112))
    result = {
        "status": "success",
        "time_s": 5,
        "usage": {},
        "harness": {"subagents": {"enabled": True, "spawned": 3, "active": 2}},
    }
    assert "2/3" not in plain(delegating._format_harness_row("lead", 112, result))


def test_next_lead_turn_and_finish_clear_the_batch(delegating):
    assert delegating._subagents["lead"]
    delegating.start_harness_turn("lead", 500)
    assert "lead" not in delegating._subagents
    assert delegating._format_subagent_rows("lead", 112) == []
    delegating.update_subagent("lead", 4, label="04-late", status="waiting")
    delegating.set_phase("lead", "finished")
    assert "lead" not in delegating._subagents


async def test_live_frame_renders_the_hud_within_the_terminal_height(delegating, monkeypatch):
    frames = []

    def capture(frame):
        frames.append(plain(frame))
        delegating._running = False

    monkeypatch.setattr(delegating, "_flush_frame", capture)
    # 3 chrome lines above the rows and 3 below leave lines-6 rows for the model;
    # the HUD head survives down to one spare row, then the model row stands alone.
    for lines, expected_agents in ((40, 3), (12, 3), (10, 1), (8, 0), (7, None)):
        monkeypatch.setattr(
            module.shutil,
            "get_terminal_size",
            lambda *args, lines=lines: os.terminal_size((120, lines)),
        )
        frames.clear()
        delegating._running = True
        await delegating._animate()
        frame = frames[0]
        assert len(frame.splitlines()) <= lines
        assert frame.count("↳ agents") == (0 if expected_agents is None else 1)
        assert sum("-page" in line for line in frame.splitlines()) == (expected_agents or 0)
        assert "more…" not in frame  # The model row itself is never hidden by its agents.
        assert "2/3" in frame


def test_waiting_agents_are_counted_apart_from_running(clock):
    tracker = ProgressTracker(1, {}, model_names=["lead"])
    tracker.update_harness("lead", {"api_turns": 2}, 4.0)
    tracker.update_harness_tools(
        "lead",
        {"calls": 5, "failures": 0},
        subagents={"enabled": True, "spawned": 6, "active": 6, "cap": 12, "parallel": 4},
    )
    tracker.set_phase("lead", "delegating")
    for number in range(1, 7):
        tracker.update_subagent("lead", number, label=f"0{number}-part", status="waiting")
    for number, status in enumerate(("thinking", "streaming", "tools", "linting"), 1):
        tracker.update_subagent("lead", number, status=status, turn=1, max_turns=16)
    rows = [plain(row) for row in tracker._format_subagent_rows("lead", 112)]
    assert rows[0].startswith("    ↳ agents 4 running · 2 waiting · 0 done · cap 6/12")
    assert "turn" not in rows[5] and "turn" not in rows[6]  # No request made yet.
    assert "4/6" in plain(tracker._format_harness_row("lead", 112)).split()

    # A queued agent's time restarts when it gains a slot, like its recorded time_s.
    clock["now"] += 7.0
    assert plain(tracker._format_subagent_rows("lead", 112)[6]).endswith("7.0s")
    tracker.update_subagent("lead", 1, status="completed", finished=clock["now"])
    tracker.update_subagent("lead", 5, status="thinking", turn=1)
    clock["now"] += 2.0
    rows = [plain(row) for row in tracker._format_subagent_rows("lead", 112)]
    assert rows[0].startswith("    ↳ agents 4 running · 1 waiting · 1 done")
    assert rows[1].endswith("7.0s") and rows[5].endswith("2.0s") and rows[6].endswith("9.0s")


async def test_delegating_models_never_hide_other_models(clock, monkeypatch):
    names = ["gpt6Astra", "claudeOpus5.5", "grok4.7", "gemini3.8Flash", "mimoV2.6Pro"]
    tracker = ProgressTracker(len(names), {}, model_names=names)
    for name, spawned in zip(names, (8, 3, 0, 0, 0), strict=True):
        tracker.update_harness(name, {"api_turns": 3}, 6.0)
        tracker.update_harness_tools(
            name,
            {"calls": 7, "failures": 0},
            subagents={"enabled": True, "spawned": spawned, "active": spawned, "cap": 12},
        )
        tracker.set_phase(name, "delegating" if spawned else "building")
        for number in range(1, spawned + 1):
            tracker.update_subagent(name, number, label=f"0{number}-part", status="streaming")
    frames = []

    def capture(frame):
        frames.append(plain(frame))
        tracker._running = False

    monkeypatch.setattr(tracker, "_flush_frame", capture)
    # Six chrome lines plus five model rows leave the rest for the two HUDs.
    for lines, heads, agents in ((40, 2, 11), (18, 2, 3), (13, 2, 0), (12, 1, 0), (11, 0, 0)):
        monkeypatch.setattr(
            module.shutil,
            "get_terminal_size",
            lambda *args, lines=lines: os.terminal_size((120, lines)),
        )
        frames.clear()
        tracker._running = True
        await tracker._animate()
        frame = frames[0].splitlines()
        assert len(frame) <= lines
        assert all(any(f" {name} " in line for line in frame) for name in names), lines
        assert not any(re.search(r"\+\d+ more…", line) for line in frame)
        assert sum("↳ agents" in line for line in frame) == heads, lines
        assert sum("-part " in line for line in frame) == agents, lines


async def test_hud_rows_do_not_starve_other_models(delegating, monkeypatch):
    delegating._model_names = ["lead", "other"]
    delegating._total = 2
    delegating.update_harness("other", {"api_turns": 1}, 1.0)
    delegating.set_phase("other", "building")
    frames = []

    def capture(frame):
        frames.append(plain(frame))
        delegating._running = False

    monkeypatch.setattr(delegating, "_flush_frame", capture)
    monkeypatch.setattr(
        module.shutil, "get_terminal_size", lambda *args: os.terminal_size((120, 11))
    )
    delegating._running = True
    await delegating._animate()
    frame = frames[0]
    assert "other" in frame and "lead" in frame
    assert len(frame.splitlines()) <= 11
