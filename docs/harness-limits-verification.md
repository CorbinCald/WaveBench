# Stream and cumulative-budget verification — 2026-09-11

Issues [#31](https://github.com/CorbinCald/WaveBench/issues/31),
[#32](https://github.com/CorbinCald/WaveBench/issues/32),
[#33](https://github.com/CorbinCald/WaveBench/issues/33), and
[#34](https://github.com/CorbinCald/WaveBench/issues/34) were exercised with
deterministic HTTP/controller tests, real isolated execution, terminal checks,
and bounded paid requests. The [sanitized live records](evidence/harness-limits/live-probes.json)
contain limits, usage, counters, decisions, and outcomes, without transcripts,
credentials, generated source, or response excerpts.

## Paid model probes

Each probe used one API slot and one process slot, a 240-second build deadline,
14 build turns at most, no dependencies, and automatic cleanup of managed
processes. The opt-in script also has an 850-second overall deadline. All model
and compactor charges count against that probe's recorded fixed token limit.

| Probe | Fixed budget | Reported input + output | Outcome | Reported cost |
| --- | ---: | ---: | --- | ---: |
| DeepSeek V4.1 Flash website | 320,000 | 4,903 + 1,129 | Files, lint, `done`, and static runtime passed | $0.000886002 |
| Gemini 3.8 Flash compaction, initial instructions | 160,000 | 77,750 + 793 | Compacted; then ended with a progress note without submitting | $0.037330200 |
| Gemini compaction, explicit continuation instructions | 160,000 | 108,560 + 1,019 | Compacted, received warning, linted, submitted, runtime passed | $0.063548475 |
| Gemini finishing with a deliberately tight budget | 24,000 | 16,221 + 146 | Warning received and lint completed; next input did not fit | $0.010043625 |
| Gemini finishing with room for the requested four calls | 40,000 | 24,274 + 174 | Warning received, files written, linted, submitted, runtime passed | $0.013528875 |

Total provider-reported cost was **$0.125337177**. These are separate runs;
no running session's budget was increased. The first failed outcomes are retained
alongside successful probes.

DeepSeek used the actual streamed `wb` interface to build a safe three-file
counter website plus README. Its requests allowed up to 64,000 output tokens;
the new policy admitted them and recorded separate wire/content/tool counters.
The model did not produce more than 8 MiB in this small live task. A deterministic
HTTP regression separately proves that a valid tool response with more than
8 MiB of SSE overhead completes.

Gemini's first probe compacted from **29,552 to 8,172** estimated context tokens,
charging **23,354** provider tokens while retaining finishing capacity. Its next
response contained only a progress note, so the controller correctly left the
project unsubmitted. A fresh probe clarified that tool-result turns advance
automatically and that the model should continue calling tools until `done`.
That run compacted from **29,229 to 7,933** context tokens, charged **23,506**
tokens for compaction, and preserved an estimated **36,808-token** finishing
reserve. It later received the warning with **81,872 tokens remaining**, linted,
submitted through the real interface, and ran the Python project successfully.
Optional compaction yielded once finishing was active.

The initial 24,000-token finishing probe required an initial reference read plus
separate write, lint, and submission calls. The model received the warning but
still consumed those separate round trips. Tool-result and input estimates were
exceeded and recorded; after lint it had **7,633 tokens remaining** against an
**8,327-token estimated next input**. It stopped with a structured token-budget
failure, without executing or automatically submitting the project. A separate
40,000-token run afforded all four requested calls and completed. The reserve is
a forecast for a bounded finishing sequence, not a guarantee against additional
reads, extra turns, or inaccurate provider estimates.

Reproduce the current three bounded positive probes with a configured API key
and the documented Linux sandbox setup:

```bash
PYTHONPATH=. python scripts/verify_harness_limits_live.py \
  --live --output /tmp/wavebench-limits-live
```

Use `--case stream`, `--case compaction`, or `--case reserve` to run one probe.
The script records unexpected outcomes and stops at the first failed probe.

## Deterministic and interactive checks

The real-tokenizer regression also reproduces approximately 80,000 tokens of
active context while cumulative usage approaches a fixed 1,000,000-token limit.
Additional tests cover unaffordable or ineffective compaction, exact tool pairing,
cached-input charging, underestimated requests, warning growth, repair,
cancellation, bounded stream parsing, and failure propagation.

The server initially lacked the distribution's Bubblewrap AppArmor profile.
After installing and loading the profile specified in `docs/harness.md`, all
12 real sandbox runtime tests and all 40 existing session lifecycle tests passed.
No unconfined execution fallback was introduced.

See [terminal verification](budget-metrics-verification.md) for the actual prompt
editor, dashboard, final results, and history at 60, 80, and 120 columns, including
both representative stream and token-limit failures.

Final local checks: **904 passed, one paid test deselected** with `python -m
pytest`; `ruff check .`, `ruff format --check .`, and `git diff --check` passed.
The live helper also passed explicit Ruff checks despite the repository's default
exclusion of ad-hoc scripts. Changed files were checked against the configured
secret values before staging; none were present.
