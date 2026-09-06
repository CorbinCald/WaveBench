# Harness verification — issue #30

Verified locally on 2026-09-06 with Linux aarch64, Bubblewrap, Python 3.13.7 in
the sandbox, Node 22.23.2, and Bubblewrap 0.11.0. The WaveBench/test environment used Python 3.14.

## Live CLI matrix

Each row used the real `python -m wavebench --mode harness` CLI, two tool-capable
families, isolated state, reasoning effort `low`, and a two-second preview review.
Python projects have cooperating main/helper files. Their first execution raised
`RuntimeError: intentional acceptance failure`; repair kept the same conversation,
then printed `14` and exited successfully. Web projects have HTML, CSS and JS.

| Scenario | Auto-open | Luna attempts | Haiku attempts | Combined tokens | Combined cost |
|---|---|---:|---:|---:|---:|
| Python failure → repair | off | 2 | 2 | 26,279 | $0.023552 |
| Python failure → repair | incremental | 2 | 2 | 28,953 | $0.027591 |
| Python failure → repair | after_all | 2 | 2 | 24,486 | $0.022539 |
| Static web | off | 1 | 1 | 18,637 | $0.019647 |
| Static web | incremental | 1 | 1 | 19,515 | $0.022577 |
| Static web | after_all | 1 | 1 | 16,935 | $0.019438 |

All twelve model sessions passed runtime/startup validation. No successful first
run entered repair. Both preview-enabled web cases loaded both managed URLs
through the headless HTTP viewer. The `after_all` timestamps show every initial
submission before the first admitted run; independent repairs do not wait for
another barrier. Full per-turn usage, provider/model, attempts, phase timestamps,
and saved project paths are in [live-matrix.json](evidence/harness-30/live-matrix.json).

Representative output: [Python repair CLI](evidence/harness-30/python-repair-after_all.txt)
and [managed web preview CLI](evidence/harness-30/web-after_all.txt). The rerunnable
matrix is [`scripts/verify_harness_live.py`](../scripts/verify_harness_live.py); it
requires `--live`, stops on failure, and is excluded from default tests.

## Interactive and browser checks

Ran the real interactive WaveBench CLI in a 110 × 36 PTY, selected Harness,
opened Configuration → Settings, changed Auto-open to off, and verified that
Auto-install remained visible. Restored incremental scheduling and generated
the two models’ static counter projects through the prompt editor. The display
showed building, linting, queued, running, and runtime-passed results with usage.
The [terminal transcript](evidence/harness-30/interactive.txt) includes the
settings and final results from that interactive run.

Playwright CLI opened the existing managed URLs, found the counter at 0, clicked
Increment, and observed 1 on both pages. Screenshots:
[Haiku counter](evidence/harness-30/counter-haiku.png),
[Luna counter](evidence/harness-30/counter-luna.png). Neither preview opened a new
project execution: each result retained exactly one attempt. Pressing Enter
ended review; both ports (36809 and 39955) refused connections afterward.
The only browser console errors were missing optional favicon.ico files.

## Dependencies and overhead

Installed and imported `colorama==0.4.6` from two explicit manifests through the
real wheel-only setup. Both separate model workspaces printed `0.4.6`; neither
used a shared writable environment. Auto-install-off and invalid manifest paths
are covered by offline checks.

Measured with `tiktoken` and `cl100k_base`, using compact JSON for the schema:

| Dependency policy | System prompt | Tool schema | Total overhead |
|---|---:|---:|---:|
| off | 76 tokens | 328 tokens | 404 tokens |
| on | 78 tokens | 328 tokens | 406 tokens |

The serialized schema is 1,491 UTF-8 bytes. This is a reproducible tokenizer
measurement, not a claim that every provider uses this tokenizer. Provider
reported input/output/reasoning usage is retained independently in the evidence.

## Offline verification

The offline suite covers real file-CLI round trips, traversal and link races,
overlapping disjoint file I/O, dependency ordering, cancellation, bounded output,
trusted lint without project imports/plugins, sandbox credential/sibling denial,
Unix-socket replacement attacks, storage deadlines, server readiness and cleanup,
streamed UTF-8/arguments/reasoning fields, HTTP retries, catalog races, unsupported
tools, model/repair budgets, duplicate launches, and all scheduling policies.
Existing history/configuration and text/TTS/image smoke tests also pass.

Final local checks:

```text
.venv/bin/python -m pytest -q
570 passed, 1 deselected in 18.28s

.venv/bin/ruff check .
All checks passed!

.venv/bin/ruff format --check .
88 files already formatted
```

The deselected test is the repository's explicit live directory-naming test.
The paid harness matrix above runs separately. Ruff is pinned to CI's 0.15.11.

## Astra budget estimate and Settings follow-up

The Astra runs starting at 15:28 and 16:27 MDT on 2026-09-06 stopped after
598.90 and 588.81 active seconds, respectively. They had used 133,950 and
142,407 tokens of the 256,000-token limit. Both stopped before submission or
execution because the next-request guard reserved the full conversation's
UTF-8 byte count as tokens; neither reached the 900-second build deadline.

Replayed their saved assistant turns and usage offline through the updated
controller, using fresh workspaces and real file/lint tools. Both reached the
next model-request checkpoint with the full 16,384-token output allowance.
The measured prompt prefix plus new-message byte estimate reserved about
41,000 input tokens, versus 129,000–135,000 previously.
[Replay measurements](evidence/harness-30/astra-budget-replay.json) record the
original totals and corrected admission checks. This verifies the budget fix;
it does not claim either original project completed generation or execution.

The real `wavebench --config` menu was exercised in an 80 × 24 PTY. Verified
the renamed **Preview review timeout (s)**, editing all four build/repair time
and token controls, rejecting zero, scrolling to the final Settings row, and
loading saved values through `Limits.from_config` while preserving other
Harness settings. [Settings capture](evidence/harness-30/budget-settings.txt).

Regression checks cover large conversation continuation, output-budget
clamping, real total-token exhaustion, separate build/repair timeouts, and
context admission using the measured estimate. The full offline suite passed:
574 tests, one live test deselected; Ruff check and format also passed.
