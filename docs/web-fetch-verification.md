# Source-page reading verification

Verified locally on 2026-09-12.

- The real interactive WaveBench CLI opened the web setup screen and completed
  a Harness run in a PTY resized through 120, 80, and 60 columns. The READS/GET
  column stayed aligned and separate from WEB SEARCHES/WEB.
- A local HTTP/SSE provider fixture issued seven native tool turns: search,
  read, continuation, a blocked private-address read, write, lint, and done.
  The real transport, dispatcher, source reader, sandbox, and program execution
  ran unchanged except for routing the fixture hostname to its local server.
  The generated program printed a fresh verification value that appeared only
  in the source page. Final metadata recorded one search, three read attempts,
  and one read failure. Continuation reused the same page snapshot, so the
  source server received one request.
- The production reader, with its public-address checks unchanged, also read
  live Python asyncio documentation, the Artificial Analysis models page, and
  GitHub's public CPython repository JSON endpoint. These checks verified live
  retrieval and extraction, not the accuracy or completeness of third-party
  benchmark data. JavaScript rendering and PDF extraction are not implemented.
- Automated tests cover HTML tables, links, source dates, plain text/Markdown/
  JSON, continuation, output escaping, disabled tools, separate attempt budgets,
  replay, repair, HTTP failures, deadlines, cancellation, format/size limits,
  credentials and cookies, redirect checks, and private/mixed DNS answers.
  The lifecycle test verifies search → read → generated-program output.

Validation: `python -m pytest` — 1041 passed, one paid test deselected;
`ruff check .`, `ruff format --check .`, and `git diff --check` passed.
No paid model or Brave API calls were made. Temporary fixture servers,
configuration, generated projects, and raw terminal recordings were removed.
