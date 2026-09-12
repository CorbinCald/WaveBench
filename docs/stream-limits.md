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
| `response_headers_seconds` | 60 | Maximum time from starting an HTTP request to receiving its response headers |
| `stream_seconds` | 1,800 | Maximum elapsed time after response headers |
| `stream_idle_seconds` | 60 | Maximum wait for the next response-body bytes |

Settings use byte counts, so 1 MiB is `1048576`. Each minimum must be no greater
than its corresponding maximum. For raw and output bytes, the effective allowance
is `min(maximum, max(minimum, resolved_max_tokens * bytes_per_token))`. The resolved
output allowance includes model/context limits and any affordable-token adjustment
from a rejected HTTP request. With 64,000 resolved output tokens, defaults allow
65,536,000 raw bytes and 4,096,000 generated UTF-8 bytes. Raising one limit never
disables the other limits. Existing session/phase deadlines can expire sooner.

The default stream and active build limits are both 1,800 seconds (30 minutes).
Each response is still bounded by the remaining active phase time; the repair
phase defaults to 300 seconds. Receiving output resets only the idle wait, not
the stream duration or phase deadline. Saved settings override these defaults.

The separate response-header deadline covers DNS, connection/TLS setup, sending
the request, and waiting for complete response headers. It applies to each HTTP
attempt, including attempts after an explicit rejection. Once headers arrive,
the stream duration and idle guards take over. This allows a healthy stream to
outlive the header deadline while ending a stalled initial request promptly.
The active phase deadline can interrupt either stage sooner.

A header timeout is reported as `request_timeout` / `response_headers_timeout`
with the summary `No response headers received`, its effective limit, stage,
and elapsed seconds. Cancellation during this stage retains `request_cancelled`
diagnostics and remains cancellation rather than a header timeout. No response
content or credentials are recorded. Successful requests record header timing in
`adjustments.response_headers`. Requests without headers are never automatically
replayed: provider acceptance and usage are unknown, so the saved failed turn
retains the input budget estimate and missing provider accounting.

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
object arguments. The transport never replays a failed stream. The build/repair
controller allows one further request per phase when an explicit provider error
arrives before any model output, with a transient or missing error code. Partial
output, native failure reasons, malformed streams, disconnections and limits do
not qualify. Both requests count toward the existing turn, time and token limits;
the failed request and its usage or budget estimate remain in the saved turns.

Failure diagnostics include a stable failure code, the specific limit, effective
policy, raw/content/reasoning/tool/assembly byte counters, pending line/event size,
event count, completion state, elapsed time, and short sanitized model/provider
identifiers when available. Provider errors also retain numeric HTTP error codes,
recognized symbolic codes and recognized native finish reasons (such as
`MALFORMED_FUNCTION_CALL`), with a flag indicating eligibility for the single
empty-response retry. Arbitrary error messages and metadata are discarded.
Recognized corrupted, invalid or missing Gemini thought-signature errors are
classified as `thought_signature_invalid` for both HTTP rejections and SSE
errors. Only the classification is retained from the provider's message or
nested error body. These failures do not negotiate reasoning settings or retry
the same invalid history. Stream diagnostics also record the pinned provider;
`provider_changed` prevents tools from executing if a response violates that
restriction.
Codes distinguish raw, output, frame, assembly, idle,
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
python -m pytest tests/integration/test_harness_protocol.py tests/integration/test_harness_stream_limits.py tests/integration/test_harness_failure_results.py
```

The local HTTP tests exercise a valid tool response with more than 8 MiB of
framing, output/frame/raw/assembly overflows, malformed JSON and metadata, prompt
echoes in model/provider fields, recognized public identities, split UTF-8,
multiline SSE events, usage preservation, cancellation, and stream timeouts.
Header deadline coverage includes withheld headers, stalled TLS handshakes,
HTTP rejection followed by a stalled retry, connection cleanup, cancellation,
an earlier phase deadline, saved failure accounting, and a healthy tool stream
that outlives the header deadline.
They use no paid provider calls. Live DeepSeek generation and tool-use evidence
must be recorded separately with the actual observed result.

The real interactive Harness CLI was also exercised in a 110 × 32 terminal
with local HTTP fixtures, isolated temporary settings and outputs, and one
process slot. With a one-second header deadline, the stalled-header fixture
made one request, executed no tools, and displayed `No response headers received`;
provider usage and cost remained unknown. A second fixture streamed for more
than one second before completing a valid tool call, then wrote, linted, and
successfully executed a Python project in three model requests. Temporary
settings and outputs were removed after verification.

Protocol behavior was checked against the [OpenRouter streaming reference](https://openrouter.ai/docs/api/reference/streaming)
on 2026-09-11, including heartbeat comments, final usage chunks, and mid-stream
error events.
