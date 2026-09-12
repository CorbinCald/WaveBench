# Source extraction and research budget verification

Verified locally on 2026-09-12.

- Reopened all seven distinct Artificial Analysis URLs behind the 13 rejected
  reads: the models leaderboard and the Claude Fable 5.1, Gemini 3.5 Flash-Lite,
  Gemini 3.8 Flash, GPT-5.6 Luna, GPT-6 Astra, and Mercury 2 model pages. All
  returned readable content. Responses were 2.76–3.52 MB before extraction;
  extracted text was approximately 18,600–71,000 characters.
- Confirmed that advertising Markdown in `Accept` made BenchLM's `/llm-speed`
  return a 490-character mirror with empty tables. The revised request returned
  HTML with populated model measurements and 38,976 characters of extracted text.
- The legacy Three.js `docs/#manual/en/introduction/Installation` URL resolved
  to `/manual/pages/installation.html`, returning 7,648 characters including
  installation commands and CDN instructions. The reader preserves the requested
  and resolved URLs; it does not execute JavaScript.
- Exercised the real interactive CLI in a 120-column PTY with isolated settings
  and a local HTTP/SSE provider. The provider searched, read a 3.5 MB compressed
  HTML fixture, and continued the same cached section. After the configured
  three research turns, its next request offered only `wb`. It wrote and linted
  a program, then submitted with `done` alone. The sandboxed program printed a
  fresh value available only in the source. The run passed with five model
  turns, six tools, one search, two reads, one source HTTP request, and no tool
  failures. Production transport, extraction, budget enforcement, workspace
  operations, linting, and execution were exercised; only provider endpoints
  and the fixture hostname's DNS mapping were replaced.
- Automated cases cover large compressed HTML, decompression and element limits,
  empty tables, navigation, section selection, independent text limits, research
  turn/time/token reserves, batch deadlines, exhausted tools, failed calls,
  replay, repair persistence, finishing warnings, and successful submission
  after research closes. Live reads validate retrieval and extraction; scripted
  provider checks validate the controller workflow, not future model behavior.

Validation: `python -m pytest` — 1,081 passed, one paid test deselected;
`ruff check .`, `ruff format --check .`, and `git diff --check` passed.

No paid model or Brave calls were made. Temporary servers, generated projects,
settings, downloaded pages, and raw terminal recordings were removed after
verification. The public-address and credential protections remain in place.
