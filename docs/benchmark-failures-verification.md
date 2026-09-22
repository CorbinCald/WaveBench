# Benchmark failure investigation — September 21, 2026

Inspected run `e73186803a254fb3be3f1c53a6faec70`, including saved results,
conversations, compaction decisions and tool records. Luna passed; four models
failed before submitting. The original results and generated projects were left
intact.

| Model | Evidence | Change |
|---|---|---|
| MiMo Pro Ultraspeed | The finishing warning reduced output from 64,000 to 4,096 tokens. The next response used 4,095 tokens for reasoning and ended at the output limit, with 277,538 task tokens still left. | Keep the configured/model output allowance during finishing, including reasoning. |
| MiMo Pro | The same cap cut off JSON arguments at 4,096 completion tokens, with 264,548 task tokens left. The provider still reported `tool_calls`. | Keep normal output capacity; recognize incomplete arguments at the reported output cap as truncation. |
| Grok | Compaction was rejected: estimated cost 254,657 tokens exceeded projected savings of 238,308. Duplicate reasoning and opaque signatures inflated the summary input. After the warning, further compaction was disabled; repeated inputs exhausted the budget. | Send readable, deduplicated evidence to the compactor and allow useful, affordable compaction after warnings. |
| MiMo Flash | The stream assembled 256 tool calls before hitting the fixed index guard. No final usage was available. | Count distinct calls against the advertised batch allowance, allow sparse indices, and give one corrective request after an oversized batch. |

Truncated output, invalid tool arguments and oversized batches now allow one
corrective request per build/repair phase or subagent. No calls from a rejected
response execute or enter conversation history. The model is asked for smaller,
complete operations. Repeated failures stop; retries share the original token,
turn and time limits, with missing usage retained as unknown.

The default task budget remains 1,000,000 tokens and the output allowance remains
64,000. Finishing estimates reserve that normal output allowance for both
validation and submission. Reasoning settings are unchanged. OpenRouter's
[reasoning documentation](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
confirms that reasoning and visible output generally share `max_tokens`.

## Saved-history replay

Rebuilt the separate summary requests from the original histories using the real
tokenizer, without making model calls. Estimated summary input changed as follows:

| Model | Before | After |
|---|---:|---:|
| Grok | 238,273 | 74,173 |
| MiMo Pro Ultraspeed | 199,463 | 132,330 |
| MiMo Pro | 232,850 | 145,966 |
| MiMo Flash, first compaction | 198,647 | 124,265 |

These are local admission estimates, not provider usage measurements. Original
archives and the benchmark model's preserved messages keep all signatures and
reasoning fields exactly; only the separate compactor's data view changes.

## Interactive verification

Ran the real `python -m wavebench --mode harness --open off --no-web-search
--no-subagents` entry point in a 120 × 32 PTY and typed a prompt. A local HTTP/SSE
fixture replaced the provider. Settings, outputs and raw recordings were isolated
in a temporary directory; the task budget was 300,000 tokens, with one process
slot and the normal 64,000-token output allowance. File tools, lint and sandboxed
execution were real.

| Fixture | Requests | Corrective retries | Executions | Outcome |
|---|---:|---:|---:|---|
| Finish after warning, including a file larger than 4,096 tokens | 3 | 0 | 1 | Passed; printed `42` |
| Oversized batch, then complete write/lint/done | 4 | 1 | 1 | Passed; printed `42` |
| Persistent oversized batches | 2 | 1 | 0 | Failed with “Too many tool calls in one response” |

Every request kept High reasoning and 64,000 output tokens. Rejected writes never
appeared in the workspace. Saved budgets remained within the configured limit;
missing usage from interrupted batches remained estimated in the dashboard.

Regression tests cover default output capacity after warnings, provider caps,
compaction of a Grok-sized signed history before and after a warning, exact
preservation of provider state, sparse indices, bounded recovery, persistent
errors, unknown usage, subagent recovery and total-budget enforcement. The
original paid benchmark was not rerun; deterministic fixtures do not establish
that those models will complete the original game successfully.

Validation: `WAVEBENCH_REQUIRE_SANDBOX_TESTS=1 python -m pytest` — 1,221 passed,
one paid test deselected. `ruff check .`, `ruff format --check .` and
`git diff --check` passed.
