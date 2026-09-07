# Harness accounting verification

Verified locally on 2026-09-06 after investigating the supplied live dashboard
screenshot and the corresponding run's saved provider usage.

## Findings and corrections

The screenshot's $0.79 was a rounded subtotal of completed calls. Live token
counts already included the current call, but its cost was excluded, making
Claude's first turn appear free. The live token approximation also counted JSON
framing and signatures while missing structured reasoning text.

The dashboard now combines completed provider charges and an explicitly marked
current-call estimate. Model rows and the global total use the same calculation.
Native provider usage replaces estimates; cache and reasoning detail counts are
not added again to prompt/completion totals. Cost uses the reported account
charge, rather than upstream inference cost. Estimates cannot predict hidden
reasoning or final cache charges exactly.

Streaming estimates tokenize assembled content, tool names/arguments, and exposed
reasoning once. Cumulative usage chunks merge rather than add, including usage
on error chunks. Interrupted streams retain any reported usage and known
subtotals; missing data is marked incomplete. Reported zero tokens/cost remain
zero. Requests rejected locally before being sent do not consume a turn.

## Saved-run reconciliation

The following snapshot of completed-turn records was read during the audit;
it is not a claim about final totals for the still-running benchmark:

| Model | Recorded turns | Total tokens | Cached input tokens | Provider cost |
|---|---:|---:|---:|---:|
| Astra | 11 | 325,023 | 248,468 | $2.606868 |
| Fable | 12 | 790,464 | 650,610 | $5.0460325 |
| Gemini Flash | 25 | 998,481 | 774,614 | $0.4516413 |
| Grok | 10 | 423,238 | 304,000 | $0.61102 |

Every model's aggregate matched the sum of its raw provider reports, and turn
counts matched the recorded calls. The combined reported cost was $8.7155618,
displayed as $8.72. These token totals include repeated and cached conversation
input, not just newly generated output.

## Interactive check

Ran the real `python -m wavebench --mode harness` prompt editor in an 80 × 28
PTY with isolated temporary settings. Luna and Haiku each wrote a Python file,
made a separate lint call, submitted it, and successfully printed `42`.

Captured first-turn rows showed provisional costs before either call finished:

```text
Luna   building  ~574 tk ~26 tk/s ~$0.0002 1 turn 2.0s
Haiku  building  ~575 tk ~27 tk/s ~$0.0007 1 turn 2.0s
Generating 0/2 complete · 2.0s · ~$0.0009
```

Final rows matched native usage: Luna 1,827 tokens / 3 turns, Haiku 4,345 tokens /
3 turns. Each model's final cost matched its saved provider charges. The header
matched their unrounded sum. All captured model rows stayed within 80 columns.

Validation: `pytest -q` — 710 passed, one live test deselected.
`ruff check .`, `ruff format --check .`, and scoped `git diff --check` passed.

Accounting references: [OpenRouter usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting),
[reasoning tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens),
and [prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching).

## Live speed and stalled-stream follow-up — 2026-09-07

Harness was displaying a whole-call average during generation and carrying the
completed-call average into tool phases. Its token-update throttle also depended
on another network chunk arriving, which could leave the count behind during a
pause. Live TPS now measures output received in the trailing second; counts
update on each text batch. Both use the local tokenizer. A provider accounting
correction changes totals without being counted as a burst of streamed tokens.
Output after an intermediate usage snapshot continues to advance estimated
tokens and cost until the next provider measurement.

Ran the real interactive `--mode harness` prompt editor in an 80 × 28 PTY with
isolated settings and a local HTTP/SSE server. The server supplied five exposed
reasoning tokens per second, paused for five seconds, resumed, and then supplied
write/lint/done tool calls. This deterministic check made no paid API calls.

Captured rows (decoration and optional path removed):

```text
Pause  building  ~10,015 tk  0 tk/s  ~$0.10  1 turn   4.2s
Pause  building  ~10,020 tk ~5 tk/s  ~$0.10  1 turn   9.1s
Pause  passed    30,300 tk  28 tk/s   $0.37  3 turns 10.7s
```

The count stayed at 10,015 and TPS stayed zero throughout the measured pause;
resuming added five tokens and showed 5 TPS. All 136 captured model rows fit on
one line within 80 columns. The generated Python program passed lint and printed
`42`. Saved usage matched the three supplied reports: 30,300 total tokens,
300 output tokens, three turns, and $0.369 cost. Final TPS remained the average
over API time. Raw recordings and generated files were temporary.

Validation: `python -m pytest` — 716 passed, one paid test deselected;
`ruff check .`, `ruff format --check .`, and `git diff --check` passed.
The automated regressions cover five-second stalls without callbacks, resumed
output, intermediate usage, final reconciliation, turn resets, and HTTP batches
that arrive before the former throttle interval expires.

Protocol reference: [OpenRouter streaming and final usage chunks](https://openrouter.ai/docs/api_reference/streaming).
