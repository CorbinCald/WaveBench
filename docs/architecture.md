# WaveBench Architecture

A current "where does X live" reference for navigating the codebase. Pair this
with the module-level docstrings in each `.py` file for local details.

## 10-second picture

```text
user prompt
  │
  ▼
wavebench.__main__
  ├─ loads config/models/history state from storage.py
  ├─ optionally opens tui.menus.run_config_menu
  └─ dispatches to core.main_async
          │
          ▼
    core.orchestrator.main_async
      ├─ resolves CodeMode/TextMode/TTSMode
      ├─ asks parsers.get_directory_name for benchmarkResults/<dir>
      ├─ starts tui.progress.ProgressTracker
      ├─ fans out concurrent core.runner.run_model tasks
      ├─ auto-opens / auto-installs when configured
      └─ records analytics with storage.record_run
                │
                ▼
          core.runner.run_model
            ├─ mode.frame_prompt(user_prompt)
            ├─ api.call_model_streaming(...)
            ├─ mode.parse_response(raw)
            └─ writes one output artifact per model
```

## Package map

```text
wavebench/
├── __init__.py
├── __main__.py                 CLI args, startup UI, state loading, dispatch
├── api.py                      OpenRouter client, SSE parser, TTS speech, retries, catalog fetch
├── models.py                   default text/TTS mappings, catalog scoring, TTS helpers
├── parsers.py                  code extraction and prompt-derived directory names
├── storage.py                  JSON persistence for local state and analytics
│
├── modes/
│   ├── __init__.py             Mode protocol, ParsedOutput, MODES registry
│   ├── code.py                 CodeMode prompt framing + parser wrapper
│   ├── text.py                 TextMode prompt framing + Markdown pass-through
│   └── tts.py                  TTSMode prompt framing + audio-byte pass-through
│
├── core/
│   ├── __init__.py             public re-exports for core package users
│   ├── orchestrator.py         main_async run coordinator and result display
│   ├── runner.py               per-model run_model + get_unique_filename
│   ├── auto_open.py            viewer/terminal/tab launching helpers
│   └── auto_install.py         dependency detection, venv creation, pip install
│
└── tui/
    ├── __init__.py
    ├── styles.py               themes, ANSI helpers, box drawing, formatting
    ├── input.py                raw key reads, resize-aware key handling
    ├── line_editor.py          prompt editor with history/navigation
    ├── tts_player.py           arrow-key TTS output browser/player
    ├── progress/
    │   ├── __init__.py         re-exports ProgressTracker, render_idle_wave
    │   ├── tracker.py          live multi-model progress UI
    │   └── wave.py             braille wave / pulse rendering primitives
    ├── analytics/
    │   ├── __init__.py         re-exports compute_cost, display_analytics
    │   ├── cost.py             token-cost calculation helper
    │   └── table.py            lifetime analytics table renderer
    └── menus/
        ├── __init__.py         re-exports menu entry points
        ├── _shared.py          price/name/filter/layout helpers
        ├── model_list.py       model catalog browser + selection flow
        └── config_menu.py      tabbed Models/TTS/Settings config menu
```

## Current module sizes

Line counts are approximate and useful mostly for spotting oversized files:

| Module | Lines | Role |
|---|---:|---|
| `wavebench/api.py` | 880+ | OpenRouter HTTP/SSE client, TTS speech, reasoning-effort negotiation, model catalog |
| `wavebench/tui/progress/tracker.py` | 728 | Animated progress tracker and final progress-state rendering |
| `wavebench/tui/styles.py` | 635 | Theme definitions, ANSI helpers, box drawing, formatting |
| `wavebench/tui/menus/config_menu.py` | 710 | Interactive tabbed Models/TTS/Settings menu |
| `wavebench/core/orchestrator.py` | 382 | Top-level benchmark run coordinator |
| `wavebench/__main__.py` | 371 | CLI parsing, startup mode/prompt UI, config dispatch |
| `wavebench/core/auto_open.py` | 324 | Viewer, terminal, and tab launching |
| `wavebench/tui/menus/model_list.py` | 313 | Interactive model list browser |
| `wavebench/parsers.py` | 302 | Code extraction and directory naming |
| `wavebench/tui/progress/wave.py` | 274 | Wave animation primitives |
| `wavebench/core/runner.py` | 272 | Per-model streaming, parsing, file writing, auto-install hook |
| `wavebench/tui/line_editor.py` | 263 | Prompt input editor |
| `wavebench/tui/analytics/table.py` | 207 | Lifetime analytics table |
| `wavebench/tui/input.py` | 161 | Raw keyboard input helpers |
| `wavebench/storage.py` | 152 | JSON persistence |
| `wavebench/core/auto_install.py` | 144 | Dependency detection and venv install helpers |
| `wavebench/models.py` | 105 | Default models and catalog scoring |
| `wavebench/modes/__init__.py` | 104 | Mode protocol and registry |

## Data flow: one benchmark run

1. **CLI / startup (`wavebench.__main__`)**
   - Parses flags such as `--prompt`, `--mode`, `--text`, `--config`,
     `--open`, `--auto-install`, `--stats`, and `--clear-history`.
   - Loads `.benchmark_models.json` and `.benchmark_config.json` through
     `storage.load_models()` / `storage.load_config()`.
   - Starts a background OpenRouter catalog fetch for the config menu and
     pricing lookup.
   - If no prompt was supplied, renders the interactive Code/Text selector
     and prompt editor.

2. **Run setup (`core.orchestrator.main_async`)**
   - Resolves the active mode: explicit `--mode`, then legacy `--text`, then
     code mode by default. Code mode is instantiated with `allow_deps=True`
     when auto-install is enabled; TTS mode is instantiated with configured
     voice/format/speed.
   - Determines default output extension (`.md` for text, `.py` for Python-ish
     prompts, otherwise `.html`).
   - Creates an async task for `parsers.get_directory_name()` so the output
     directory can be prepared while model calls are starting. Directory
     naming uses the configured mode: `llm` or local `slug`.
   - Starts `tui.progress.ProgressTracker` and builds any reasoning-effort
     notices for the ticker.

3. **Per-model work (`core.runner.run_model`)**
   - Calls `mode.frame_prompt(user_prompt)`.
   - Streams from OpenRouter through `api.call_model_streaming()` with progress
     and retry callbacks, or calls `api.call_tts_speech()` for TTS audio bytes.
   - Passes the raw response to `mode.parse_response()`.
   - Creates a unique filename with `get_unique_filename()` and writes the
     parsed content into `benchmarkResults/<prompt_dir>/`.
   - If code mode + Python output + auto-install + auto-open are enabled,
     detects packages, ensures a `.venv` in the output directory, and installs
     packages before opening.
   - If `auto_open == "incremental"`, opens the artifact as soon as it is saved.
   - In TTS mode, successful audio artifacts can be browsed with arrow keys and
     played through WaveBench's native audio backend with Enter/Space in the
     post-run `tui.tts_player` screen, without launching an external app.

4. **Finish / reporting (`core.orchestrator.main_async`)**
   - For `auto_open == "after_all"`, opens all successful artifacts after every
     model has finished.
   - Stops the progress tracker, prints a fallback run-results table if needed,
     computes estimated costs, records the run with `storage.record_run()`, and
     renders compact lifetime analytics.

## Public seams and import compatibility

The package intentionally re-exports common entry points from package
`__init__.py` files:

| Import | Provided by |
|---|---|
| `from wavebench.core import main_async` | `wavebench/core/__init__.py` → `orchestrator.py` |
| `from wavebench.core import run_model, get_unique_filename` | `wavebench/core/__init__.py` → `runner.py` |
| `from wavebench.tui.progress import ProgressTracker, render_idle_wave` | `wavebench/tui/progress/__init__.py` |
| `from wavebench.tui.analytics import compute_cost, display_analytics` | `wavebench/tui/analytics/__init__.py` |
| `from wavebench.tui.menus import run_model_selection, run_config_menu` | `wavebench/tui/menus/__init__.py` |
| `from wavebench.modes import MODES, Mode, ParsedOutput` | `wavebench/modes/__init__.py` |

`tests/unit/test_public_api.py` is the contract test for these imports.

## Where to look for specific changes

| If you want to change… | Start here |
|---|---|
| CLI flags, startup mode selection, prompt history | `wavebench/__main__.py` |
| OpenRouter request/response behavior, retries, SSE parsing, TTS speech | `wavebench/api.py` |
| Reasoning-effort payload formats and per-model effort mapping | `wavebench/api.py` (`_reasoning_attempts`, `_supported_efforts`) |
| Model catalog ranking, default text/TTS mappings, and TTS model/voice/format helpers | `wavebench/models.py` |
| Code extraction from model responses | `wavebench/parsers.py` and `wavebench/modes/code.py` |
| Adding a new response mode | `wavebench/modes/` and the guide in `docs/CONTRIBUTING.md` |
| TTS output navigation/playback | `wavebench/tui/tts_player.py` |
| Benchmark fan-out, output directory setup, history recording | `wavebench/core/orchestrator.py` |
| Per-model file writing and parse-failure handling | `wavebench/core/runner.py` |
| Auto-open terminal/viewer behavior | `wavebench/core/auto_open.py` |
| Python dependency detection and venv install | `wavebench/core/auto_install.py` |
| Live progress animation and model status display | `wavebench/tui/progress/tracker.py` and `wavebench/tui/progress/wave.py` |
| Lifetime analytics table | `wavebench/tui/analytics/table.py` |
| Cost calculation | `wavebench/tui/analytics/cost.py` |
| Model browser menu | `wavebench/tui/menus/model_list.py` |
| Tabbed configuration menu | `wavebench/tui/menus/config_menu.py` |
| Themes, colors, box drawing, width helpers | `wavebench/tui/styles.py` |
| Persistent state files | `wavebench/storage.py` |

## Modes

Modes are small value objects implementing `wavebench.modes.Mode`:

- `CodeMode` frames prompts for dependency-free single-file code by default.
  When auto-install is on, the orchestrator creates `CodeMode(allow_deps=True)`
  so the prompt permits PyPI packages.
- `TextMode` frames prompts for Markdown prose and saves the raw response as
  `.md`.
- `TTSMode` sends the user text to OpenRouter's `/audio/speech` endpoint,
  saves returned audio bytes with provider-compatible extensions (`.mp3` by default
  for OpenAI/Voxtral/Zonos and most speech models, `.pcm` for Gemini TTS), maps
  known non-OpenAI defaults to provider voices such as Gemini `Kore`, Zonos
  `american_female`, Voxtral `en_paul_neutral`, and Kokoro `af_alloy`, and plays
  saved outputs through the native TTS player without launching an external app.

A mode must provide:

```python
def frame_prompt(self, user_prompt: str) -> str: ...
def parse_response(self, raw: str) -> ParsedOutput: ...
```

Registered modes are available through `wavebench --mode <name>`. The current
interactive startup selector displays Code/Text explicitly; add key handling in
`__main__.py` if a new mode should appear there.

## Persistent state

WaveBench stores local state in the current working directory:

| File | Contents |
|---|---|
| `.benchmark_models.json` | selected `{short_name: openrouter_id}` mapping; TTS mode filters this to TTS-capable IDs and falls back to bundled TTS defaults if none are selected |
| `.benchmark_config.json` | `reasoning_effort`, `analytics_sort`, `theme`, `directory_naming`, `auto_open`, `auto_install`, `tts_voice`, `tts_format`, `tts_speed` |
| `.benchmark_history.json` | `{version: 1, runs: [...]}` analytics history |
| `.benchmark_query_history.<mode>` | mode-specific prompt-entry history for the interactive editor (`code`, `text`, `tts`, `image`) |

For backward compatibility, Code mode reads a legacy `.benchmark_query_history`
file until `.benchmark_query_history.code` is created on the first new Code
prompt.

Persistent-state path helpers call `os.getcwd()` at use time. This keeps
tests easy to isolate with `monkeypatch.chdir(tmp_path)` and gives each project
directory its own WaveBench state, but it also means running from a different
directory uses different settings/history.

## Testing tiers

| Tier | Location | Purpose |
|---|---|---|
| Unit | `tests/unit/` | Pure functions, mode behavior, storage round-trips, public imports |
| Integration | `tests/integration/` | Mocked OpenRouter/SSE behavior through real API-client code paths |
| Characterization | `tests/characterization/` | Contract tests around refactor-sensitive seams such as core, menus, and progress |

Fixtures in `tests/conftest.py`:

- `tmp_state_dir` — changes CWD to a temporary directory for state-file tests.
- `isolated_env` — removes `OPENROUTER_API_KEY` from the environment.

Use pytest's built-in `capsys`, `monkeypatch`, and `tmp_path` for output,
patching, and temporary files.

This architecture document is the authoritative map of the current codebase.
