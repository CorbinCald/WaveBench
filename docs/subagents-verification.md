# Subagents verification

Verified locally on 2026-09-21 using the real interactive WaveBench CLI in
100 × 30 and 100 × 34 PTYs, with temporary settings, models, and outputs
isolated from the maintainer's directory.

- Startup displayed `Subagents: Off [s]`. The `s` shortcut opened the setup
  screen; Space enabled it, Space on the second row cycled the parallel window to
  5, and typing replaced the total cap with 10. An empty cap displayed a readable
  error and did not save. Enter returned to the menu showing `Subagents: On [s]`,
  and the model summary showed `Subagents: On (5 parallel, 10 total)`. The saved
  configuration contained `subagents: on`, `harness.subagent_parallel: 5`, and
  `harness.subagent_cap: 10`.
- Settings → Subagents (Harness) opened the same screen. Disabling, adjusting the
  cap with arrow keys, re-enabling, and saving the Settings page persisted the
  new cap while other Harness limits were preserved. Esc cancelled without
  changing the configuration.

## Real LLM subagent use

Also verified on 2026-09-21 with paid OpenRouter calls to two real model families
through the unchanged production HarnessSession, streaming transport,
dispatcher, subagent pool, file tools, lint, and sandbox execution. Both ran with
Low reasoning effort, `--open off`, a 400,000-token budget, and a parallel
window and cap of three agents.

The task asked for a three-page static website sharing one stylesheet and one
script, with each page delegated to a separate subagent that must not touch the
shared files, then lint and a `static` submission.

| Model | Spawn pattern | Agent turns | Lead turns | Runtime | OpenRouter cost |
|---|---|---:|---:|---|---:|
| `google/gemini-3.8-flash` | Three `spawn_agent` calls in one turn; all three agents started together and overlapped | 6 / 3 / 3 | 6 | Passed | $0.0494 |
| `anthropic/claude-haiku-4.5` | One `spawn_agent` call per turn; agents ran one after another | 7 / 6 / 10 | 8 | Passed | $0.1484 |

Each agent wrote only its own page. Every lead brief named the file the agent
owned, the shared files it must not modify, the markup contract, and the
required report; every report described the file written and confirmed the
shared files were untouched. Both leads then read the pages, linted, and
submitted with their own `done`. The generated sites loaded in the sandbox.

Saved results recorded `spawned: 3`, `completed: 3`, and no rejections for both
models, with subagent usage as a subset of the model's totals (Haiku: 23 of 31
API turns and $0.105 of $0.148). The final table showed the `AGT` column with 3
for each model; lifetime analytics showed `agents 6`. Each agent's brief,
conversation, tool records, and report were saved under
`metadata/<model-slot>/subagents/`.

## Live delegation HUD

Verified on 2026-09-21 with a second paid run of `google/gemini-3.8-flash`
through the real interactive CLI in a 120 × 40 PTY, same task and limits. The
recorded frames showed the AGENTS column at `3/3` while all three agents ran
and `2/3` after the first finished, the head line advancing from
`3 running · 0 done` to `2 running · 1 done` with `cap 3/3`, the lead's
`turn 3/32` and active time against the 15-minute limit, and the remaining
budget falling as agents spent tokens. Agent rows moved through `thinking`,
`streaming` with an interval rate, `linting`, and `done ✓`, with request
numbers against the 10-request limit, output tokens, tool counts, and elapsed
time. The block disappeared when the lead's next request began; the final table
showed the AGENTS column with 3. The run passed at $0.080.

## Queued agents and several delegating models

A benchmark on 2026-09-22 with five models, subagents at 4 parallel and a cap
of 12, and web search on ran with subagents switched off, so no HUD appeared.
Rendering that configuration showed three faults the single-model check had
missed: agents waiting for a slot were counted as running (`8 running`, `8/8`
with four slots), the first delegating models' agent rows hid the models after
them on a 24-line terminal (`+3 more…`), and the AGENTS column cut model names
such as `claudeOpus5.5` to `claudeOp…` at 120 columns.

Verified on 2026-09-22 through the real CLI in a PTY with isolated settings and
a scripted offline model in place of the provider call (every other layer
real): three models, 2 parallel slots, cap 6, one lead spawning five agents and
one spawning three while the third built alone. At 110 × 22 the AGENTS column
read `2/5` with `2 running · 3 waiting · 0 done`, waiting agents showed their
queue time and restarted their clocks on gaining a slot, and all three models
stayed visible in all 271 HUD frames. At 124 × 16 the two HUDs shared the spare
rows (head, one agent, and `+N more agents…` each) with the solo model still
visible in all 357 HUD frames, and no frame exceeded the terminal height. Both
runs passed and lifetime analytics recorded `agents 8`.

## One agent line per model

On 2026-09-22 the per-agent heads-up display was replaced with one line beneath
each delegating model, after the multi-row display proved crowded in a live
four-model run. Verified through the real CLI in a 124 × 24 PTY in the `pear`
theme with isolated settings and a scripted offline model in place of the
provider call: four models, two parallel slots, cap 6, one lead spawning three
agents and one spawning one while two built alone, with one agent's stream
paused for 13 seconds. Across 488 live frames every model stayed visible, no
frame exceeded the terminal, and each agent kept one column for the whole run
(`gameplay-engine`, `scene-render`, and `visual-ui` at columns 10, 40, and 70)
through waiting, running, idle, done, and after both the delegation and the
model finished. The lone agent showed its tokens and rate, the paused agent
showed `idle 12s`, and all four models passed.

Rendered frames received independent reviews from `google/gemini-3.8-flash`.
The first design-intent review found that variable-width items shifted their
neighbours as states changed and that collapsing a finished delegation to a
count hid which agents ran; fixed-width slots and retained names addressed
both. A second found the line vanished when the delegating model finished; it
now lasts the run, with live delegations taking spare rows first. A blind
review found the waiting `○` and idle `◌` hard to tell apart; idle now uses a
yellow `●`, matching the app's existing attention glyph. Blind reviews also
noted that agents do not align to the table's columns, which is the chosen
one-line direction. The final design-intent review was rejected by the review
helper as incomplete twice and was not repeated.

## Adoption without a delegation hint

A benchmark run on 2026-09-21 with four models and subagents enabled (cap 12)
on a large single-prompt game task produced no `spawn_agent` calls at all: the
tool and instructions were present in every request, nothing was rejected, and
every model chose to research or write files itself. The lead instructions were
rewritten to be directive (delegate unless the project fits in one or two small
files; decide the layout and shared contracts, write that scaffolding, spawn one
agent per independent module in the same turn, then integrate), the tool
description gained the same rule, spawn calls now wait for file changes earlier
in the same batch, and a lead that has written two files itself without
spawning receives one reminder.

A paid re-run of the same prompt (`Recreate as accurately as possible, the
first level of Super Mario 64, but as a zombie shooting game, playable in
browser with three.js.`) with three cheap models, Low effort, web search off,
a 400,000-token budget, and three parallel agents from a cap of six, before the
reminder existed:

| Model | Behavior | Result |
|---|---|---|
| `z-ai/glm-5.3-flash` | Wrote `index.html`, then spawned `engine`, `level`, and `zombies` agents in the same turn with contracts for the globals each file exposes; all three overlapped; integrated with `main.js`, linted, submitted | Passed, $0.037 |
| `google/gemini-3.8-flash` | Planned a seven-file layout and wrote every file itself, one per turn, without spawning | Passed, $0.185 |
| `deepseek/deepseek-v4.1-flash` | First response spent the test's 16,000-token per-turn output allowance on reasoning (14,380 reasoning tokens at 33 tk/s) before any tool call | Failed (output allowance), $0.010 |

The reminder targets Gemini's pattern: after its second solo file it would now
be told how many agents can run and asked to delegate the remaining modules.
Adoption remains a model decision that the benchmark records; the harness
guarantees the tool, the instructions, and one reminder.

Automated coverage includes parallel execution bounded by the window, cap
enforcement and tool withdrawal, rejected nesting and submission, read-only
agents, the final-request notice and unexecuted pending calls, subagent failures
and shared-budget exhaustion that preserve the lead's finishing reserve, phase
and per-agent time limits, research quotas shared with the lead, spawning during
repair, disabled runs, the setup screen, CLI flags, orchestrator wiring, the
display column, the delegation HUD at every width and terminal height,
lifetime analytics, the one-time reminder, null optional fields, spawn ordering
against file changes in a batch, and readable errors for tool calls missing
required fields.

Validation: `python -m pytest` — 1,203 passed, one paid test deselected;
`ruff check .`, `ruff format --check .`, and `git diff --check` passed.
Temporary settings, projects, logs, and recordings were removed after
verification.
