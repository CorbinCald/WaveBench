# Lifetime Harness analytics verification

Verified locally on 2026-09-12 through the real terminal CLI, using isolated
temporary settings, history, and generated projects. No paid API calls were made.

At 60 × 28, 80 × 28, and 120 × 28 terminal sizes, entered a prompt in the Harness
editor and benchmarked two local HTTP/SSE fixture models. One submitted a
working Python project directly; the other encountered a failed file read,
submitted a failing program, and repaired it. Both final programs printed `42`.
One response deliberately omitted cost and cache accounting.

The post-run summary and a subsequent `python -m wavebench --stats` agreed with
the saved results at every size:

- 2 successful model attempts, 1 first-pass success, and 1/1 repairs recovered.
- 1,000 output tokens, 10,000 prompt tokens, 11,000 total tokens, and 10 API turns.
- 10 tool calls with a 10% failure rate.
- At least $0.090 spent and at least $0.045 per successful run; neither partial
  cost was presented as complete.
- 80% cache hits with 1/2 runs measured; the other model's cache remained unknown.
- No compaction calls, with an explicit zero compaction cost.

All lifetime analytics lines fit each terminal width. The 80-column full report
also passed with `NO_COLOR=1`. Reading `--stats` left the history file byte-for-byte
unchanged. Through the real Settings menu, cycled through `speed`, `cache`,
`tool_fail`, `cost_per_pass`, and `p95`, saved, and confirmed `p95` persisted.

A single-process aggregation of 50,000 synthetic model results across 25 models
completed in 3.48 seconds locally. This measures aggregation, excluding JSON
loading and terminal rendering.

Regression coverage includes weighted rates, successful/failed/cancelled work,
partial usage and billing, legacy budgets, explicit zeros, invalid measurements,
repair recovery, latency percentiles, failure categories, all sorting modes,
more than ten models, large counters, and separate historical one-shot records.

Validation: `python -m pytest` — 1,104 passed, one paid test deselected;
`ruff check .`, `ruff format --check .`, and `git diff --check` passed.
Raw terminal recordings remain in temporary files outside the repository.
