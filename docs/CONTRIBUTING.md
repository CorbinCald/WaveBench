# Contributing to WaveBench

Thanks for wanting to hack on WaveBench! This document covers local setup,
running tests, style conventions, and how to structure a change so it's
easy to review.

## Prerequisites

- Python 3.10+
- An [OpenRouter API key](https://openrouter.ai/keys) (only needed to *run*
  the tool — tests never call OpenRouter)

## Local setup

```bash
git clone <repository-url>
cd WaveBench
python -m venv .venv
source .venv/bin/activate            # macOS / Linux
# .venv\Scripts\activate              # Windows
pip install -e '.[dev]'
pre-commit install
```

`pre-commit install` wires up a git hook that runs `ruff check --fix` and
`ruff format` before every commit. If a commit fails because a hook fixed
something, re-stage and re-commit.

## Running tests

```bash
pytest                               # full suite
pytest tests/unit                    # unit tests only (fast)
pytest tests/integration             # integration tests
pytest -k streaming                  # substring filter
pytest -x --lf                       # stop on first failure; re-run last-failed
pytest --cov=wavebench               # coverage report (HTML in htmlcov/)
```

The suite should finish in well under a second. If you're writing a slow
test, mark it with `@pytest.mark.slow` and run it with `pytest -m slow`.

## Style & linting

```bash
ruff check .                         # lint
ruff format .                        # format
ruff check --fix .                   # lint + auto-fix
```

Ruff is configured in `pyproject.toml`. We use line-length 100 and rule set
`E, F, I, B, UP, SIM, RUF`. If a warning seems wrong for a specific line,
use `# noqa: <rule>` with a comment explaining why rather than disabling
the rule globally.

## Where code lives

```
wavebench/
├── __main__.py          CLI entry point
├── api.py               OpenRouter HTTP client
├── core.py              benchmark orchestration + per-model runners
├── models.py            default model mapping + catalog scoring
├── parsers.py           LLM output → code + extension
├── storage.py           JSON persistence (models/config/history)
└── tui/
    ├── components.py    ProgressTracker + analytics display
    ├── interactive.py   model selection + config menus
    └── styles.py        themes, ANSI helpers

tests/
├── unit/                fast, pure-function tests
├── integration/         fake aiohttp server, real streaming code paths
└── characterization/    scaffolding used during refactoring
```

See `docs/architecture.md` for a more detailed map of how the pieces fit
together and where to look when you want to change a specific behavior.

## Writing tests

- **Unit tests** (`tests/unit/`) — pure functions, no network, no file I/O
  beyond `tmp_path`. These should run in a handful of milliseconds each.
- **Integration tests** (`tests/integration/`) — exercise real code paths
  against a mocked collaborator (e.g., `aiohttp.test_utils.TestServer` for
  the OpenRouter client).
- **Characterization tests** (`tests/characterization/`) — temporary
  scaffolding for refactors. Pin current behavior at the module's *public*
  boundary (not internals) so the tests survive the refactor. Delete them
  once the refactor lands and proper unit tests supersede.

Helpful fixtures in `tests/conftest.py`:

- `tmp_state_dir` — redirects `storage.py` paths to an isolated tmp dir.
- `isolated_env` — scrubs WaveBench-relevant env vars.
- pytest's own `capsys` (not a WaveBench fixture) — capture `print()` output.

## Adding a new mode

WaveBench's response modes — `CodeMode`, `TextMode` — implement the
`Mode` protocol in `wavebench/modes/__init__.py`. A mode captures two
mode-specific decisions: how to frame the user's prompt, and how to
parse the raw LLM response into savable content with a file extension.

To add a new mode (e.g., `JsonMode`):

1. **Create the module.** `wavebench/modes/json.py`:

   ```python
   from __future__ import annotations

   import json as _json
   from dataclasses import dataclass

   from wavebench.modes import ParsedOutput


   _SYSTEM_PROMPT_JSON = (
       "You are a JSON generator. Respond with ONLY a valid JSON object "
       "matching the user's request. No preamble, no explanation, no code "
       "fences — just the JSON."
   )


   @dataclass(frozen=True)
   class JsonMode:
       name: str = "json"
       display_name: str = "JSON"

       def frame_prompt(self, user_prompt: str) -> str:
           return f"{_SYSTEM_PROMPT_JSON}\n\nSchema: {user_prompt}"

       def parse_response(self, raw: str) -> ParsedOutput:
           text = raw.strip()
           try:
               _json.loads(text)
           except _json.JSONDecodeError as exc:
               return ParsedOutput(
                   content=text, extension="json", parse_ok=False,
                   parse_error=f"invalid JSON: {exc.msg}",
               )
           return ParsedOutput(content=text + "\n", extension="json", parse_ok=True)


   JSON_MODE = JsonMode()
   ```

2. **Register it.** Edit `wavebench/modes/__init__.py`, add an import
   for your singleton and call `register(JSON_MODE)` alongside the
   built-ins:

   ```python
   from .json import JSON_MODE   # noqa: E402
   register(JSON_MODE)
   ```

   Also add `"JSON_MODE"` to the `__all__` list.

3. **Add unit tests.** In `tests/unit/test_modes.py` add tests for:
   - The registry contains your mode: `"json" in MODES`.
   - `frame_prompt` produces expected framing.
   - `parse_response` returns `parse_ok=True` for valid input and
     `parse_ok=False` with a useful `parse_error` for malformed input.

4. **(Optional) Update the CLI mode-select screen.** Once registered,
   your mode is automatically listed in `wavebench --help` and
   accessible via `wavebench --mode json`. If you want it in the
   interactive startup "Select Mode" box in `__main__.py`, edit the
   `_print_mode_menu` function to include it.

5. **Run the full check.** `pytest && ruff check . && ruff format --check .`
   — all green means you're done.

That's it. No wiring into `core/runner.py` is needed — the mode
abstraction handles framing and parsing, and `run_model(mode, ...)`
treats any registered mode identically.

## Commits & PRs

- Keep commits focused — one logical change per commit.
- Include a short commit message ("what" + "why", not "how").
- For PRs that touch `core.py`, `tui/components.py`, or `tui/interactive.py`,
  prefer small incremental PRs over large sweeping ones (these files are
  being decomposed; large changes collide with the refactor).
- Run `ruff check .` and `pytest` before pushing.

## Getting unstuck

- `docs/superpowers/specs/` — design docs for ongoing architectural work.
- `docs/architecture.md` — "where does X live" reference.
- README — user-facing feature overview.
