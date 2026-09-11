# Budget-aware context compaction

WaveBench checks cumulative token capacity before each model request. A context
can fit the model's window and still be too expensive to send repeatedly. Issue
[#32](https://github.com/CorbinCald/WaveBench/issues/32) reported this at roughly
80,000 active tokens: only 65,312 of a 1,000,000-token budget remained, so the next
request could not fit.

The controller now considers compaction once at least 16,384 active tokens remain
and the available budget approaches the cost of one ordinary request plus the
two-request finishing sequence that would follow it. The projection includes the
ordinary response and expected tool results in both future inputs, with the same
input estimation margin used for request admission. A full permitted response can
otherwise make the next warning arrive too late. Compaction and finishing use
this shared planning boundary.

Existing triggers at more than 240,000 active tokens
or insufficient model-window headroom still apply. These rules use the same
accounting across models; the controller does not change the configured limits.

Before calling the compactor, WaveBench estimates the actual summary request and
the retained context. The summary cap scales with removable history, from 1,024
to 8,000 tokens. The replacement estimate preserves provider-tokenizer calibration
and reserves that full summary cap. The compactor's output allowance is reduced
when needed to leave the shared reserve for two finishing requests: final fixes
and validation, then reading results and calling `done`. A third follow-up is
included in the savings forecast when affordable.

Budget-driven compaction is skipped if its estimated input savings cannot repay
its request cost within those two or three follow-ups. It is also skipped when
the summary request and finishing reserve cannot both fit, or when the model has
already received the finishing warning. An unsuccessful budget-driven attempt
is reconsidered only after context grows by at least 8,192 tokens or 25 percent,
whichever is larger. Mandatory context-window failures remain explicit errors.

The original task and the latest complete assistant/tool interaction stay
verbatim, including parallel results and provider signatures. Missing, duplicate,
or unmatched results in the latest interaction prevent compaction. Earlier
history becomes a factual handoff; working files remain available through the
normal tools. Invalid, interrupted, or ineffective summaries leave the original
conversation intact.

Compactor input and output count toward the same cumulative budget as benchmark
requests. Cached prompt tokens are included: a price discount is not extra token
capacity. Provider usage remains authoritative; if usage is missing, the local
input bound and observed output provide a provisional charge. This includes
partial output received before cancellation. See OpenRouter's
[usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)
and [prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching)
documentation for provider usage fields.

Compaction records include the trigger, before/after context size, summary and
output caps, estimated savings, reserved follow-up capacity, actual usage and
charge, and any skip or failure reason. If actual provider usage exceeds the
planning estimate, `reserve_outcome` reports `underestimated`; the total limit
still applies. Required compactions rejected before a paid request are recorded
in harness events. Full transcripts and summary requests/responses stay in the
run's diagnostic directory.

## Verification

`tests/unit/test_budget_compaction.py` uses the real tokenizer and conversation
controller with deterministic model responses. Its 78,000-token history and
715,000-token prior usage reproduce the reported cumulative-budget pattern;
compaction occurs while affordable and real `wb write` and `wb done` calls then
complete. A smaller 23,000-token history exercises the same policy under a
120,000-token cap. Both cases run with Gemini and another model identifier.

Additional coverage checks the affordability frontier, reduced compactor output,
small contexts, insufficient savings, repeated skips, active finishing reserves,
exact tool pairing, summary size, cached-input charges, interrupted output, and
provider overruns. These are offline controller tests, not evidence of a paid
model run or sandboxed program execution. Paid verification and environment
limitations are recorded separately with the run evidence.
