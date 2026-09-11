# Generation failure investigation — 2026-09-11

Inspected the latest `3d_visualization_fastest` generation's saved results,
conversations and tool logs.

- Gemini 3.8 Flash completed six model turns and eleven successful tool calls
  (ten searches and one directory listing). The seventh request received a
  provider error from Google AI Studio before any model output. Earlier turns
  reported Google. No project files had been written. The estimated cumulative
  budget was 20,904 of 1,000,000 tokens. The old diagnostics discarded the error
  code and native finish reason, so the specific upstream cause is unknown.
- GPT 5.6 Luna wrote `index.html` and passed lint. It then returned a text summary
  with finish reason `stop`, without calling `wb done`. Its initial empty-path
  directory listing failed, but the model corrected that on the next turn.
  The missing submission caused the final failure at 15,982 of 1,000,000 tokens.

The controller now allows one retry per phase for an explicit provider error
received before model output, provided the error has a transient or missing
code and no native failure reason. It also sends one submission reminder per
phase after a complete text reply without `done`. Requests still consume the
existing turn, active-time and cumulative-token budgets. Only a valid model tool
call can submit a project. Failed turns and recovery decisions stay in the
results; unavailable provider usage is not fabricated.

Provider errors now retain safe numeric/recognized symbolic codes and recognized
native finish reasons. The dashboard identifies a provider response failure
instead of using the generic protocol-failure message. Raw provider error bodies
remain excluded from diagnostics. This follows the
[OpenRouter streaming reference](https://openrouter.ai/docs/api_reference/streaming),
checked on 2026-09-11: an SSE error may arrive after HTTP 200 headers but before
the first output token.

## Local interactive verification

Ran the real `python -m wavebench --mode harness --open off --no-web-search`
CLI in a 120 × 32 PTY, entered a prompt interactively, and supplied model
responses through a local HTTP/SSE fixture. Settings, outputs and recordings
were isolated in a temporary directory. Real file tools, lint and sandboxed
execution remained enabled, with one process slot. No paid model calls were made.

| Fixture | Recovery | Requests | Total tokens | Tool calls | Result |
|---|---|---:|---:|---:|---|
| Gemini | One empty provider error, then write/lint/done | 4 | 4,300 | 3 | Passed; printed `42` |
| Luna | Write/lint, text summary, reminder, then done | 4 | 4,310 | 3 | Passed; printed `42` |
| Persistent provider error | One retry, then failure | 2 | 2,000 | 0 | Provider failed during response |

The two successful fixtures each executed once. Dashboard values matched saved
usage and budget totals. The failed fixture never executed a project. The original
paid generation was not rerun or modified.

Regression coverage also checks ignored reminders, last-turn limits, insufficient
remaining tokens, unknown usage, partial tool calls before an error, output in
the error event itself, nontransient errors, and private text in error metadata.

Validation: `WAVEBENCH_REQUIRE_SANDBOX_TESTS=1 python -m pytest` — 927 passed,
one paid test deselected. `ruff check .`, `ruff format --check .` and
`git diff --check` passed.

## Gemini provider continuity — 2026-09-11

A subsequent run failed on turn eight with provider error code `400`, before
output. Seven successful turns reported Google; the failing request reported
Google AI Studio. No recovery was attempted because a `400` was not classified
as transient. A bounded diagnostic replay of the saved history reached Google
without a signature rejection, while restricting it to Google AI Studio
reproduced the provider message `Corrupted thought signature.` The replay used a
256-token output cap and did not dispatch tools.

Gemini sessions now retain their first successful provider and explicitly
restrict later requests to it. Google and Google AI Studio are separate routing
targets. This survives cache resets, compaction, HTTP retries, empty-stream
recovery and repair. It follows OpenRouter's
[provider restriction controls](https://openrouter.ai/docs/guides/routing/provider-selection)
and preserves the complete
[reasoning history](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
Other models and the separate compaction model keep their existing routing.

The real interactive CLI was exercised again in a 120 × 32 PTY with isolated
settings, one process slot and temporary outputs:

- Local HTTP fixtures covered both Google and Google AI Studio through six
  model turns each: build, deliberate execution failure, repair and successful
  resubmission. Both final programs printed `42`. All continuation requests
  supplied the original provider in `provider.only` and `allow_fallbacks: false`.
- A separate SSE fixture verified the visible `Gemini thought signature rejected`
  error and that no project executed after that rejection.
- A live Gemini 3.8 Flash run completed four turns entirely through Google.
  Continuations used `only: ["google-vertex"]` and disabled fallback. The model
  corrected one rejected tool command, then wrote, linted and submitted `main.py`.
  The real sandbox printed `42`. Reported usage was 2,682 tokens and $0.0023595,
  within the 24,000-token test budget.

Regression tests cover unchanged signatures, provider changes or missing
identities, unavailable pinned providers, compaction and repair continuity,
normal routing for other models, HTTP/SSE signature rejection, usage retention
and omission of private provider error text from diagnostics.

Validation: `WAVEBENCH_REQUIRE_SANDBOX_TESTS=1 python -m pytest` — 941 passed,
one paid test deselected. `ruff check .`, `ruff format --check .` and
`git diff --check` passed.
