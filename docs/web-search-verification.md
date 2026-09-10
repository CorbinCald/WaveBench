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
