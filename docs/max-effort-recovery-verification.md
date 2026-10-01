# Max-effort generation recovery — September 30, 2026

The Skyrim generation's first Opus 5.5 request reported 128,000 completion
tokens, all reasoning, and stopped at the output limit without any tool calls.
The provider charged $2.571441. Its corrective request was rejected with HTTP
402 and `limit_source: openrouter_credits`, including after three 30-second
waits. The saved record does not retain the original `Retry-After` header, so
it does not establish whether the wait cap caused that particular rejection.
The original run and generated projects were left intact.

## Changes

- The lead's system prompt asks it to start with one small file write that
  creates a runnable entry point, then build the full requested features and
  quality through later tool calls. Output-limit recovery similarly asks for
  the next concrete file change. The selected effort and output allowance stay
  unchanged; there is no automatic reasoning downgrade.
- Rejected HTTP 402 requests honor the full numeric `Retry-After` delay.
  Other HTTP retry waits remain capped at 30 seconds. Invalid or non-finite
  delays fall back to the existing backoff. The existing phase deadline,
  cancellation, and HTTP retry count still bound credit settlement waits.

This is workflow guidance, not a guarantee that an unbounded-thinking setting
will finish a particular task. Anthropic documents that
[`max` places no constraint on thinking length](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost),
and Opus 5.5
[does not support forced tool choice](https://platform.claude.com/docs/en/models/opus-5-5/whats-new-opus-5-5).
OpenRouter documents
[credit holds and their retry delays](https://openrouter.ai/docs/api_reference/limits).

## Verification

Regression coverage replays an all-reasoning truncated response followed by a
credit rejection with a 120-second retry delay, then writes and submits a real
project through the controller. Every request uses `max` and a 128,000-token
allowance. The HTTP retry resends identical messages and settings. Separate
cases verify the unchanged 30-second cap on 429s, invalid retry headers,
cancellation, phase deadlines, API-slot release, and exclusion of private
provider text and discarded reasoning from saved history.

Ran the real interactive `wavebench --mode harness --open off --no-web-search
--no-subagents` CLI in a PTY with isolated temporary settings, outputs and raw
recordings, and one process slot. A local HTTP/SSE fixture supplied a truncated
reasoning-only response, then an HTTP 402 requiring a 31-second settlement wait.
The retry arrived after 31.004 seconds. The real file tools, lint and sandboxed
Python execution completed successfully and printed `42`. All five HTTP
requests retained `max` and 128,000 output tokens.

A first paid Opus 5.5 checkout-program probe with an earlier version of the
incremental guidance hit its five-minute phase deadline. Three file calls were
partially streamed but none executed because the response was unfinished.
Provider usage was unavailable. This motivated the explicit first-file
instruction and a longer deadline for the subsequent probe; it is not a
successful acceptance result.

The subsequent paid probe used the final guidance with a 20-minute deadline
and eight-request limit. It completed in 400.066 seconds, with four model
requests and six successful tool calls. Its first response made one small
`write_file` call for `main.py`; later responses wrote the full checkout
implementation, linted it and submitted it. Every request retained `max` and
128,000 output tokens, and every completed response reported Claude Platform
on AWS. The provider reported 44,563 completion tokens and $1.2903736 in cost.
Sandboxed execution printed the independently checked receipt:

```json
{"subtotal": "53.48", "discount": "5.35", "tax": "3.85", "total": "51.98"}
```

The first response alone used 38,138 reasoning tokens and took 349.969 seconds;
the next used 248 reasoning tokens, and the final two used none. Keeping `max`
still requires enough phase time for the first response. This compact acceptance
probe validates the real API/file/lint/submission/runtime path at `max`; it does
not establish that the original Skyrim task will complete, or measure the
effect of the prompt change against a comparable control run. Raw recordings,
fixtures and generated projects remained temporary and were removed after
verification.

Validation: `python -m pytest` — 1,246 passed, one paid test deselected.
`ruff check .`, `ruff format --check .` and `git diff --check` passed.
