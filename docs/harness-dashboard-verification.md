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
