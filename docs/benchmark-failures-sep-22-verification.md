# Benchmark failure investigation — September 22, 2026

Inspected run `2708b2e9131c472c9b6509ede83a21ee` ("Recreate as accurately as
possible, the first level of Super Mario 64, but as a zombie shooting game"),
including saved results, conversations, compaction requests/responses and tool
records. GPT 6 Astra and Claude Opus 5.5 passed; three models failed. The
original results and generated projects were left intact.

| Model | Evidence | Validity | Change |
|---|---|---|---|
| Grok 4.7 | After five successful requests, xAI sent an HTTP 502 SSE error 12.7 s into turn 6, after 1,168 bytes of reasoning and no content or tool calls. The provider retry only covered errors before any output, so the run ended with no files. | Provider outage, not model capability. | One retry per phase also covers a transient error after reasoning only. |
| Gemini 3.8 Flash | All eight files were written and lint passed by request 15. The remaining 19 requests were single-chunk file reads. A warning with 14 requests left was ignored; the next compaction summarized it away and it was never repeated. The first summary's outstanding list asked for `wb ls`/`wb read` verification. The second began with leaked GPT tool-call text (`to=wb (json…) {"command":"lint"}`, four times). | Mostly model behavior, amplified by the harness. | Clean leaked tool-call paragraphs from summaries; restate limits after every compaction; remind before the final request; ask the compactor not to prescribe re-reads. |
| MiMo V2.6 Pro | Three requests took 303, 360 and 704 s. The fourth, streaming 14 file writes, was discarded at the 1,800 s build limit; `index.html` imports a missing `js/main.js`. Its first request exhausted the 300 s research window, so both searches were refused. The prompt did not state the time limit and no warning considered time. | Valid timeout. | State per-phase request/time limits in the system prompt; warn when active time falls to a time reserve based on observed response durations. |

The research window counting a model's own thinking time was left unchanged.

## Changes

- `retryable_after_reasoning` marks transient provider errors (408, 429, 5xx,
  `server_error`, `rate_limit_exceeded`, or no code) when only reasoning was
  streamed. It shares the existing once-per-phase provider retry and is recorded
  as `reasoning_provider_retry`. Visible text, tool calls or arguments, completion
  tokens above reported reasoning tokens, and native failure reasons still end
  the attempt. The unchanged conversation is resent; reasoning is discarded.
- Compaction removes paragraphs with leaked tool-call syntax or chat-template
  tokens, except fenced code, and records `leaked_tool_call_blocks_removed`. The
  saved compaction response keeps the original text.
- A budget notice follows every successful compaction. After the finishing
  warning it repeats the instruction to lint and call `done`. A reminder also
  precedes a phase's final model request, and one follows when time runs low
  after an earlier token or request warning.
- The finishing warning also triggers at
  `max(20% of phase time, 2 × slowest of the last three responses + lint allowance)`
  and states the remaining seconds and slowest recent response.
- The system prompt states build and repair request/time limits and the shared
  token budget.

## Interactive verification

Ran the real `wavebench --mode harness --open off --no-web-search
--no-subagents` entry point in a 120 × 32 PTY and typed the prompt. A local
OpenRouter-compatible HTTP/SSE server replaced the provider and compactor. It
replayed the three failure patterns. Settings, outputs and raw recordings were
isolated in a temporary directory, with 8 build requests, a 20 s build phase and
a 1 s lint allowance. File tools, lint, sandboxed execution and the live
dashboard were real. The same fixture then ran against an export of the previous
commit.

| Replay | Before | After |
|---|---|---|
| Reasoning, then a 502 SSE error | Failed: “Provider failed during response” | Passed; `reasoning_provider_retry`, identical retry request, printed `42` |
| Reads files until told this is its final request; compactor leaks tool calls | Failed: turn limit; leaked summaries reached the model | Passed; 3 compactions removed 2 leaked blocks each, 2 status notices, turn warning, final-request reminder, printed `42` |
| 4 s responses in a 20 s phase; submits after a warning | Failed: time limit at 20.0 s | Passed; time warning with 8.0 s left (9.0 s reserve), printed `42` |

Regression tests replay each failure through the real controller and `wb`
dispatcher. They cover the Grok stream, bounded and non-transient cases, the
Gemini compaction handoff with its leaked text, MiMo's 303/360/704 s timeline
with a fake clock, the final-request reminder, and summary cleaning edge cases.
The original paid benchmark was not rerun; deterministic fixtures do not show
that these models will now complete the original game.

Validation: `WAVEBENCH_REQUIRE_SANDBOX_TESTS=1 python -m pytest` — 1,246 passed, one
paid test deselected. `ruff check .`, `ruff format --check .` and `git diff --check` passed.
