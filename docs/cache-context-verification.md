# Cache and context verification — September 6, 2026

Paid requests through the production OpenRouter endpoint verified all three
provider policies. Each probe used the real streamed tool protocol, the `wb ls`
dispatcher, and three consecutive requests with a shared reference prefix.

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
