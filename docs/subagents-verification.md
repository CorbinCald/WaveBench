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

Automated coverage includes parallel execution bounded by the window, cap
enforcement and tool withdrawal, rejected nesting and submission, read-only
agents, the final-request notice and unexecuted pending calls, subagent failures
and shared-budget exhaustion that preserve the lead's finishing reserve, phase
and per-agent time limits, research quotas shared with the lead, spawning during
repair, disabled runs, the setup screen, CLI flags, orchestrator wiring, the
display column, and lifetime analytics.

Validation: `python -m pytest` — 1,176 passed, one paid test deselected;
`ruff check .`, `ruff format --check .`, and `git diff --check` passed.
Temporary settings, projects, logs, and recordings were removed after
verification.
