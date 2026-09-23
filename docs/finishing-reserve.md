# Finishing reserve

WaveBench sends one remaining-budget warning when another ordinary request could
use the capacity needed to finish. This projection includes that request's output
and tool results in both subsequent inputs. The model is told to batch essential file
edits with `wb lint`, inspect the tool results, then call `wb done` alone with
the runtime and entry file. It also warns with two model requests left in a phase,
or when the phase's remaining active time falls to the time reserve described
below. The controller never submits on the model's behalf.

The policy is the same for every benchmark model. Let `I` be the calibrated input
bound and `O` the smaller of the model's output limit and the configured turn limit
(64,000 by default). The finishing estimate is:

```text
2 × (I + 512 warning tokens) + 2 × O
  + input-bound(O + tool-result allowance)
```

Two requests pay for their input and output. The second input also includes the
first assistant response and uses the usual margin for newly added input. The
tool-result allowance is the smaller of 4,096 and
the configured diagnostic character limit, plus 1,024 tokens for result wrappers.
It is a practical estimate for final edits and validation, not a worst-case bound
for arbitrary batches or large file reads. The warning's actual added input is
measured before admission. Finishing retains the normal output allowance, bounded
by the remaining total tokens. Reasoning and tool arguments share this allowance;
the former hidden 4,096-token cap could cut off thinking or a file write while
hundreds of thousands of task tokens remained. The model's reasoning setting stays
unchanged. See OpenRouter's [reasoning and output limits](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
Tool evidence retains the existing full local artifacts and diagnostic limits.

## Time reserve

Response time is only observable, so the time reserve uses the slowest of the
model's last three requests `R`:

```text
max(20% of the phase's active time, 2 × R + lint allowance)
```

Before any request has finished, only the 20 percent share applies. The warning
states the remaining active seconds and the slowest recent response. If
`2 × R + lint` no longer fits, it says the finishing sequence no longer fits.
A response still streaming at the phase deadline is discarded, so a model with
several-minute responses is warned while one more complete round trip remains.
The warning's record lists its `triggers` (`tokens`, `turns`, `time`), with
`seconds_left`, `time_reserve_seconds` and `time_affordable` when timed.

## Reminders

The warning is sent once, but compaction can summarize it away, and a model can
ignore it. Short reminders therefore restate the phase's remaining requests,
active seconds and total tokens:

- after every successful compaction (`[WaveBench budget status]`, or
  `[WaveBench budget reminder]` once finishing has begun);
- once per phase when time runs low after an earlier token or request warning;
- before a phase's final model request, telling the model to call `done` alone
  now if the project can run.

No reminder is added when a warning is sent for the same request, or when the
reminder itself would not fit the remaining tokens. Each is recorded as a
`budget_notice` with its reason and delivery outcome.

## Compaction

Compaction gets the first opportunity at the shared budget boundary. A compaction
request must leave the same finishing reserve for its projected replacement
context. Budget-triggered compaction remains available after the warning when
it pays for itself and preserves that reserve; otherwise repeated large inputs
can exhaust the task while the model is still fixing files. Mandatory context-window
compaction also checks the reserve. Failed
estimates, unaffordable compaction, and reduced capacity have explicit records.

The warning persists through repair without being repeated. Repair has its
existing turn/time limit and shares the original total token budget; successful
submission does not promise enough capacity for a later repair. If the finishing
sequence already appears unaffordable, the warning says so. If even the warning
cannot fit, the controller records that fact. It never adds budget or treats an
unsubmitted project as successful.

`result.json` stores decisions under `harness.finishing_budget.records`: warning
injection and response status, reserve affordability, bounded requests, estimate
overruns, cancellation/failure, repair, blocked requests, and agent submission.
`warning_injected` means the message was added; the warning's `received_response`
status confirms a subsequent response. Cancellation may leave it `interrupted`.
Compaction records separately report whether the finishing reserve survived its
actual charge. Provider-reported input/output totals remain authoritative,
including cached input. When usage is absent, budget accounting charges estimated
input plus observed partial output; it leaves provider usage unknown. OpenRouter
describes its native-token usage reports in its
[usage accounting documentation](https://openrouter.ai/docs/cookbook/administration/usage-accounting).

## Verification

`tests/integration/test_finishing_budget.py` exercises the actual conversation
controller and `wb` dispatcher. Its finishing model writes a Python file, invokes
syntax validation through `lint`, receives that result, and submits through a
standalone `done` call after receiving the warning. Further cases cover small
budgets, inaccurate usage estimates, repeated-warning prevention, cancellation
with and without reported usage, full tool evidence, and repair after a simulated
runtime failure. Sandbox startup and process execution are replaced in these
controller tests; production sandbox tests remain separate.

A paid model run is separate evidence and requires a configured OpenRouter key.
No live-model outcome is claimed by these deterministic tests.
