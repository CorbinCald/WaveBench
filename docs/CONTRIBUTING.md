# Contributing to WaveBench

Jump on in! This document covers local setup,
running tests, style conventions, and how to structure a change so it's easy
to review.

## Prerequisites

- Python 3.10+
- An [OpenRouter API key](https://openrouter.ai/keys) only if you want to run
  real benchmarks. The test suite never calls OpenRouter.

## Local setup

```bash
git clone <repository-url>
cd WaveBench
python -m venv .venv
source .venv/bin/activate            # macOS / Linux
# .venv\Scripts\activate             # Windows
pip install -e '.[dev]'
pre-commit install
```

`pre-commit install` wires up a git hook that runs `ruff check --fix` and
`ruff format` before every commit. If a commit fails because a hook fixed
something, re-stage the changed files and re-commit.

## Running tests

```bash
python -m pytest                      # full suite
python -m pytest tests/unit            # unit tests only
python -m pytest tests/integration     # integration tests
python -m pytest tests/characterization # contract tests for refactor seams
python -m pytest -k streaming          # substring filter
python -m pytest -x --lf               # stop on first failure; re-run last-failed
python -m pytest --cov=wavebench       # coverage report (HTML in htmlcov/)
```

The suite is intentionally fast; it currently runs in well under a second in
the project venv. If your shell's default Python does not have the dev extras
installed, use `.venv/bin/python -m pytest ...`.

If you're writing a slow test, mark it with `@pytest.mark.slow` and run it
with `python -m pytest -m slow` when needed.

## Style & linting

```bash
ruff check .                          # lint
ruff format .                         # format
ruff check --fix .                    # lint + auto-fix
ruff format --check .                 # verify formatting only
```

Ruff is configured in `pyproject.toml`. We use line-length 100 and the rule
set `E, F, I, B, UP, SIM, RUF`. If a warning is wrong for a specific line,
use `# noqa: <rule>` with a short explanation rather than disabling the rule
globally.

## Where code lives

```text
wavebench/
├── __main__.py                 CLI entry point, interactive startup, dispatch
├── api.py                      OpenRouter HTTP/SSE client, TTS speech, catalog fetch
├── models.py                   default text/TTS mappings, TTS voice/format helpers, catalog scoring
├── parsers.py                  LLM output → code + extension; directory names
├── storage.py                  JSON persistence for models/config/history
├── modes/                      response-mode protocol and built-ins
│   ├── __init__.py             Mode, ParsedOutput, MODES registry
│   ├── code.py                 CodeMode prompt framing + parser wrapper
│   ├── text.py                 TextMode prompt framing + Markdown pass-through
│   └── tts.py                  TTSMode prompt framing + audio-byte pass-through
├── core/                       benchmark run orchestration and artifact handling
│   ├── orchestrator.py         main_async, concurrency, result display, history
│   ├── runner.py               run_model and get_unique_filename
│   ├── auto_open.py            viewer/terminal/tab launching
│   └── auto_install.py         dependency detection and per-run venv setup
└── tui/
    ├── styles.py               themes, ANSI helpers, boxes, formatting
    ├── input.py                raw key reads
    ├── line_editor.py          prompt editor
    ├── tts_player.py           arrow-key TTS output browser/player
    ├── progress/               ProgressTracker and wave rendering
    ├── analytics/              display_analytics and compute_cost
    └── menus/                  model browser and configuration menu

tests/
├── unit/                       pure functions and mode behavior
├── integration/                mocked OpenRouter/SSE paths
└── characterization/           contract tests around refactor-sensitive seams
```

See `docs/architecture.md` for a more detailed map of how the pieces fit
together and where to look when changing specific behavior.

## Writing tests

- **Unit tests** (`tests/unit/`) — pure functions, no network, no file I/O
  beyond `tmp_path`. These should run in a handful of milliseconds each.
- **Integration tests** (`tests/integration/`) — exercise real code paths
  against mocked collaborators, such as an `aiohttp.test_utils.TestServer`
  standing in for OpenRouter.
- **Characterization tests** (`tests/characterization/`) — contract tests for
  public behavior at refactor seams (`core`, progress, menus). Prefer asserting
  state transitions and import compatibility over byte-for-byte ANSI output.

Helpful fixtures in `tests/conftest.py`:

- `tmp_state_dir` — changes the working directory to an isolated temp dir so
  `.benchmark_*.json` files are test-local.
- `isolated_env` — removes `OPENROUTER_API_KEY` from the environment.
- pytest's built-in `capsys` — capture `print()` output.

## Adding a new mode

WaveBench's response modes — currently `CodeMode`, `TextMode`, and `TTSMode` — implement
the `Mode` protocol in `wavebench/modes/__init__.py`. A mode captures two
mode-specific decisions: how to frame the user's prompt, and how to parse the
raw LLM response into savable content with a file extension.

To add a new mode, for example `JsonMode`:

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

       def parse_response(self, raw: str | bytes) -> ParsedOutput:
           text = raw.strip()
           try:
               _json.loads(text)
           except _json.JSONDecodeError as exc:
               return ParsedOutput(
                   content=text,
                   extension="json",
                   parse_ok=False,
                   parse_error=f"invalid JSON: {exc.msg}",
               )
           return ParsedOutput(content=text + "\n", extension="json", parse_ok=True)


   JSON_MODE = JsonMode()
   ```

2. **Register it.** Edit `wavebench/modes/__init__.py`, import the singleton
   and call `register(JSON_MODE)` alongside the built-ins:

   ```python
   from .json import JSON_MODE  # noqa: E402
   register(JSON_MODE)
   ```

   Also add `"JSON_MODE"` to `__all__`.

3. **Add unit tests.** In `tests/unit/test_modes.py` add tests for:
   - the registry contains your mode: `"json" in MODES`;
   - `frame_prompt` produces expected framing;
   - `parse_response` returns `parse_ok=True` for valid input and
     `parse_ok=False` with a useful `parse_error` for malformed input.

4. **Decide how it appears in the interactive startup UI.** Once registered,
   your mode is automatically accepted by `wavebench --mode json` and appears
   in `wavebench --help`. The interactive startup selector currently displays
   Code/Text/TTS shortcuts explicitly, so update `_print_mode_menu()` and key
   handling in `wavebench/__main__.py` if the new mode should be selectable
   there too.

5. **Run the full check.**

   ```bash
   python -m pytest
   ruff check .
   ruff format --check .
   ```

No wiring into `core/runner.py` is normally needed. The mode abstraction
handles framing and parsing, and `run_model(mode, ...)` treats registered
modes uniformly.

## Commits & PRs

- Keep commits focused — one logical change per commit.
- Include a short commit message that explains the what and why.
- Prefer small incremental PRs for changes that touch the run loop, streaming
  client, progress tracker, or interactive menus. Those areas have many user-
  visible behaviors and are covered by characterization tests.
- Run `ruff check .`, `ruff format --check .`, and `python -m pytest` before
  pushing.

## Getting unstuck

- `docs/architecture.md` — current "where does X live" reference.
- `README.md` — user-facing feature overview.
