# Cache and context verification — September 6, 2026

Paid requests through the production OpenRouter endpoint verified all three
provider policies. Each probe used the real streamed tool protocol, the `wb ls`
dispatcher, and three consecutive requests with a shared reference prefix.
These initial probes used long user messages and appended a user continuation
after each tool result. They verified caching of that shared user prefix, but
missed the tool-result failure documented below.

| Model | Second request cached / prompt tokens | Third request cached / prompt tokens |
|---|---:|---:|
| GPT-5.6 Luna | 12,407 / 12,567 (98.7%) | 12,564 / 12,687 (99.0%) |
| Claude Haiku 4.5 | 13,837 / 14,016 (98.7%) | 14,013 / 14,137 (99.1%) |
| Gemini 2.5 Flash | 14,609 / 14,645 (99.8%) | 14,609 / 14,682 (99.5%) |

OpenAI and Anthropic extended their cached prefix. Gemini reused its original
checkpoint and reported **zero additional cache writes** on requests two and
three. Its first request reported 29,217 input tokens, including a 14,609-token
cache creation charge; the estimator excludes that extra billed copy from
context size while retaining it in total usage. Provider cache hits remain
dependent on eligibility, routing and cache lifetime; these are observed results,
not guaranteed hit rates. [Full cache reports](evidence/cache-context/cache-probes.json)
include actual costs, providers, TTLs, and breakpoint positions.

`scripts/verify_context_live.py` seeded **270,288 locally counted tokens** of
synthetic inspection history. The real Luna High request reported 270,095 input
tokens and compacted the active conversation to **840 estimated tokens** in
5.13 seconds, costing **$0.0543886**. The original user message and latest full
assistant/tool-result tail matched exactly after compaction. Luna then wrote,
linted, and submitted a Python project; it passed its single sandboxed run.

The real interactive WaveBench prompt was also exercised in a 30×100 PTY using
the same seeded history and real model requests. The display showed
`compacting`, resumed generation, and finished with `runtime passed (1 run(s))`.
Results and lifetime analytics included the compactor's cost and tokens:
**273,117 total tokens**, approximately **$0.055**, and **12.2 seconds** active
time. Archived and active messages were compared after the run to verify both
protected boundaries. The fixture injection and raised test budget were confined
to `/tmp`; normal user settings and benchmark history were unchanged.
[Compaction and interactive records](evidence/cache-context/compaction.json)
capture model, effort, usage, timing, phases, and preservation checks.

Automated coverage includes the exact 240,000/240,001 threshold, smaller context
windows, stable cache payloads across HTTP retries, long parallel tool batches,
Google cache expiry, Anthropic TTL promotion, strict High reasoning, preserved
signatures/tool results, cancellation, empty/truncated/wrong-model summaries,
token/time limits, and the two-run build/repair invariant after compaction.
Local validation passed: **615 tests**, with the paid test deselected, plus
`ruff check .` and `ruff format --check .`.

Reproduce paid checks with:

```bash
python scripts/verify_context_live.py --live --output /tmp/wavebench-context-check
```

## Tool-result caching fix

The GPT-6 Astra run `001-gpt6Astra-11151a34568a` made six valid model requests,
but reported zero cache reads and writes even after its input exceeded 8,000
tokens. Its initial user prompt was short; almost all later context came from
the generated HTML in a tool call and subsequent tool results.

Controlled requests reproduced the failure. OpenRouter's streaming
`debug: {"echo_upstream_body": true}` showed the precise conversion:

1. WaveBench sent explicit cache markers on the original user message and the
   latest tool result, with `prompt_cache_options.mode: "explicit"`.
2. OpenRouter converted Chat Completions messages into OpenAI Responses input.
   The user marker survived. Tool results became `function_call_output` items
   whose `output` was a plain string, losing their content-block cache markers.
3. Only the short user prefix remained eligible, while explicit-only mode
   disabled automatic breakpoints. Repeating the request produced no cache
   writes or reads. Anthropic-style markers on the same tool results were also
   dropped; changing marker syntax did not fix it.

The harness now uses OpenAI's implicit mode with a stable key and 30-minute
TTL. Up to three explicit anchors on non-tool messages preserve useful user
boundaries while leaving one of the four write slots for automatic caching.
Tool messages, call IDs, assistant reasoning, and conversation history remain
unchanged. The separately configured single-use compactor still requests
explicit-only mode without markers to avoid unused cache writes.

This follows [OpenAI's documented implicit and explicit controls](https://developers.openai.com/api/docs/guides/prompt-caching).
The loss of tool markers is an observed OpenRouter conversion behavior, not an
OpenAI minimum-context restriction. [Evidence](evidence/cache-context/tool-cache-regression.json)
includes before/after usage, generation IDs, a sanitized upstream conversion
summary, and complete per-turn metrics from new harness runs.

Replaying the affected run's saved contexts with the fixed policy produced:

| Original turn | Input tokens | Cache read tokens | Cache write tokens |
|---|---:|---:|---:|
| 3 | 8,509 | 0 | 8,434 |
| 4 | 8,883 | 8,434 | 374 |
| 5 | 9,029 | 8,808 | 146 |
| 6 | 9,229 | 8,954 | 200 |

These were diagnostic requests using the recorded history, with output capped
at 64 tokens and tool execution disabled. The input cost for these four contexts
fell from the original **$0.356500** to **$0.143621**, including cache-write
charges: **59.7% less**. This compares input costs only; generated outputs differ.

The new `scripts/verify_tool_cache_live.py` separately exercises the real
controller, streamed model tool calls, file dispatcher, lint, and sandboxed
execution. A **458-token initial prompt** asks the model to read `reference.txt`,
write a program using the exact output specified inside that file, lint it,
and submit it. No user messages are inserted after tool results. Both models
completed those four turns and printed `tool cache verified 7391` in one
successful runtime attempt:

| Model | First long request: cache writes | Third turn: cached / input | Fourth turn: cached / input |
|---|---:|---:|---:|
| GPT-6 Astra | 5,522 | 5,522 / 5,645 (97.8%) | 5,642 / 5,850 (96.4%) |
| GPT-5.6 Luna | 5,541 | 5,541 / 5,685 (97.5%) | 5,682 / 5,899 (96.3%) |

The probe fails unless the initial input stays below 1,024 tokens, the tool
history creates a cache entry, and every subsequent request reads over 80% of
its input from cache. It also checks successful tools, exactly one user message,
the returned model, and actual program output. These cache assertions remain
separate from runtime success so a functional project cannot hide a cache failure.

Reproduce the paid regression with:

```bash
PYTHONPATH=. .venv/bin/python scripts/verify_tool_cache_live.py --live --output /tmp/wavebench-tool-cache-check
```

Offline regressions cover short prompts followed by long parallel tool batches,
unchanged tool/reasoning messages, three explicit anchors plus the implicit
write slot, stable keys after compaction, and complete HTTP retry/continuation
payloads for GPT-6 Astra. Provider cache hits still depend on routing and cache
lifetime; the percentages above are observed results, not guarantees.

After this fix, local validation passed **646 tests** with the existing paid
pytest case deselected, including required sandbox tests, plus `ruff check .`
and `ruff format --check .`. The new live probe passed separately for both
OpenAI models above.
