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
