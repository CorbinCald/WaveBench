# Harness dashboard metrics verification

Verified locally on 2026-09-06 through the real interactive `python -m wavebench`
CLI in an 80 × 28 terminal. Selected Harness, entered a two-file Python repair
task, and ran Luna (`openai/gpt-5.6-luna`) and Haiku
(`anthropic/claude-haiku-4.5`) concurrently with isolated test configuration.
Used long display names to exercise name truncation in the single-line layout.

Both models intentionally failed their first execution, then repaired the
project and printed `42`. Streaming frames showed estimated tokens and speed,
provider cost from completed calls, and the current turn. Metrics remained
visible on the same line as each model's name and status as the models
progressed independently from building to repair.

Final dashboard metrics matched the saved provider usage and API timing:

| Model | Total tokens | Output tk/s | Displayed cost | Turns | Executions |
|---|---:|---:|---:|---:|---:|
| Luna | 7,411 | 44 | $0.001 | 6 | 2 |
| Haiku | 13,104 | 84 | $0.016 | 6 | 2 |

Both final results passed. Captured dashboard rows fit the 80-column terminal
without wrapping or placing metrics beneath a model:

```text
  │  1. ✓ Haiku-generation-mo… passed  13,104 tk 84 tk/s $0.016 6 turns 9.5s │
  │  2. ✓ Luna-generation-mod… passed  7,411 tk 44 tk/s $0.001 6 turns 10.6s │
```

Automated checks also cover compaction, successful and failed repairs,
cancelled results, missing usage/cost, streaming counter resets, short terminals,
and metrics in building, compacting, linting, running, and repairing phases
at 80 and 110 columns.

Validation: `pytest -q` — 671 passed, one live test deselected;
`ruff check .`, `ruff format --check .`, and `git diff --check` passed.

## Cache and tool metrics — 2026-09-07

Ran the real interactive `python -m wavebench --mode harness --open off` CLI
in 80 × 28 and 120 × 28 PTYs with isolated temporary settings and outputs.
A local HTTP/SSE fixture supplied two concurrent models, intermediate usage
reports, and native write/read/lint/done calls. The real dispatcher and sandbox
executed the projects. No paid API calls were made.

One model attempted a missing-file read, built a program that failed execution,
then repaired it. The other model completed successfully without reporting
cache usage. Both final programs printed `42`.

| Model fixture | Tools used | Tool failures | Cache hit | API turns | Program runs |
|---|---:|---:|---:|---:|---:|
| Cache | 8 | 12.5% (1/8) | 84.8% (17,800/21,000) | 6 | 2 |
| Missing cache usage | 3 | 0.0% (0/3) | — | 3 | 1 |

All 187 captured generation frames fit their terminal width and height.
At 80 columns, the new metrics appeared below each model; at 120 columns,
they fit beside the existing metrics. Counts survived the repair and matched
the saved `harness.tool_usage` fields. Intermediate cache reports updated the
percentage during streaming, and final values matched `usage.cache_read_ratio`.
Sanitized final detail lines:

```text
cache hit — · tools used 3 · tool fail 0.0%
cache hit 84.8% · tools used 8 · tool fail 12.5%
```

Automated regressions also cover partial parallel batches, failed lint,
cancelled/queued tools, idempotent replays, invalid or missing cache counts,
compaction, terminal outcomes, and hiding complete model rows in short terminals.
Raw recordings and generated projects stayed under `/tmp`.

Validation: `python -m pytest` — 742 passed, one paid test deselected;
`ruff check .`, `ruff format --check .`, and `git diff --check` passed.
The existing lint-isolation test failed on the first full run, then passed both
in isolation and in the full rerun without code changes.
