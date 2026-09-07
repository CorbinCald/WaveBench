# Harness dashboard metrics verification

Verified locally on 2026-09-06 through the real interactive `python -m wavebench`
CLI in an 80 × 28 terminal. Selected Harness, entered a two-file Python repair
task, and ran Luna (`openai/gpt-5.6-luna`) and Haiku
(`anthropic/claude-haiku-4.5`) concurrently with isolated test configuration.

Both models intentionally failed their first execution, then repaired the
project and printed `42`. Streaming frames showed estimated tokens and speed,
provider cost from completed calls, and the current turn. Metrics remained
visible as the models progressed independently from building to repair.

Final dashboard metrics matched the saved provider usage and API timing:

| Model | Total tokens | Output tk/s | Displayed cost | Turns | Executions |
|---|---:|---:|---:|---:|---:|
| Luna | 7,380 | 46 | $0.001 | 6 | 2 |
| Haiku | 16,800 | 69 | $0.021 | 7 | 2 |

Unrounded combined cost was $0.02258744. Both final results passed. Captured
dashboard rows fit the 80-column terminal without wrapping.

Automated checks also cover compaction, successful and failed repairs,
cancelled results, missing usage/cost, streaming counter resets, short terminals,
and metrics in building, compacting, linting, running, and repairing phases
at 80 and 110 columns.

Validation: `pytest -q` — 664 passed, one live test deselected;
`ruff check .`, `ruff format --check .`, and `git diff --check` passed.
