# Harness stream limits and diagnostics

Harness requests use separate byte limits for the SSE response body, generated
text, incomplete events, and retained response fields. These are resource guards,
not token counts or provider billing estimates. Every benchmark records the
configured values in its Harness limits, and every accepted request records its
effective policy and final counters under `adjustments.stream`.

Configure these positive integers inside the existing `harness` settings object:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `stream_raw_min_bytes` | 16 MiB | Minimum SSE body allowance |
| `stream_raw_bytes_per_token` | 1,024 | SSE bytes allowed per resolved output token |
| `stream_raw_max_bytes` | 128 MiB | Absolute SSE response-body ceiling |
| `stream_output_min_bytes` | 1 MiB | Minimum generated-text allowance |
| `stream_output_bytes_per_token` | 64 | Generated UTF-8 bytes per resolved output token |
| `stream_output_max_bytes` | 32 MiB | Absolute generated-text ceiling |
| `stream_frame_bytes` | 2 MiB | Maximum incomplete SSE event or line |
| `stream_assembly_bytes` | 32 MiB | Conservative allowance for retained parsed fields |
| `stream_seconds` | 300 | Maximum elapsed time after response headers |
| `stream_idle_seconds` | 60 | Maximum wait for the next response-body bytes |

Settings use byte counts, so 1 MiB is `1048576`. Each minimum must be no greater
than its corresponding maximum. For raw and output bytes, the effective allowance
is `min(maximum, max(minimum, resolved_max_tokens * bytes_per_token))`. The resolved
output allowance includes model/context limits and any affordable-token adjustment
from a rejected HTTP request. With 64,000 resolved output tokens, defaults allow
65,536,000 raw bytes and 4,096,000 generated UTF-8 bytes. Raising one limit never
disables the other limits. Existing session/phase deadlines can expire sooner.

Raw bytes include SSE comments, JSON framing, and provider metadata. Generated
bytes count content, reasoning text, tool names, and tool arguments separately.
Repeated reasoning represented in both `reasoning` and `reasoning_details` counts
toward the byte guard in both places because both representations occupy memory;
the displayed local token estimate continues to count the reasoning once.
Opaque signatures and unknown retained fields consume the assembly allowance.
That allowance charges serialized incoming retained fields before merging, so
repeated replacements can conservatively consume more than their final size.
It is not a measurement of Python process memory. Reads are limited to 64 KiB and
events are checked before assembly, keeping temporary allocations bounded too.

Complete SSE comments and ignored fields do not accumulate an incomplete event.
Data fields separated across lines are joined for JSON parsing; UTF-8 characters
split across network reads are decoded after the line is complete. An incomplete
tool call never becomes executable: completion still requires the final marker,
a successful finish reason, unique valid IDs, function names, and complete JSON
object arguments. Streams that fail after response headers are never replayed.

Failure diagnostics include a stable failure code, the specific limit, effective
policy, raw/content/reasoning/tool/assembly byte counters, pending line/event size,
event count, completion state, elapsed time, and short sanitized model/provider
identifiers when available. Codes distinguish raw, output, frame, assembly, idle,
and total limits from malformed data, disconnection, provider errors, truncated
output, and cancellation. Diagnostics contain no response excerpts, prompts,
request headers, or provider error bodies. Returned model metadata must match the
requested ID or its final slug, or an exact ID in the already-loaded public model
catalogue. A matching slug is recorded as the requested full ID. Provider metadata
must exactly match a public name in the checked-in allowlist, sourced from the
[OpenRouter provider catalogue](https://openrouter.ai/api/v1/providers) on
2026-09-11 (106 unique names). Unknown names, prompt-like prose, and
credential-like values are omitted. New provider labels require refreshing this
snapshot; requests do not perform another network lookup. Filtering applies only
to diagnostics: original response model/provider fields remain available for
controller validation and accounting. The fixed shape and capped identifiers
keep normal diagnostics below 4 KiB.
Original provider usage is retained separately when received; missing usage stays
missing. A final diagnostics callback also runs on external cancellation so the
session can retain evidence even when a phase deadline interrupts the request.

Deterministic verification:

```bash
python -m pytest tests/integration/test_harness_protocol.py tests/integration/test_harness_stream_limits.py
```

The local HTTP tests exercise a valid tool response with more than 8 MiB of
framing, output/frame/raw/assembly overflows, malformed JSON and metadata, prompt
echoes in model/provider fields, recognized public identities, split UTF-8,
multiline SSE events, usage preservation, cancellation, and both timeouts.
They use no paid provider calls. Live DeepSeek generation and tool-use evidence
must be recorded separately with the actual observed result.

Protocol behavior was checked against the [OpenRouter streaming reference](https://openrouter.ai/docs/api/reference/streaming)
on 2026-09-11, including heartbeat comments, final usage chunks, and mid-stream
error events.
