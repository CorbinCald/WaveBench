# Token budgets and failure metrics

Harness dashboards keep `OUT TK` (generated output) separate from a per-model
`Budget used / limit tk; remaining left` line. Input is paid again on each request,
including cached input and compactor requests, so cumulative consumption can be
much larger than generated output. Web-search counts keep their existing column.
The budget line and failure explanation wrap at narrow widths without removing
the output, cost, cache, or tool metrics.

While streaming, the budget adds the current provider total when available, or
an input/output estimate with `~`. Native total-only snapshots have their own
output anchor so later snapshots do not count the same visible output twice.
Settled provider input, output, cache, and cost fields retain their original
meaning. Missing usage and costs stay unknown; estimates never become provider
usage. A budget with any estimated turn retains `~` after the turn ends.

Results preserve `harness.budget_tokens` for compatibility and add
`harness.budget` with `used_tokens`, `limit_tokens`, `remaining_tokens`,
`estimated`, and `next_input_tokens_estimate`. Top-level `failure` contains a
stable category, code, summary, phase, and relevant bounded diagnostics or budget
details. Categories distinguish stream limits, token budgets, response/tool
protocol, project runtime, environment setup, other Harness limits, cancellation,
and unknown failures. A later successful repair clears the terminal failure.
Legacy results still load and can derive readable summaries from their older
error/budget fields. History preserves the added records, displays the recent
failure/budget details, and uses wrapped analytics rows at narrow widths.

## Interactive verification — 2026-09-11

Ran the real interactive `python -m wavebench --mode harness --open off` prompt
editor and benchmark dashboard through PTYs at **60, 80, and 120 columns**. Each
run used a separate temporary working directory with its own model/config,
output, and history files. Typed a safe workspace-listing request into the prompt
editor. The application made one local HTTP SSE request per fixture model at
each width. Also opened the resulting history with the public
`python -m wavebench --stats` command in PTYs at all three widths.

The Gemini-named fixture used synthetic cumulative usage seeded from issue #34
and a calibrated next-input estimate. Its local SSE response dispatched a real
`wb ls`, reached **78,094 output / 934,688 cumulative tokens**, and then rejected
the next **80,687-token estimated input** with **65,312 tokens remaining**. Its
saved category was `token_budget`. These numbers are replayed fixture accounting,
not a new Gemini provider measurement.

The DeepSeek-named fixture returned valid SSE JSON whose 1,080 generated UTF-8
bytes exceeded a deliberately small 256-byte output guard. Its saved category
was `stream_limit`, code `stream_output_limit`, with 1,187 raw bytes recorded.
It made no tool calls and reported no provider usage; the output/cost cells
remained unknown and the conservative cumulative budget displayed `~`.

Sanitized lines visible at every tested width:

```text
Budget 934,688 / 1,000,000 tk; 65,312 left
Token budget exhausted
65,312 tokens remain; next input ~80,687

Budget ~566 / 1,000,000 tk; ~999,434 left
Stream limit reached (output bytes)
```

All three interactive processes and history commands exited successfully. The
final results and history frames were checked for lines wider than their terminal;
none remained. The final verification used the production sandbox preflight at
every width after the host's documented Bubblewrap profile was installed. Only
the ordinary workspace `ls` tool ran; these failure fixtures stop before project
execution. This verifies presentation, transport failure propagation, and
accounting, separately from paid provider generation and runtime verification.

Raw recordings and fixture settings stayed in temporary files. No prompt bodies,
credentials, response excerpts, or personal paths are included here.

## Automated verification

The following targeted suite passed **162 tests**, including native total-only
snapshots, unknown usage, cached-input accounting, the Gemini arithmetic, distinct
failure categories, HTTP error/cancellation propagation into saved results,
legacy history, terminal widths, and finishing-budget controller behavior:

```bash
python -m pytest tests/unit/test_harness_failure_metrics.py \
  tests/integration/test_harness_failure_results.py \
  tests/unit/test_harness_progress.py tests/unit/test_storage.py \
  tests/unit/test_orchestrator_backstop.py \
  tests/integration/test_finishing_budget.py
```

Ruff lint and formatting checks passed for the changed implementation and tests.
