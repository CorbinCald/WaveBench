# Harness mode

Harness is the default code benchmark. `--mode code` is an alias for
`--mode harness`; old history, prompt history (`.benchmark_query_history.code`),
and single-file artifacts remain readable and are not converted or executed.
Existing saved Auto-open and Auto-install settings are preserved. A new config
defaults to incremental opening and dependencies off.

## Preview destination

In `wavebench --config`, open **Settings → Preview destination** and press Space
to cycle the choices. Enter saves; Esc cancels. This setting applies to Harness
web previews, independently of Auto-open's off, incremental, and after-all timing.

| Destination | Behavior |
|---|---|
| Automatic (default) | Use the connected laptop for SSH sessions, previously connected remote Herdr sessions, or a headless Linux host. Use the host browser in a local desktop session. A live laptop companion also takes priority. |
| Connected laptop | Publish each ready preview to the laptop companion; never open a browser on the Wavebench host. |
| Wavebench host | Open the browser on the machine running Wavebench, including when explicitly selected over SSH. |

The saved top-level setting is `preview_destination`: `automatic`, `laptop`, or
`host`. Existing settings gain Automatic when this field is absent. Auto-open
off continues to validate and stop the generated app without presenting it.
Non-web program output remains in the terminal, and generated projects remain
in their existing output directories.

Every managed web preview opens with a WaveBench header showing the model,
result number, prompt, short run ID, and execution attempt. The browser tab or
app window title carries the same identity. Result numbers follow the selected
model order, so they stay consistent even when generations finish out of order;
the run ID distinguishes repeated benchmarks. Hover over the model or run ID
for its full value, or over the prompt to read the full text.

The header stays outside the generated app as you navigate or the app changes
its own title, on both the host and connected laptop. Generated source files
are unchanged. **Open app only** opens the original page without the header,
including for apps that disallow embedding. The terminal review URLs and saved
attempts also identify the labeled preview.

Laptop previews require the server's `herdr-review` helper with the `offer`
interface and the existing laptop `herdr-server` companion. Connect using
`herdr-server` or `herdr-server --public`. Wavebench uses `HERDR_SESSION` (default
`work`); run it in the same server session as the laptop connection. The laptop
client and server helper must both be current. Plain SSH without the companion can run
Wavebench but does not establish automatic preview forwarding.

The controller publishes each existing sandbox preview's loopback proxy; it
does not rerun the generated project. The review screen reports **Waiting for
laptop connection**, successful laptop opening, or forwarding/browser errors.
It never falls back to a host browser when a laptop is unavailable. Late
connections and reconnections pick up still-active previews. Missing or outdated
helpers produce an actionable presentation error while preserving the actual
runtime result.

Enter, Ctrl-C, review timeout, or controller exit closes the registrations and
managed apps. Previews stay active while waiting for other generations or a
laptop. The configured review window defaults to 10 minutes after the batch
completes. Closing a preview preserves the generated project. Clean the task up
when done.

## File tools

Install WaveBench with `pip install -e .` to get `wb`, or use
`python -m wavebench.harness`. The developer binds an **existing** project root:

```bash
mkdir -p /tmp/my-project
wb --root /tmp/my-project write src/main.py <<'PY'
from helpers import answer
print(answer())
PY
wb --root /tmp/my-project write src/helpers.py <<'PY'
def answer():
    return 42
PY
wb --root /tmp/my-project ls src
wb --root /tmp/my-project read src/main.py 1:2
wb --root /tmp/my-project edit src/helpers.py <<'JSON'
{"old":"return 42","new":"return 43"}
JSON
wb --root /tmp/my-project parallel "read src/main.py" "read src/helpers.py"
wb --root /tmp/my-project lint
wb --root /tmp/my-project done <<'JSON'
{"runtime":"python","entry":"src/main.py"}
JSON
wb --root /tmp/my-project delete src --recursive
```

`done` validates and submits a launch descriptor. The standalone file CLI prints
the descriptor and does not launch a project; only a benchmark's controller
admits execution. Model calls have one function, `wb`, with a structured
`command` argument. There is no model-supplied root, environment, shell, or run
command. `--json` accepts one command object or an array on stdin:

```json
[
  {"command":"write","path":"a.txt","content":"quotes ' \" and\nnewlines\n"},
  {"command":"read","path":"a.txt"},
  {"command":"read","path":"missing.txt"}
]
```

Every call gets an identified result in submitted order, even on validation
failure. Independent reads and disjoint-file operations overlap (four at a
time); conflicting paths and subtree operations wait in order. Lint waits for
pending writes and blocks subsequent writes. A batch containing `done` and any
other operation is rejected in full. Call IDs are deduplicated for the session;
reusing an ID with different arguments fails. An exact edit must match once or
the file stays unchanged. Output is bounded and explicitly marked when
truncated; full tool records and subprocess logs are saved in `metadata/`.
Providers that fill optional schema properties may send unused fields; the
dispatcher uses only the fields for the selected command. Unknown fields,
including any attempt to change the root, are rejected.

## Supported project runners

| Runtime | Descriptor | Success rule |
|---|---|---|
| `python` | `.py` entry, optional literal `args` | Program exits with code 0 within 60 seconds |
| `node` | `.js`, `.mjs`, or `.cjs` entry, optional literal `args` | Program exits with code 0 within 60 seconds |
| `python-server` / `node-server` | Entry as above; HTTP listener on `PORT` (8000 inside the private namespace); optional `preview` URL path | An HTTP 2xx/3xx response within 20 seconds |
| `static` | `.html`/`.htm` entry; optional `preview` URL path | Trusted static server loads the entry over HTTP within 20 seconds |

For example: `{"command":"done","runtime":"python-server","entry":"server.py","preview":"/health"}`.
Arguments are passed after the entry point as literal strings. Shell strings,
package scripts, arbitrary executables, GUI/interactive desktop runners, npm
dependencies, and development reloaders are unsupported. Use plain HTTP server
entry points without debug/watch/reload behavior. WaveBench sets `CI=1`,
`NODE_ENV=production`, and `FLASK_DEBUG=0`, and never launches a watcher or
restarts a preview. These are startup/runtime checks, not browser interaction
tests or a project quality grade. A static HTML load alone does not validate its
JavaScript interactions; inspect them in the managed preview.

An initial pass completes the model session. Only first-run failure opens one
repair phase in the same conversation. It may include many file/lint calls,
followed by `done` and one final execution. Failed process startup counts as an
admitted attempt. Missing tooling, unsupported launch descriptors, dependency
setup errors, failed lint, generation failure, and budget exhaustion before a
launch do not consume attempts or unlock repair. A cancelled or abandoned
repair keeps the first failure, with no fabricated second attempt.

## Isolation and dependency policy

Ubuntu's AppArmor policy may require enabling the distribution's Bubblewrap
profile. A preflight error such as `bwrap: loopback: Failed RTM_NEWADDR: Operation
not permitted` can indicate this restriction. On Ubuntu 24.04, an administrator
can install and load the packaged profile:

```bash
sudo apt-get install bubblewrap nodejs python3-pip apparmor-profiles
sudo install -m 0644 /usr/share/apparmor/extra-profiles/bwrap-userns-restrict /etc/apparmor.d/bwrap-userns-restrict
sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict
```

Our Ubuntu CI loads this profile before requiring the real sandbox tests.
The profile allows Bubblewrap's namespace setup and removes capabilities from
its children. See [AppArmor's profile](https://gitlab.com/apparmor/apparmor/-/blob/master/profiles/apparmor/profiles/extras/bwrap-userns-restrict)
and [Ubuntu's namespace policy](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/).

The first runner requires Linux, Bubblewrap, system Python 3 and Node, with
working unprivileged namespaces. Auto-install additionally requires system
pip. Preflight fails explicitly; WaveBench never falls back to a working
directory alone. Tool definitions and runtime availability are the same for
every selected model.

All file operations walk from an open root directory descriptor with
`O_NOFOLLOW`. Absolute paths, traversal, symlinks, linked-file reads/writes,
and special-file operations are rejected. Paths are limited to 4,096 UTF-8 bytes
and 128 components; there is no small file-count cap. Writes replace atomically, and
directory creation/deletion use directory descriptors, including under link
replacement races. Prompt-derived directory names are single sanitized
components; exclusive invocation and model directories prevent collisions.

Lint and generated code see only their project, isolated runtime data, a
read-only trusted helper, and the read-only system toolchain (`/usr`, `/bin`,
`/lib`, `/lib64`). A private PID/network namespace, cleared environment, and
closed inherited descriptors keep host credentials, host services, controller
state, history, and sibling outputs out of reach. The only external preview
connection is a controller-owned loopback proxy to the existing sandbox server
over a pinned Unix socket descriptor. Socket path replacement cannot redirect
the controller into a host service. A preview does not relaunch the project. All subprocesses
are stopped on timeout/cancellation, before repair, and at review completion.

`.wb/` is reserved inside the project for disposable, per-operation runtime
state and dependency directories. Model file tools cannot alter it, and the
sandbox hides it at its project mount; the active runtime directory is mounted
at `/state`, and dependencies at `/deps`. Neither contains attempt counters or
controller state. Python imports its entry directory, project root, and `/deps`;
dependency `.pth`/`sitecustomize` hooks are not loaded.

Auto-install is visible and effective even with Auto-open off. When enabled,
`requirements.txt` accepts PyPI names and version constraints. Pip downloads and
installs **wheels only** in a fresh per-model target, using an isolated config
and the PyPI index. No source builds, local paths, URLs, includes, extra indices,
generated setup scripts, or package scripts are allowed. Only this trusted pip
setup has networking; generated code and lint do not. A changed manifest on
repair gets a fresh dependency target. With Auto-install off, a nonempty
requirements manifest fails setup explicitly. No LLM guesses imports or shares
an environment between models.

Lint uses Python compilation without imports, `node --check`, JSON parsing, and
HTML parsing. It ignores package scripts, project plugins, and configuration
hooks. HTML checks are structural parsing, not full HTML/CSS validation.

## Optional web search

Press **w** in the benchmark menu or use `wavebench --setup-web-search` to configure
Brave Search. **Settings → Web search (Harness)** opens the same screen with Space;
Enter or Tab saves the Settings changes. The validated credential is saved
immediately, even if you later cancel the surrounding Settings menu. Search is
disabled by default. Once enabled, every model in a benchmark receives the same
additional native tools:

```json
{"name": "web_search", "arguments": {"query": "Python documentation", "count": 5}}
{"name": "web_fetch", "arguments": {"url": "https://docs.python.org/3/library/asyncio.html"}}
```

`query` accepts up to 600 characters and 75 words. `count` defaults to 5 and can
be 1–10. The tool returns titles, URLs, and snippets from Brave's
[Web Search endpoint](https://api-dashboard.search.brave.com/api-reference/web/search/get).
Use `web_fetch` to open a result URL or a public URL supplied in the prompt. It
returns readable HTML text (including table rows and resolved links), plain text,
Markdown, or JSON. The result includes the requested and final URL, title,
retrieval time in UTC, and available source-reported publication/modification
metadata. Missing dates are null; a retrieval time is not a publication date.
The session prompt includes the current UTC date and tells models to read sources,
verify claims and metric definitions, cite URLs, and identify estimates or missing
evidence. Source content remains untrusted evidence, never instructions. The
date and instructions are fixed at session creation and retained through repair
and context compaction.

Page reads return up to 8,000 characters by default. `max_chars` accepts 1–12,000;
`start` is a zero-based character offset. When `truncated` is true, call again
with `next_start` as `start` and the same URL. The dispatcher may shorten a
section further to fit the configured tool-output limit, preserving its
continuation offset. Four recent pages are cached per model for consistent
sections; `cached` and `fetched_at` identify reused snapshots. An evicted page
is downloaded again. For a response whose `next_start` is 8000, continue with:

```json
{"name": "web_fetch", "arguments": {"url": "https://docs.python.org/3/library/asyncio.html", "start": 8000, "max_chars": 4000}}
```

This is an HTTP source reader: it does not run JavaScript, log in, or extract PDF
or binary documents. HTML results explicitly flag that dynamically loaded
content may be absent. An empty page shell returns an error suggesting a public
text page or JSON data endpoint. HTTP errors, blocked access, unsupported formats,
oversized pages, and timeouts produce readable tool errors; there is no automatic
retry or browser fallback.

Each model gets 20 search attempts across build and repair; set
`harness.web_search_calls` in `.benchmark_config.json` to change that limit for
all models. Calls are paced to at most one request per second within a benchmark,
with a 15-second HTTP timeout, a 1 MB response limit, and bounded snippets. They
also consume the existing build/repair time and tool-output budgets. Invalid
keys, quotas, timeouts, and provider failures return readable tool errors. Requests
are not automatically retried. Replaying the same tool-call ID returns its saved
result without another request.

Each model also gets 20 page-read attempts across build and repair, independently
of searches; configure `harness.web_fetch_calls` to change this. Continuation and
cached reads count as attempts; replaying the same tool-call ID does not. Reads
have a 20-second download deadline including redirects, at most five redirects,
and 2 MB limits on the decompressed response and extracted text. Only public
HTTP(S) destinations are allowed. Every redirect and the DNS answers used for
the connection are checked; loopback, private, link-local, and other nonpublic
addresses are rejected. Reads use no API credentials, ambient proxies, netrc
authentication, or cookies.

The controller owns requests and credentials; generated code keeps its isolated
network and receives no Brave key. `harness.web_search` in each result records
whether search was enabled, its provider, attempts, and failures. Search calls
also appear in ordinary tool counts and diagnostic files. When search is enabled,
the live and final metrics show each model's **WEB SEARCHES** count (**WEB** in
narrower terminals). It starts at zero, updates after each search attempt,
includes failed attempts, and persists across repair and compaction. Replayed
calls do not increase the count. Brave billing is
separate from the reported OpenRouter cost. `harness.web_fetch` records enabled
state, attempts, and failures separately; the **READS** column (**GET** in narrow
terminals) shows page-read attempts. Page reads also contribute to ordinary tool
counts, logs, time budgets, and model input-token costs, but make no Brave API
request. Older results without page-read metadata remain supported.
`--no-web-search` disables both tools
for one run; an enabled configuration with no key stops before model generation
and explains how to complete setup.

## Prompt caching and context compaction

Harness keeps instructions, tool schemas, and earlier messages stable, appends
new content, and sends a per-conversation OpenRouter `session_id` for provider
affinity. Each model/session has its own key, retained through repair and
compaction. Cache markers are added to outgoing copies, leaving saved messages,
reasoning signatures, tool IDs, and tool results intact. HTTP retries reuse the
same prepared payload. This is **prompt caching**; responses are freshly generated.

| Provider/model | Policy |
|---|---|
| OpenAI GPT-5.6 and later | Automatic caching of the growing conversation, stable `prompt_cache_key`, and 30-minute TTL. Up to three explicit anchors on the original prompt and recent non-tool message boundaries leave one write slot for the automatic breakpoint. |
| Earlier OpenAI models | Automatic caching with a stable `prompt_cache_key`; no unsupported explicit controls. |
| Anthropic | Explicit `cache_control` breakpoints on the original prompt and recent request boundaries, up to four. Retaining the previous boundary supports batches beyond the 20-block lookback. Start with 5-minute TTL; promote to 1 hour when request spacing reaches 4 minutes. |
| Google Gemini 2.5 and later | Implicit caching for short prompts. At a locally estimated 4,096-token prefix, add one explicit checkpoint and keep it fixed until its 5-minute TTL expires. Advancing it every turn would repeatedly pay creation/storage costs. |
| Other models | Stable prefixes, schemas, and session affinity support provider-managed automatic caching without sending unsupported vendor controls. |

Cache eligibility, minimum lengths, routing, eviction, and discounts remain
provider-dependent. `usage.prompt_tokens_details` records cache reads/writes;
`usage.cache_read_ratio` is known only when all relevant counts are reported.
The provider's actual cost includes cache-write and storage charges. Unknown
cost stays unknown rather than being estimated using an incorrect uncached rate.
Single-shot text calls use deterministic prompt affinity for repeated prompts;
they do not explicitly provision a cache for an unknown future conversation.

OpenAI harness requests use `prompt_cache_options.mode: "implicit"`, recorded as
`openai_hybrid`. OpenRouter converts tool results to plain function-call output
strings and drops their explicit content markers. Explicit-only mode therefore
cannot cache a conversation that starts short and grows through tool calls.
Automatic breakpoints cover that history without inserting user messages or
rewriting tool results. [Failure analysis and live verification](cache-context-verification.md#tool-result-caching-fix)
include upstream request evidence and actual cache reads.

Before each build/repair request, Harness checks active context and remaining
cumulative budget. It compacts when estimated input **exceeds 240,000 tokens**,
when a smaller model window needs output headroom, or when repeated inputs would
leave too little budget for useful follow-up work. Budget-driven compaction
starts below the normal context threshold and must pay for its own request while
preserving capacity for validation and submission. The compactor is fixed to
**`openai/gpt-5.6-luna`, High effort**, independent
of the benchmark's model and reasoning setting; an effort rejection never
silently downgrades it.

The controller preserves the leading instructions and **first user message**
exactly, plus the **latest complete assistant message**, including tool calls,
reasoning/signature fields, all following tool results, and repair feedback.
Luna summarizes only the intervening history, retaining requirements,
corrections, file state, failures, and outstanding work. It receives no tools and
cannot edit the project. Previous summaries are included in subsequent
compactions. The summary is factual memory; project files remain readable.

The TUI shows `compacting` during the request. Before replacement, the original
conversation is archived as `conversation-before-compaction-NNN.json` in model
metadata. `compaction-NNN.json` records the exact request, complete response,
usage, duration, reason, and before/after sizes. Empty, truncated, oversized,
wrong-model, or ineffective summaries leave the original context intact.
Invalid summaries and mandatory context failures end generation with an error;
optional compaction with insufficient savings is recorded and skipped. If the required preserved messages cannot fit,
Harness fails explicitly instead of truncating them. Cache boundaries reset
after successful replacement; provider affinity remains stable.

Compaction input/output and elapsed time count toward the **same total-token and
active-phase limits** as generation. It consumes no project execution attempt
and does not reset turn limits. Results include its cost in overall usage, with
`harness.model_usage`, `harness.compaction.usage`, and `timing.compaction_s`
separately identifying overhead. Luna's one-use summary input requests no paid
cache writes. The default **1,000,000 total-token budget** includes every repeated
input, cached input, generated output, and compaction request. Compaction never
raises this limit. See [budget-aware compaction](budget-compaction.md) for
affordability, savings checks, and recorded outcomes.

Provider references: [OpenRouter caching and routing](https://openrouter.ai/docs/guides/best-practices/prompt-caching),
[OpenAI cache controls](https://developers.openai.com/api/docs/guides/prompt-caching),
[Anthropic cache behavior](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
[Google caching](https://ai.google.dev/gemini-api/docs/caching), and
[GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna).

## Budgets and records

Defaults are configured in `wavebench/harness/config.py`. Override them under
`"harness"` in `.benchmark_config.json`; all values must be positive integers.
To change the preview review timeout interactively, open `wavebench --config`,
go to **Settings → Preview review timeout (s)**, and press Space. Enter a positive
whole number of seconds (Ctrl-A clears the field), press Enter to apply, then
Enter again to save the menu. Esc cancels an edit. The default is 600 seconds
(10 minutes); the saved value is `harness.review_seconds`.
The same Settings page exposes **Build time limit (s)**, **Repair time limit (s)**,
**Total token budget**, and **Output tokens per turn**. These save to
`harness.build_seconds`, `repair_seconds`, `total_tokens`, and `turn_tokens`.
Time limits count active model requests and tools; scheduler waiting and preview
review are separate. The total token budget is per model across every build and
repair request, including repeated conversation input and generated output.

Before that capacity becomes scarce, the model receives one actionable warning
to finish essential edits, lint, inspect results, and call `done`. The shared
[finishing reserve](finishing-reserve.md) accounts for both requests' inputs and
outputs plus the tool-result round trip. Warning text is counted, output is
capped while finishing, and inaccurate estimates or insufficient reserve have
explicit records. The model must still submit its work itself.

| Limit | Default |
|---|---:|
| Build / repair model turns | 32 / 12 |
| Total tokens, including every input and output | 1,000,000 |
| Output tokens per turn | 64,000 |
| Active build / repair time | 1,800 / 300 seconds |
| Program / startup / lint / dependency setup | 60 / 20 / 30 / 120 seconds |
| Managed preview review | 600 seconds, or Enter/Ctrl-C |
| Tool response / saved subprocess diagnostics | 16,000 characters / 8 MiB per subprocess |
| Stream body / generated text | Output-scaled, capped at 128 MiB / 32 MiB |
| Incomplete stream event / retained parsed fields | 2 MiB / 32 MiB |
| Connection, request send, and response headers | 60 seconds per HTTP attempt |
| Stream duration / idle wait | 1,800 / 60 seconds, within the active-phase deadline |
| Calls per batch / concurrent file calls | 64 / 4 |
| Concurrent API requests / subprocess checks or launches | 12 / 4 |
| File data / project source data | 8 MiB per file / 128 MiB |
| Total source, runtime and dependency storage | 512 MiB, monitored during subprocesses |
| Project execution attempts | **One initial run, plus one retry only after failure** |

The token budget uses provider usage where available. Local counting uses
`tiktoken`'s `o200k_base` as an estimate; it is not an exact tokenizer for every
vendor. The next input estimate reuses the last measured prompt count for the
unchanged prefix and counts only appended content locally, calibrated to that
provider's observed ratio. Ten percent extra is reserved on unmeasured content,
plus 1,024 tokens for context admission. Compaction retains the calibration but
resets the measured prefix. Google's separately billed explicit-cache creation
input is excluded from the context measurement while remaining in total usage
and budget accounting. Provider context/output caps and reasoning adjustments
are recorded per turn. Missing usage and cost are persisted as unknown, never
invented as zero. HTTP retries are bounded separately and never replay completed
tool effects. Truncated or malformed streamed arguments do not execute.
Stream byte limits are separately configurable and recorded. Diagnostics retain
the reached limit, byte counters, parsing state, and sanitized provider/model
identifiers without response excerpts. See [stream policy and defaults](stream-limits.md).
The [limit verification notes](harness-limits-verification.md) include bounded
live DeepSeek/Gemini runs and the full local checks.
Python execution also has a 1 GiB address-space limit; Node uses a 512 MiB V8
heap. A subprocess has a 128 MiB individual-file write limit. The aggregate
storage monitor polls every 100 ms, so a fast writer can briefly overshoot it;
the project is retained when the process is stopped.

History and each model's `metadata/result.json` record generation completion,
runtime attempts/outcomes, workspace/entry, lint/setup logs, configuration,
actual provider/model, all turn usage and costs, API retries, and phase
timestamps. API/tool time, initial generation, repair, scheduling wait, setup,
and runtime durations are separate. Leaderboard `time_s` is active build plus
repair time; an `after_all` wait does not inflate model performance time.
Lifetime analytics label harness records and do not mix them into historical
one-shot model rows. Failed runs' known costs are also included.

The live dashboard and final results table show each model's status, generated
output tokens (`OUT TK`, or `OUT` on narrow terminals), output tokens per second
(`tk/s`), cost, turns, elapsed time, cache hit percentage, tools used, and tool
failure percentage. Each model has a metric row beneath
shared column headers, with phase and metrics aligned across models. A separate
budget line shows cumulative input plus output used, the configured limit, and
remaining tokens; estimates carry `~`. Final failure details distinguish stream
limits, token exhaustion, model/protocol failures, and project runtime failures.
Unaffordable requests include remaining tokens and the next input estimate.
These details also appear in history, with older records supported. Values
use compact k/M/B/T suffixes when needed; `—` means unknown, and estimation
and partial-usage markers remain visible. At 60 columns, abbreviated headings
include `TN` (turns), `HIT%` (cache hits), and `USE` (tools used). Below 60 columns,
the display prioritizes model, phase, tokens, cost, and time. Model names and
phases shorten to fit, and short terminals reserve a count of hidden models.
Output counts accumulate across build, repair, and context-compaction calls,
including failed calls with reported usage.
Input prompts and tool definitions do not appear in this output count. Before
the first output, the count and speed show `—` and an active building row says
`waiting`. Each model starts showing its own output count and speed as output
arrives. Once output has started, an interval without output shows `0` TK/S.
Cost includes input and output, so an estimated input charge can appear while
the model is still waiting. Provider-reported output can include hidden reasoning;
without received output text, live speed remains `—`.
A turn is one model API call, including the current call; HTTP
retries and individual tools within a call do not add turns.
The elapsed timer beside the phase runs continuously for that model.

`cache hit` is total reported cached input tokens divided by total reported
prompt tokens, including compaction. It updates when a provider reports cache
usage during or after a call; between reports it retains the settled percentage.
Missing counts or no reported input show `—`. Cache writes and estimated tokens
do not count as hits.

`tools used` counts completed tool calls across build and repair, including
failed, rejected, and cancelled calls. Each result updates the count immediately,
even while other tools in a batch are still running. An identical call-ID replay
does not count again. `tool fail` is failed results divided by completed calls;
it shows `—` before the first result. Failed lint checks count as tool failures;
API retries and program execution failures do not. Final counts are saved as
`harness.tool_usage.calls` and `harness.tool_usage.failures`.

While a call streams, `~` marks estimated tokens, output speed, and cost. Output
estimates tokenize the assembled text, tool arguments, and exposed reasoning;
SSE framing, opaque signatures, and duplicate reasoning fields do not add tokens.
Live cost combines measured charges from earlier calls with the current call's
estimated input/output charge at its model's catalog rates. Compaction uses the
compactor's rates. Cache discounts, hidden reasoning, and provider-specific
charges can make estimates differ from the final bill.

Provider usage replaces estimates as soon as it arrives. The output column uses
reported completion tokens, including reasoning counted once. Saved usage and
budget totals still include every call's input and output; reasoning and
cache-detail counts are subsets, not extra tokens to add again. Cost uses
`usage.cost`, not the upstream cost or catalog pricing when provider billing
is available. The global total adds the same unrounded values shown per model,
including queued and finished models;
rounding each displayed row separately can produce small display differences.

If a failed or interrupted call omits usage, known subtotals remain visible with
`≥`; `~…+` means an estimated subtotal with some usage still unknown. A completely
unknown cost is never shown as zero. Locally rejected requests that never reach
the API do not add turns. HTTP retries and tool calls do not add extra turns.
Each build or repair phase can recover once from a provider error received before
any model output; partial streams are never replayed. If a model sends a text
reply without `wb done`, the controller gives it one submission reminder per
phase. Only an actual, valid `done` tool call submits the project. These recovery
requests consume the remaining turn, time and token budgets and are recorded in
`harness.recoveries`; missing provider usage remains unknown.

Gemini conversations bind to the provider reported by their first successful
turn: `Google` maps to `google-vertex`, and `Google AI Studio` maps to
`google-ai-studio`. Later requests restrict `provider.only` to that provider and
disable cross-provider fallback, including HTTP retries, stream recovery and
repair. Compaction preserves this binding while its separate summarizer keeps
normal routing. This prevents signed tool history from crossing between the two
Google services. If the bound provider becomes unavailable, the request fails
within the existing retry limits. Missing or unexpected provider identities stop
the response before tools execute. Results record `harness.gemini_provider` and
each completed turn's `adjustments.provider_routing`.

Live OUT TK and TK/S refresh together every 250 ms and hold between refreshes.
TK/S is the number of locally tokenized output tokens received since the previous
refresh divided by the actual elapsed time. Bursts are collected into that same
interval, including across tool waits and new turns. An interval with no output
shows zero once output has started, with no lingering smoothed rate. Internal
accounting still consumes every event immediately. Provider completion-usage
corrections reconcile the displayed output count at the next refresh without
counting as new output throughput.
Finished generations publish their final totals immediately. Final results show average
speed: reported output tokens divided by total API time, excluding tools,
execution, and review waits.
Missing provider usage or cost remains explicitly incomplete. Metrics remain visible during
linting, execution, repair, retries, and after failure or cancellation.

Browser stdout/stderr from opening a preview is saved separately in
`metadata/<model-slot>/browser.log`, including messages written after the
launcher returns. The execution attempt records this path as `browser_log`.
WaveBench keeps the platform's default browser and `BROWSER` preference. A
failed browser launch leaves the successful runtime result intact and shows
the preview URL for manual opening, along with the browser log path.

## Verification

The default suite is offline. Lifecycle tests use scripted conversations and
real sandboxed Python/Node/static subprocesses; protocol tests use a local HTTP
SSE server. CI installs Bubblewrap and requires the sandbox tests. The first
tokenizer use downloads its public vocabulary; prepare it once before offline
testing (CI does this during setup):

```bash
python -c 'import tiktoken; tiktoken.get_encoding("o200k_base")'
```

```bash
python -m pytest -m 'not slow'
ruff check .
ruff format --check .
```

The explicit live matrix uses two model families, real CLI invocations, all
three Auto-open settings, multi-file Python with intentional failure/repair,
and static web projects. It writes full logs, usage totals, timestamps,
attempt counts, and project paths into the chosen directory:

```bash
python scripts/verify_harness_live.py --live \
  --model openai/gpt-5.6-luna --model anthropic/claude-haiku-4.5 \
  --output /tmp/wavebench-live-check
```

This command uses paid OpenRouter requests. Its headless viewer loads the
managed URL without rerunning the project; browser interaction and the TUI
should also be exercised locally. See [verification evidence](harness-verification.md).

Protocol references: [OpenRouter tool calling](https://openrouter.ai/docs/guides/features/tool-calling),
[Anthropic parallel-call semantics](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use),
and [Bubblewrap's security model](https://github.com/containers/bubblewrap).

To verify real cache reads on OpenAI, Anthropic, and Google and compact a
270K-token fixture with Luna High before completing a real Python project:

```bash
python scripts/verify_context_live.py --live --output /tmp/wavebench-context-check
```

This uses paid requests; `--cache-only` skips the long-context case. See
[cache and compaction verification](cache-context-verification.md).
