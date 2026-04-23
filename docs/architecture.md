# WaveBench Architecture

A "where does X live" reference for navigating the codebase. Updated as
structure evolves. Pair with the module-level docstrings (each `.py`
opens with a paragraph on its role) for the freshest detail.

## 10-second picture

```
user prompt ──► __main__.py ──► core.main_async ──► parallel [process_model] ──► writes files
                                    │                        │
                                    │                        ├─ api.call_model_streaming ──► OpenRouter SSE
                                    │                        ├─ parsers.parse_llm_output  ──► code + extension
                                    │                        └─ tui.ProgressTracker      ──► live UI update
                                    │
                                    └─ storage.record_run ──► .benchmark_history.json
```

## Module map

| Module | Role | Size |
|---|---|---|
| `__main__.py` | CLI parsing, state loading, dispatch | 317 |
| `api.py` | OpenRouter HTTP (streaming + non-streaming), reasoning-effort negotiation, catalog fetch | 681 |
| `core.py` | Benchmark orchestration, per-model runners, auto-open, auto-install | 1025 |
| `models.py` | Default MODEL_MAPPING, catalog scoring, stealth classification | 81 |
| `parsers.py` | LLM-output → code + extension (4-stage cascade) | 256 |
| `storage.py` | JSON persistence for models/config/history | 115 |
| `tui/components.py` | ProgressTracker + analytics display + wave math | 1085 |
| `tui/interactive.py` | Keyboard input + line editor + model/config menus | 1195 |
| `tui/styles.py` | Theme system, ANSI helpers, box drawing | 456 |

> **Note:** Three of these (`core.py`, `tui/components.py`,
> `tui/interactive.py`) exceed 1,000 lines. An active maintainability
> initiative decomposes them into focused sub-packages. See
> `docs/superpowers/specs/2026-04-22-maintainability-foundation-design.md`.

## Data flow: a single benchmark run

1. **`__main__.main()`** parses args, loads `.benchmark_models.json` and
   `.benchmark_config.json` via `storage.load_*`, then picks up the user's
   prompt (flag, interactive entry, or readline history).
2. **`core.main_async()`** derives a directory name from the prompt
   (`parsers.get_directory_name`, a fast LLM call), creates
   `benchmarkResults/<dir>/prompt.txt`, and starts a `ProgressTracker`.
3. For each selected model, **`core.process_model()`** (code mode) or
   **`core.process_model_text()`** (text mode) is spawned as a concurrent
   task. Each:
   - calls **`api.call_model_streaming()`** with a progress callback that
     feeds the `ProgressTracker`;
   - in code mode, passes the content through **`parsers.parse_llm_output()`**
     to extract code + language + extension;
   - writes the output file with a unique filename
     (`core.get_unique_filename`);
   - returns a result dict (status, timing, file, usage).
4. Once all models finish, `main_async` tears down the `ProgressTracker`
   (which renders the final leaderboard), then calls
   **`storage.record_run()`** to persist a history entry.
5. If `--open` is active, **`core._open_files_as_tabs()`** (or the
   single-file opener) launches the results in a terminal or viewer.
6. If `--auto-install` is active, **`core._detect_dependencies` + `_install_packages`**
   spin up a `.venv` inside the output dir and pip-install detected deps.

## Where to look for specific changes

| If you want to change… | Start here |
|---|---|
| How models are ranked in the config menu | `models._model_score` |
| The set of built-in themes | `tui/styles.py` (search for theme dict) |
| How code is extracted from an LLM response | `parsers.parse_llm_output` (4 stages) |
| The streaming SSE parser | `api.call_model_streaming._do_stream` |
| Which reasoning-effort formats to try | `api._reasoning_attempts` |
| The live progress bar animation | `tui/components.py::ProgressTracker._animate` |
| The leaderboard / analytics display | `tui/components.display_analytics` |
| The config-menu layout | `tui/interactive.interactive_config_menu` |
| CLI flag handling | `__main__.main` |
| What gets saved to history | `storage.record_run` |
| Auto-open terminal detection | `core._find_terminal` + `_open_file_in_tab` |

## Persistent state

Three gitignored JSON files in the working directory:

- `.benchmark_models.json` — `{short_name: openrouter_id}` mapping
- `.benchmark_config.json` — `{theme, reasoning_effort, analytics_sort, auto_open, auto_install}`
- `.benchmark_history.json` — `{version: 1, runs: [...]}` append-only log

Readline prompt history also persists in `.benchmark_query_history`.

### Implicit dependency on CWD

`storage.py` computes its paths via `os.getcwd()` at every call, not via
an explicit config. This means:

- Tests can isolate with `monkeypatch.chdir(tmp_path)`.
- Running `wavebench` from a different directory writes state into *that*
  directory — intentional for per-project separation but surprising if
  you didn't realize.

A future refactor may make paths explicit at the module boundary; tracked
in `docs/superpowers/specs/`.

## Testing tiers

| Tier | Location | Purpose | Speed |
|---|---|---|---|
| Unit | `tests/unit/` | pure functions | milliseconds |
| Integration | `tests/integration/` | real code paths, mocked collaborators | tens of ms |
| Characterization | `tests/characterization/` | scaffolding for refactors | varies |

Fixtures in `tests/conftest.py`:
- `tmp_state_dir` — isolated working directory for storage tests.
- `isolated_env` — scrubs `OPENROUTER_API_KEY` from env.

Pytest's own `capsys` / `caplog` / `monkeypatch` / `tmp_path` are the
preferred tools for output capture, env/attr patching, and temp files.

## Design documents

Active specs live in `docs/superpowers/specs/`:

- `2026-04-11-model-attribution-design.md` — auto-open artifact attribution
- `2026-04-22-maintainability-foundation-design.md` — current three-phase
  refactor: Foundation → Decompose → Modes

Read the spec before touching a file it names; it explains the boundaries
being preserved and what the end state looks like.
