"""A delegating model's agents fit on one line beneath its row at every width."""

from __future__ import annotations

import os
import re
from types import SimpleNamespace

import pytest

from wavebench.tui.progress import ProgressTracker
from wavebench.tui.progress import tracker as module

SPIN = "[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]"


def plain(text: str) -> str:
    return re.sub(r"\033\[[0-9;?]*[a-zA-Z]", "", text).replace("\r", "")


def line(tracker: ProgressTracker, width: int = 112, name: str = "lead") -> str | None:
    text = tracker._format_subagent_line(name, width)
    return None if text is None else plain(text)


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 100.0}
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: state["now"]))
    return state


@pytest.fixture
def delegating(clock):
    tracker = ProgressTracker(1, {}, model_names=["lead"])
    tracker.update_harness("lead", {"api_turns": 3, "completion_tokens": 900, "cost": 0.02}, 6.0)
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
    tracker.update_subagent("lead", 2, status="linting", turn=3, settled_tokens=610)
    clock["now"] += 9.5
    tracker.update_subagent("lead", 3, status="completed", turn=3, settled_tokens=805)
    return tracker


def test_one_line_shows_each_agent_with_its_state_and_request_progress(delegating):
    text = line(delegating)
    assert text.startswith("  ╰ ") and "\n" not in text
    # The bar counts the request in progress: request 2 of 10 fills two of six blocks.
    assert re.fullmatch(
        rf"  ╰ {SPIN} home-page    ▰▰▱▱▱▱      {SPIN} about-page   ▰▰▱▱▱▱      "
        r"✓ contact-page ▰▰▱▱▱▱",
        text,
    )
    assert "01-" not in text and "tk" not in text  # Output detail is for a lone agent.


def slots(text: str) -> list[int]:
    return [match.start() for match in re.finditer(rf"(?:{SPIN}|[✓✗○●]) \S", text)]


def test_agents_keep_their_positions_as_they_stall_and_finish(delegating, clock):
    start = slots(line(delegating))
    clock["now"] += 2.5  # home-page has streamed nothing for 12 s.
    stalled = line(delegating)
    assert re.search(r"● home-page    idle 12s    ", stalled)
    delegating.update_subagent("lead", 2, status="budget_exhausted")
    ended = line(delegating)
    assert re.search(r"✗ about-page   no budget   ", ended)
    delegating.update_subagent("lead", 1, status="completed")
    assert slots(stalled) == slots(ended) == slots(line(delegating)) == start


def test_a_lone_running_agent_shows_its_output_and_rate(delegating, clock):
    delegating.update_subagent("lead", 2, status="completed")
    clock["now"] += 0.5
    delegating.update_subagent("lead", 1, output_tokens=300)
    # 300 streamed tokens over the 10 s since this request began.
    text = line(delegating)
    assert text.endswith("✓ contact-page ▰▰▱▱▱▱   700 tk · ~30 tk/s")
    assert re.search(rf"{SPIN} home-page    ▰▰▱▱▱▱      ✓ about-page", text)


@pytest.mark.parametrize("width", [16, 24, 32, 44, 52, 72, 90, 112])
def test_line_fits_every_width_and_keeps_every_state_visible(delegating, width):
    text = line(delegating, width)
    assert "\n" not in text and len(text) <= width
    assert text.startswith("  ╰ ") and re.search(SPIN, text) and "✓" in text


def test_folding_keeps_running_and_failed_agents_named(clock):
    tracker = ProgressTracker(1, {}, model_names=["lead"])
    names = ["weapons", "hud", "maps", "bots", "audio", "netcode", "menus", "physics", "lobby"]
    for number, name in enumerate(names, 1):
        tracker.update_subagent(
            "lead", number, label=f"{number:02d}-{name}", status="waiting", max_turns=16
        )
    for number in (1, 2, 5):
        tracker.update_subagent("lead", number, status="completed")
    tracker.update_subagent("lead", 3, status="streaming", turn=9, output_tokens=40)
    tracker.update_subagent("lead", 4, status="tools", turn=5)
    tracker.update_subagent("lead", 6, status="turn_limit", turn=16)

    assert "✓ weapons" in line(tracker, 200)  # Every agent keeps its slot when it fits.
    wide = line(tracker, 112)
    # Finished agents fold first, then waiting ones; running and failed agents stay named.
    assert re.fullmatch(
        rf"  ╰ ✓ 3 done   {SPIN} maps    ▰▰▰▰▱▱      {SPIN} bots    ▰▰▱▱▱▱      "
        r"✗ netcode no turns    ○ 3 waiting",
        wide,
    )
    assert re.fullmatch(
        rf"  ╰ ✓ 3 done  {SPIN} maps     {SPIN} bots     ✗ netcode  ○ 3 waiting",
        line(tracker, 90),
    )
    assert re.fullmatch(rf"  ╰ ✓✓✓✗{SPIN}{SPIN}○○○  2 running · 3 waiting", line(tracker, 40))
    assert re.fullmatch(rf"  ╰ ✓✓✓✗{SPIN}{SPIN}○○○", line(tracker, 24))


def test_a_silent_stream_turns_idle_but_reasoning_does_not(delegating, clock):
    clock["now"] += 2.5  # home-page has streamed nothing for 12 s.
    assert "● home-page    idle 12s" in line(delegating)
    delegating.update_subagent("lead", 2, status="thinking", turn=4)
    clock["now"] += 30
    text = line(delegating)
    assert "● home-page    idle 42s" in text
    assert re.search(rf"{SPIN} about-page   ▰▰▰▱▱▱", text)  # Reasoning is not idle.
    delegating.update_subagent("lead", 1, output_tokens=200)
    assert re.search(rf"{SPIN} home-page    ▰▰▱▱▱▱", line(delegating))
    clock["now"] += 200
    assert "● home-page    idle 3m" in line(delegating)


def test_line_outlives_lead_turns_then_keeps_names_until_it_must_fold(delegating):
    delegating.start_harness_turn("lead", 500)
    assert "home-page" in line(delegating)  # No flicker between the lead's turns.
    before = slots(line(delegating))
    for number in (1, 2):
        delegating.update_subagent("lead", number, status="completed")
    done = line(delegating)
    assert re.fullmatch(
        r"  ╰ ✓ home-page +▰▰▱▱▱▱ +✓ about-page +▰▰▱▱▱▱ +✓ contact-page ▰▰▱▱▱▱", done
    )
    assert slots(done) == before  # Ending a delegation moves nothing.
    assert line(delegating, 40) == "  ╰ ✓ 3 agents done"
    delegating.update_subagent("lead", 4, label="04-late-fix", status="budget_exhausted")
    assert line(delegating, 60) == "  ╰ ✓ 3 agents done   ✗ late-fix no budget"
    delegating.update_subagent(
        "lead-2", 1, label="01-integration-pass", status="completed", turn=4, max_turns=10
    )
    assert line(delegating, name="lead-2") == "  ╰ ✓ integration-pass ▰▰▰▱▱▱"
    assert line(delegating, 24, name="lead-2") == "  ╰ ✓ 1 agent done"
    delegating.set_phase("lead", "finished")
    assert line(delegating, 60) == "  ╰ ✓ 3 agents done   ✗ late-fix no budget"  # Lasts the run.


def test_agents_column_shows_running_over_spawned(delegating):
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
        tracker.update_subagent("lead", number, label=f"0{number}-part{number}", status="waiting")
    for number, status in enumerate(("thinking", "streaming", "tools", "linting"), 1):
        tracker.update_subagent("lead", number, status=status, turn=1, max_turns=16)
    assert "4/6" in plain(tracker._format_harness_row("lead", 112)).split()
    text = line(tracker)
    assert len(re.findall(rf"{SPIN} part", text)) == 4
    assert text.endswith("○ 2 waiting")
    assert line(tracker, 200).endswith("○ part5 ▱▱▱▱▱▱      ○ part6 ▱▱▱▱▱▱")


async def render(tracker, monkeypatch, lines: int) -> list[str]:
    frames = []

    def capture(frame):
        frames.append(plain(frame))
        tracker._running = False

    monkeypatch.setattr(tracker, "_flush_frame", capture)
    monkeypatch.setattr(
        module.shutil, "get_terminal_size", lambda *args: os.terminal_size((120, lines))
    )
    tracker._running = True
    await tracker._animate()
    return frames[0].splitlines()


async def test_live_frame_adds_the_agent_line_when_it_fits(delegating, monkeypatch):
    # Six chrome lines leave lines-6 rows: the model row, then its agent line.
    for lines, agent_lines in ((40, 1), (8, 1), (7, 0)):
        frame = await render(delegating, monkeypatch, lines)
        assert len(frame) <= lines
        assert sum("╰ " in row for row in frame) == agent_lines
        assert "2/3" in "\n".join(frame) and "more…" not in "\n".join(frame)


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
    # Six chrome lines and five model rows; agent lines take what is left, in order.
    for lines, agent_lines in ((40, 2), (12, 1), (11, 0)):
        frame = await render(tracker, monkeypatch, lines)
        assert len(frame) <= lines
        assert all(any(f" {name} " in row for row in frame) for name in names), lines
        assert not any(re.search(r"\+\d+ more…", row) for row in frame)
        assert sum("╰ " in row for row in frame) == agent_lines, lines


async def test_live_delegations_take_spare_rows_before_finished_ones(clock, monkeypatch):
    results = {}
    tracker = ProgressTracker(3, results, model_names=["done", "solo", "live"])
    for name in ("solo", "live"):
        tracker.update_harness(name, {"api_turns": 3}, 6.0)
        tracker.set_phase(name, "building")
    tracker.update_subagent("done", 1, label="01-finished-part", status="completed", max_turns=16)
    tracker.update_subagent("live", 1, label="01-running-part", status="streaming", max_turns=16)
    results["done"] = {
        "status": "success",
        "time_s": 30,
        "usage": {"api_turns": 4},
        "harness": {"subagents": {"enabled": True, "spawned": 1}},
    }
    # Six chrome lines and three model rows leave one spare row at ten lines.
    frame = await render(tracker, monkeypatch, 10)
    assert any("running-part" in row for row in frame)
    assert not any("finished-part" in row for row in frame)
    frame = await render(tracker, monkeypatch, 40)
    assert any("✓ finished-part" in row for row in frame)  # Finished models keep theirs.
