# Optional Brave web search verification

Verified locally on 2026-09-09 using the real interactive WaveBench CLI in
80 × 28 and 60 × 28 PTYs. Temporary configuration, credentials, projects, and
HTTP/SSE fixtures were isolated from the maintainer's settings. No paid API
calls were made; live Brave account access remains unverified.

- Startup displayed Off, On (Brave), and Needs setup as appropriate.
- The `w` shortcut opened setup. An invalid key displayed a readable error;
  clearing and pasting a replacement enabled search. Neither pasted key
  appeared in terminal output. The saved credential file had mode `0600`.
- Settings → Web search opened the same screen. Disabling and saving updated
  startup immediately; re-enabling reused the saved key.
- A complete Harness benchmark advertised `web_search`, returned an HTTP
  fixture's source URL to the agent, and completed native write/done calls.
  The generated Python program ran in the real sandbox and printed `42`.
  The saved result recorded one successful Brave search.
- `--no-web-search` suppressed the tool and all search requests for a successful
  second run while preserving the saved On preference.
- A missing saved key displayed Needs setup at 60 columns. Cancelling setup
  returned to the menu without changing the preference.

Automated coverage includes disabled dispatch, result normalization, malformed
responses, authentication/quota/server failures, response limits, deadlines,
cancellation, input validation, call budgets shared across repair, replay
without duplicate requests, credential isolation, and standalone setup without
an OpenRouter key.

Validation: `python -m pytest` — 801 passed, one paid test deselected;
`ruff check .`, `ruff format --check .`, and `git diff --check` passed.
Temporary servers, processes, settings, projects, and recordings were removed
after verification.

## Real LLM tool use

Also verified on 2026-09-09 with paid OpenRouter calls to two real model
families. The production HarnessSession, streaming transport, dispatcher,
Brave HTTP client, file tools, lint, and sandbox execution were used unchanged.
Only the Brave endpoint was replaced with a local HTTP fixture because no
Brave credential was configured. Live Brave authentication/search remains
unverified.

For each model, the fixture returned a fresh random token and source URL that
were absent from the user prompt. The task required searching for that token,
then creating and submitting a Python program that printed the token and URL.
Both generated programs printed exactly the values returned by the fixture,
proving the real LLM issued the native call and consumed its tool response.

| Model | Search calls | Search failures | API turns | Output matched | OpenRouter cost |
|---|---:|---:|---:|---|---:|
| `openai/gpt-5.6-luna` | 1 | 0 | 4 | Yes | $0.0009907 |
| `anthropic/claude-haiku-4.5` | 1 | 0 | 4 | Yes | $0.009313 |

Each used `web_search`, followed by `wb` write, lint, and done. Returned model
IDs matched the requested IDs. The runs used one process at a time, an
eight-turn / 24,000-token build budget per model, and a 15-minute task timeout.
Temporary servers, workspaces, and raw evidence were cleaned up afterward.

## Live search counts

Verified on 2026-09-09 with the real interactive Harness CLI and
`openai/gpt-5.6-luna` through OpenRouter. Brave responses came from a local
HTTP fixture; the model and all Harness tool execution were real.

The model made three separate searches. The live WEB column advanced through
0, 1, 2, and 3. Resizing the same terminal from 80 to 120 to 60 columns preserved
alignment and the count; the wide header displayed WEB SEARCHES. The final
result retained 3 searches, matching the HTTP requests and saved metadata.
The generated Python program printed all three fresh tokens from the search
results. OpenRouter reported $0.00141085 for the run.

Tests also cover zero counts, failed searches, replay without double counting,
repair and compaction, mixed enabled/disabled models, older records, and final
success/failure/cancellation rows. Validation: 825 tests passed, one paid test
deselected; Ruff lint, formatting, and diff checks passed. Temporary processes,
settings, outputs, and recordings were removed after verification.
