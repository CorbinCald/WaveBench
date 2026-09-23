# Lean Harness verification

September 22, 2026. Harness version 2 replaces the cumulative token budget and the
single multiplexed `wb` tool with request/time limits and small plain-text tools.

## What the recent runs showed

94 model results from September 10–22 passed 56 times (60%). Most failures came
from the harness, not from the generated code:

| Cause | Results | Mechanism |
|---|---:|---|
| Token budget | 5 | The 1,000,000-token total counted every resent, mostly cached input. GPT-6 Sol stopped at 961,310 tokens after producing 68,967; its context never exceeded 70,000 tokens. |
| Turn limit after budget compaction | 5 | Compaction to save budget cut a 20,000-token context to 3,000. Gemini 3.8 Flash then re-read files in 80–180 line slices, one call per request, and reached 32 requests with a complete, lint-clean project it never submitted. It never received a final-request notice: notices were withheld when the token estimate could not afford them. |
| Reasoning-only truncation | 3 | At `max` effort, Claude Opus 5.5 spent all 64,000 output tokens reasoning, twice: the only recovery resent the identical request. |
| Unretried stream failures | 6 | One retry per phase, and only before visible output; `malformed_stream` was never retried. |

Tool ergonomics added friction. The `wb` tool had twelve optional fields, and
OpenAI models (GPT-5.6 and GPT-6) fill every optional field with an empty value,
so `ls` arrived with `path: ""` and failed 22 times. Tool results were JSON with
file text escaped inside a string and cut mid-string at 16,000 characters, while
half of all generated files are larger (median 16 KB, 95th percentile 43 KB);
227 of 248 reads used line ranges. Lint missed JavaScript errors entirely: Node 24
exits 0 from `node --check` for a broken `.js` file that uses `import`/`export`,
and inline HTML scripts were never checked.

By volume, stored conversations held reasoning (49%), `write` file bodies (27%),
fetched pages (8%), search results (6%), and read results (5%).

## Changes

- **Limits:** no token budget. Build and repair phases are bounded by requests
  (50/20, up from 32/12) and active time. Usage and cost are still recorded.
- **Notices:** a finishing notice when three requests remain or one more response
  would leave less than the time reserve, a last-request notice, and a status note
  after compaction. All are always delivered.
- **Tools:** `read_file`, `write_file` (with `append`), `edit_file` (with
  `replace_all` and near-miss hints), `list_files` (recursive, with line counts),
  `delete_file`, `lint`, and `submit`. Empty optional values mean unset. Results
  are plain text; reads return up to 100,000 characters of whole lines. `submit`
  may share a response with final fixes and runs last.
- **Lint:** JavaScript is checked through stdin as an ES module or a script,
  including inline HTML scripts and import maps, with `file:line` and caret output.
- **Recovery:** up to three retries per phase for provider/stream failures,
  truncation, invalid arguments, or oversized batches; in-stream 4xx rejections are
  not retried. A reasoning-only truncation lowers the reasoning effort one level.
- **Compaction:** only for the context window; a failed or ineffective summary
  keeps the history and the build continues.
- **Research:** per-tool call limits, closing at half the requests, a third of the
  time, or finishing, instead of separate turn/time/token allowances.
- **Subagents:** no budget reservations; read-only agents are offered only
  read tools; a failed agent's status, files, and report always reach the lead.

The session module shrank from 1,701 to about 1,150 lines, and
`harness/budget.py`, the finishing-reserve estimator, and budget-driven
compaction were removed.

## Verification

- `python -m pytest`: 1,209 passed. `WAVEBENCH_REQUIRE_SANDBOX_TESTS=1` integration
  run: no skips. `ruff check .` and `ruff format --check .` pass.
- New regression tests replay the failures: a 139,900-token run of mostly cached
  input submits, reasoning-only truncation steps `max` to `xhigh` and submits,
  notices arrive in order, research closes on schedule, and lint reports a broken
  module script at its HTML line.
- Offline end-to-end: the real CLI and live TUI in a PTY against a local
  OpenRouter-compatible SSE server replaying three patterns. The Sol pattern
  (37 requests, 1,546,700 cumulative tokens) passed; version 1 would have stopped
  at 1,000,000. The Opus pattern recovered at `xhigh`. The Gemini pattern (one
  call per request) saw `js/game.js:1: SyntaxError: Unexpected token ';'` with its
  caret, fixed it with `edit_file`, and submitted with zero-filled optional fields.
- Live run (3D maze prompt, `max` effort, web search on, 20-minute build limit),
  $0.69 in total. All three passed their runtime check on the first run:

  | Model | Requests | Tool calls / failures | Cumulative tokens | Active time | Cost |
  |---|---:|---:|---:|---:|---:|
  | GPT-6 Luna | 7 | 7 / 0 | 171,899 | 266 s | $0.022 |
  | Gemini 3.8 Flash | 40 | 40 / 0 | 1,673,013 | 627 s | $0.638 |
  | DeepSeek V4.1 Flash | 7 | 9 / 0 | 156,790 | 129 s | $0.035 |

  Gemini again worked one call per request, but version 1 would have stopped it
  at both the 32-request limit and the 1,000,000-token budget. Its research closed
  at half its requests with one note; its context peaked near 67,000 tokens, so no
  compaction ran.
