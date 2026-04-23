# Maintainability Foundation — Design

**Date:** 2026-04-22
**Status:** Approved (ready for implementation planning)
**Scope:** Three phased deliverables that together make the WaveBench codebase safe to change, modular, and extensible. Evaluators, analytics/reporting, and new TUI features are explicitly parked for future brainstorms.

---

## 1. Motivation

WaveBench is a single-author CLI tool (5,666 LOC across 11 Python files) that benchmarks LLMs via OpenRouter. The goal of this effort is to ensure **future maintainability, modularity, and ease of building on** the codebase. The current audit identified three structural concerns:

1. **No automated tests at all.** No `pytest` harness, no `conftest.py`, no `tests/` directory. Refactors carry silent regression risk.
2. **Three files exceed 1,000 lines.** `wavebench/core.py` (1,025), `wavebench/tui/components.py` (1,085), `wavebench/tui/interactive.py` (1,195). Each is a single file encoding multiple responsibilities.
3. **No structural seams for expected extensions.** The user identified four extension vectors (new response modes, evaluators, richer analytics, new TUI features). Modes in particular are implemented as an `--text` flag branch through `process_model` / `process_model_text` rather than a first-class abstraction.

This spec addresses (1), (2), and the **mode seam** portion of (3). Evaluators, analytics renderers, and new TUI features are parked.

### 1.1 Why these three deliverables and not others

The user chose these three because they are *structural* rather than *feature* work — they make the codebase a better host for any future feature, whereas evaluators/analytics/TUI-features are features that would sit on top. The ordering is dictated by dependency: Foundation must precede Decompose (need tests to refactor safely); Decompose must precede Modes (the collapsed `run_model` replaces two siblings that only exist after `core.py` is split).

## 2. Target architecture (end state)

After all three deliverables land, the package layout is:

```
wavebench/
├── __init__.py
├── __main__.py                      CLI entry — resolves --mode and dispatches
├── api.py                           OpenRouter client — UNCHANGED (docstrings + tests)
├── models.py                        MODEL_MAPPING + scoring — UNCHANGED (docstrings + tests)
├── parsers.py                       code extraction — UNCHANGED (docstrings + tests)
├── storage.py                       JSON persistence — UNCHANGED (docstrings + tests)
│
├── modes/                           NEW (Deliverable #3)
│   ├── __init__.py                  Mode protocol, ParsedOutput dataclass, MODES registry
│   ├── code.py                      CodeMode — wraps parsers.py
│   └── text.py                      TextMode — pass-through
│
├── core/                            was core.py (Deliverable #2)
│   ├── __init__.py                  re-exports main_async (preserves import path)
│   ├── orchestrator.py              parallel run driver (main_async)
│   ├── runner.py                    run_model(mode, ...) — was process_model + process_model_text
│   ├── auto_open.py                 file-open/terminal/tab subsystem
│   └── auto_install.py              dep-detection + venv subsystem
│
└── tui/
    ├── __init__.py
    ├── styles.py                    theme system — UNCHANGED
    ├── input.py                     raw keyboard reads (promoted from interactive.py)
    ├── line_editor.py               readline-style prompt editor (promoted)
    ├── progress/                    was half of components.py
    │   ├── __init__.py              re-exports ProgressTracker
    │   ├── tracker.py               ProgressTracker class
    │   └── wave.py                  braille wave math
    ├── analytics/                   was other half of components.py
    │   ├── __init__.py              re-exports display_analytics
    │   ├── table.py                 display_analytics + sort/format helpers
    │   └── cost.py                  compute_cost (shared with progress)
    └── menus/                       was interactive.py
        ├── __init__.py              re-exports run_model_selection, run_config_menu
        ├── _shared.py               small helpers (format_price, short_name, _fit, ...)
        ├── model_list.py            interactive_model_menu, run_model_selection
        └── config_menu.py           interactive_config_menu, run_config_menu (~498 lines, deferred)

tests/
├── conftest.py                      tmp_state_dir, mock_openrouter_stream, silent_stdout
├── unit/
│   ├── test_parsers.py
│   ├── test_storage.py
│   ├── test_models_scoring.py
│   ├── test_modes.py                (Deliverable #3)
│   └── test_public_api.py           import-compatibility enumeration
├── integration/
│   ├── test_api_streaming.py
│   └── test_orchestrator.py
├── characterization/                temporary scaffolding, deleted after refactor lands
│   ├── test_core_contract.py
│   ├── test_progress_contract.py
│   ├── test_menus_contract.py
│   └── test_mode_parity.py
└── fixtures/
    └── reference_run/               recorded pre-refactor reference outputs

docs/
├── superpowers/
│   ├── specs/
│   └── plans/
├── CONTRIBUTING.md                  NEW (Deliverable #1; expanded in #3)
└── architecture.md                  NEW (Deliverable #1; updated after #3)

.pre-commit-config.yaml               NEW (Deliverable #1)
.github/workflows/ci.yml              OPTIONAL (Deliverable #1) — if GitHub
pyproject.toml                        adds [tool.ruff], [tool.pytest.ini_options], [project.optional-dependencies.dev]
```

### 2.1 Architectural decisions

- **Packages-as-boundaries with re-exports.** Each decomposed file becomes a package (`core/`, `tui/progress/`, `tui/analytics/`, `tui/menus/`). The package `__init__.py` re-exports previously-public names so every existing import path continues to work without caller changes.
- **Tests live outside the source tree** in top-level `tests/`, organized by *kind* (unit/integration/characterization), not by mirroring source structure.
- **`api.py`, `models.py`, `parsers.py`, `storage.py`, `tui/styles.py` are unchanged** — they already have the right shape. They receive docstrings and tests, nothing else.
- **Mode is its own package**, not inside `core/`. Modes are a cross-cutting concept future evaluators and mode-specific rendering will also consume.

## 3. Deliverable #1 — Foundation

Purpose: add the test harness, tooling, and documentation baseline needed before any structural refactor is safe.

### 3.1 Tooling & scaffolding

- Add `[project.optional-dependencies.dev]` with `pytest>=8`, `pytest-asyncio>=0.23`, `pytest-cov`, `ruff>=0.5`, `pre-commit`.
- Add `[tool.ruff]` section: `line-length = 100`, `target-version = "py310"`, lint select `["E", "F", "I", "B", "UP", "SIM", "RUF"]`.
- Add `[tool.pytest.ini_options]`: `testpaths = ["tests"]`, `asyncio_mode = "auto"`, `addopts = "--strict-markers"`.
- Add `.pre-commit-config.yaml` with `ruff-pre-commit` hook (`check` + `format`).
- Add `.github/workflows/ci.yml` (optional — only if the repo is on GitHub): Python 3.10/3.11/3.12 matrix, `ruff check`, `ruff format --check`, `pytest`.

### 3.2 Documentation

- **Module-level docstrings** on every `.py` in `wavebench/` (one short paragraph).
- **`docs/CONTRIBUTING.md`** — dev setup, test commands, style guide, PR conventions. Includes a stub "Adding a new mode" section (fleshed out in Deliverable #3).
- **`docs/architecture.md`** — package map, prompt-to-output data flow, "where to look for X" section.
- **README** — add a "Development" section linking to `CONTRIBUTING.md`.

### 3.3 Unit tests — pure modules

- **`tests/unit/test_parsers.py`** — JSON extraction, fenced extraction, fallback extraction, extension detection (Python, HTML, JS, Go, Rust, unknown), edge cases (empty response, malformed JSON, multiple fenced blocks). Excludes `generate_directory_name()` (LLM call).
- **`tests/unit/test_storage.py`** — round-trip for models/config/history; corrupted-JSON fallback; file-not-exist fallback.
- **`tests/unit/test_models_scoring.py`** — `_model_score()` ranking assertions on a known catalog; provider-tier weighting; pricing component ordering.

### 3.4 Integration tests — OpenRouter client

- **`tests/integration/test_api_streaming.py`** — `aiohttp.test_utils.TestServer` serving canned SSE events. Asserts: token accumulation, progress-callback firing, completion event, usage metadata capture. Retry behavior if `api.py` has retries.
- **`tests/conftest.py`** — `tmp_state_dir` (monkeypatches `storage.py` paths), `mock_openrouter_stream` (canned SSE events), `silent_stdout` (suppress TUI output in tests).

### 3.5 Out of scope for Deliverable #1

- Tests for `core.py`, `tui/components.py`, `tui/interactive.py` — deferred to #2 as characterization tests.
- Type-checker setup (mypy/pyright).
- Logging framework changes.
- Coverage percentage gates.

### 3.6 Completion criteria

- `pip install -e '.[dev]'` yields a working dev env from a clean venv.
- `ruff check .` and `ruff format --check .` clean.
- `pre-commit run --all-files` passes.
- `pytest` green; each pure module has ≥1 test file; `api.py` has ≥1 SSE integration test.
- `docs/CONTRIBUTING.md` and `docs/architecture.md` exist and are accurate.

## 4. Deliverable #2 — Decompose the giants

Purpose: behavior-preserving split of the three 1,000+ line files into focused sub-packages, protected by characterization tests.

### 4.1 `core/` layout (from `core.py`, 1,025 LOC)

| New file | Old line range | Contents |
|---|---|---|
| `core/__init__.py` | — | `from .orchestrator import main_async` |
| `core/orchestrator.py` | 739–1025 | `main_async` — parallel run driver, progress-tracker lifecycle |
| `core/runner.py` | 485–738 | `process_model`, `process_model_text` — per-model streaming |
| `core/auto_open.py` | 51–272 | `get_unique_filename`, viewer/terminal/tab logic |
| `core/auto_install.py` | 273–477 | dep detection, venv management |

`core/runner.py` ends with two near-parallel functions — that duplication is the explicit refactor target for Deliverable #3.

### 4.2 `tui/progress/` and `tui/analytics/` layout (from `tui/components.py`, 1,085 LOC)

| New file | Old line range | Contents |
|---|---|---|
| `tui/progress/__init__.py` | — | `from .tracker import ProgressTracker` |
| `tui/progress/wave.py` | 37–265 | `_title_wave`, `_render_pulse_bar`, `_render_pre_wave_bar`, `render_idle_wave` |
| `tui/progress/tracker.py` | 284–926 | `ProgressTracker` class (moves intact) |
| `tui/analytics/__init__.py` | — | `from .table import display_analytics` |
| `tui/analytics/table.py` | 927–1085 | `display_analytics` + sort/format helpers |
| `tui/analytics/cost.py` | 266–283 | `compute_cost` (imported by both progress and analytics) |

Dependency direction: `tui/progress` → `tui/analytics.cost` (one-way).

### 4.3 `tui/menus/` and promoted primitives (from `tui/interactive.py`, 1,195 LOC)

| New file | Old line range | Contents |
|---|---|---|
| `tui/input.py` | 81–215 | `_read_key`, `_read_key_or_resize`, `_read_key_timeout` — promoted (cross-cutting) |
| `tui/line_editor.py` | 216–438 | `_TabEscape`, `_redraw_input`, `_read_line` — promoted (cross-cutting) |
| `tui/menus/__init__.py` | — | re-exports: `run_model_selection`, `run_config_menu` |
| `tui/menus/_shared.py` | 27–80 | small helpers: `_format_price`, `_generate_short_name`, `_unique_short_name`, `_fit`, `_filter_model_indices`, `_is_printable_search_char` |
| `tui/menus/model_list.py` | 440–696 | `interactive_model_menu`, `run_model_selection` |
| `tui/menus/config_menu.py` | 697–1195 | `interactive_config_menu`, `run_config_menu` — moves intact at ~498 LOC; further splitting deferred |

### 4.4 Per-file decomposition workflow

Each of the three giants follows the same pattern, executed as three separate PRs in the order listed below:

1. **Characterization tests** — `tests/characterization/test_<file>_contract.py` asserting the file's external behavior via public-import boundary. Tests pass against unchanged code first.
2. **Mechanical split** — create the new package structure, move code blocks, add re-exports in `__init__.py`. No logic changes.
3. **Verify** — characterization tests pass; `ruff check` clean; manual smoke run (`wavebench --prompt "hello world"`).
4. **Cleanup pass within new files only** — consistent naming, short docstrings, remove dead imports. No behavioral changes.
5. **Retire or convert characterization tests** — coupled-to-internal ones get deleted; useful ones move to `tests/unit/` or `tests/integration/`.

**PR order (low-risk to high-risk):**

1. `core.py` decomposition — most testable, establishes the pattern.
2. `tui/components.py` decomposition — ProgressTracker is visually verifiable.
3. `tui/interactive.py` decomposition — most state, hardest to characterize automatically.

### 4.5 Import-compatibility guarantee

- `tests/unit/test_public_api.py` enumerates previously-public names (`main_async`, `ProgressTracker`, `display_analytics`, `run_model_selection`, `run_config_menu`, `get_unique_filename`, etc.) and asserts each is importable and callable.
- `__main__.py` is not edited in Deliverable #2 (only in #3).

### 4.6 Out of scope for Deliverable #2

- Further splitting of `interactive_config_menu` (~475 LOC function).
- API changes — all signatures and return types preserved.
- Type-annotation overhaul.
- `styles.py`, `api.py`, `models.py`, `parsers.py`, `storage.py` — untouched.

### 4.7 Completion criteria

- Three decomposition PRs merged.
- All characterization tests pass post-split.
- `tests/unit/test_public_api.py` passes.
- Manual smoke: `wavebench --prompt "hello world"` end-to-end works; analytics display works; config menu opens; model selector opens.
- No file in `wavebench/` exceeds 700 LOC except `tui/menus/config_menu.py` (documented exception).
- `ruff check` and `ruff format --check` clean.

## 5. Deliverable #3 — First-class response modes

Purpose: turn the Code/Text dichotomy into a pluggable `Mode` abstraction; collapse `core/runner.py`'s two sibling functions into one mode-parameterized function.

### 5.1 The `Mode` protocol

```python
# wavebench/modes/__init__.py
from typing import Protocol
from dataclasses import dataclass

@dataclass(frozen=True)
class ParsedOutput:
    content: str              # bytes to write to the output file
    extension: str            # file extension without the dot — e.g., "py", "md", "html"
    parse_ok: bool            # did parsing succeed (drives pass/fail on leaderboard)
    parse_error: str | None   # human-readable reason if parse_ok=False; else None

class Mode(Protocol):
    name: str                 # stable identifier — "code", "text", ...
    display_name: str         # for UI — "Code", "Text"

    def frame_prompt(self, user_prompt: str) -> list[dict]:
        """Turn the user's prompt into an OpenAI-format messages list."""
        ...

    def parse_response(self, raw: str) -> ParsedOutput:
        """Convert a raw streamed LLM response into a ParsedOutput."""
        ...

MODES: dict[str, Mode] = {}

def register(mode: Mode) -> None:
    MODES[mode.name] = mode

from .code import CODE_MODE
from .text import TEXT_MODE
register(CODE_MODE)
register(TEXT_MODE)
```

Protocol is chosen over ABC because modes are small value objects and we want structural typing without forcing inheritance.

### 5.2 Concrete implementations

- **`wavebench/modes/code.py`** — `CodeMode`: `frame_prompt()` holds current code-mode system prompt (extracted from `process_model`); `parse_response()` delegates to `parsers.py` extraction and wraps the result in `ParsedOutput`. Exports module-level `CODE_MODE = CodeMode()`.
- **`wavebench/modes/text.py`** — `TextMode`: `frame_prompt()` holds current text-mode framing; `parse_response()` is pass-through returning raw markdown with extension `"md"` and `parse_ok=True`. Exports `TEXT_MODE = TextMode()`.

`parsers.py` is not merged into `modes/code.py` — the mode *wraps* `parsers.py`, preserving its test surface.

### 5.3 Runner unification

Before (Deliverable #2 end state):

```python
# core/runner.py
async def process_model(...) -> ModelResult: ...        # code mode
async def process_model_text(...) -> ModelResult: ...   # text mode
```

After:

```python
# core/runner.py
async def run_model(
    mode: Mode,
    session: aiohttp.ClientSession,
    api_key: str,
    model_name: str,
    model_id: str,
    user_prompt: str,
    output_dir: str,
    tracker: ProgressTracker,
    ...
) -> ModelResult:
    messages = mode.frame_prompt(user_prompt)
    raw = await api.stream_completion(session, api_key, model_id, messages, on_chunk=tracker.update)
    parsed = mode.parse_response(raw)
    filename = make_output_filename(output_dir, model_name, parsed.extension)
    write_file(filename, parsed.content)
    return ModelResult(model_name=model_name, parse_ok=parsed.parse_ok, ...)
```

This deletes roughly 100 lines of duplication.

### 5.4 CLI integration

- **`__main__.py`** — add `--mode <name>` as the primary form (looks up `MODES[name]`); keep `--text` as an alias for `--mode text` (no deprecation).
- **Interactive mode-select screen** — read from `MODES.keys()` so new modes appear automatically.
- **`core/orchestrator.py`** — accepts a `Mode` instance in its call signature; CLI resolves `--mode name` → `Mode` before dispatch.

### 5.5 Tests

- **`tests/unit/test_modes.py`**:
  - `CodeMode.frame_prompt()` produces expected messages.
  - `CodeMode.parse_response()` covers the same branches as `test_parsers.py` (fenced, JSON, fallback) returning correct `ParsedOutput`.
  - `TextMode.parse_response()` is byte-for-byte pass-through; extension `"md"`.
  - `TextMode.frame_prompt()` produces text-appropriate framing.
  - `MODES` contains exactly `"code"` and `"text"` after importing `wavebench.modes`.
- **`tests/integration/test_orchestrator.py`** (updated) — `run_model(CODE_MODE, ...)` and `run_model(TEXT_MODE, ...)` against mocked streams produce expected file outputs.
- **`tests/characterization/test_mode_parity.py`** (temporary) — against frozen recorded streams, assert `process_model` (old) output bytes == `run_model(CODE_MODE, ...)` output bytes. Same for text. Deleted once integration tests supersede.

### 5.6 Contributor experience

`docs/CONTRIBUTING.md` "Adding a new mode" section walks through:

1. Create `wavebench/modes/<your_mode>.py`.
2. Define a class implementing `Mode`.
3. Export a singleton; import it in `modes/__init__.py`; call `register(YOUR_MODE)`.
4. Add unit tests in `tests/unit/test_modes.py`.
5. (Optional) Update `__main__.py` interactive mode-select display text.

### 5.7 Out of scope for Deliverable #3

- Evaluators (future brainstorm).
- Mode-specific TUI rendering.
- Dynamic mode discovery / entry points (static imports only).
- Deprecating `--text`.
- Structured-output modes (architecture supports them; none are built).

### 5.8 Completion criteria

- `wavebench/modes/` package exists with `__init__.py`, `code.py`, `text.py`.
- `Mode` protocol, `ParsedOutput`, `MODES` registry defined.
- `core/runner.py` has a single `run_model(mode, ...)`; `process_model` and `process_model_text` deleted.
- `core/orchestrator.py` passes a `Mode` into `run_model`.
- `wavebench --text`, `wavebench --mode text`, `wavebench --mode code` all produce output identical to pre-refactor (byte-diff against recorded reference).
- `tests/unit/test_modes.py` green, ≥10 tests.
- Mode-parity characterization tests pass, then are retired.
- `docs/CONTRIBUTING.md` has a complete "Adding a new mode" section.

## 6. Test strategy (cross-cutting)

Final test pyramid after all three deliverables:

```
                 ┌─────────────────────┐
                 │  characterization   │  temporary scaffolding,
                 │    (scaffolding)    │  deleted after #2 + #3 land
                 └─────────────────────┘
               ┌──────────────────────────┐
               │      integration         │  fake SSE, tmp state dir,
               │  (few, load-bearing)     │  canned recorded streams
               └──────────────────────────┘
         ┌────────────────────────────────────┐
         │              unit                  │  pure modules + modes,
         │  (many, fast, red-green feedback)  │  <1s total runtime target
         └────────────────────────────────────┘
```

### 6.1 Principles

1. **Test at contracts, not internals.** Public imports, public function signatures, public file outputs. Tests against private helpers become landfill during refactor.
2. **Deterministic inputs only.** No real LLM calls. Recorded fixtures for SSE. Synthetic inputs for parsers.
3. **No mocking of file-system shape.** `storage.py` tests hit real JSON on `tmp_path`.
4. **Async tests are first-class** via `asyncio_mode = "auto"`.
5. **Characterization tests are explicitly temporary.** Each carries a `# SCAFFOLDING — delete after <date>` marker.

### 6.2 Non-goals of the test suite

- Third-party library internals (`aiohttp` streaming correctness).
- Byte-for-byte TUI escape-sequence output.
- Real OpenRouter endpoint calls.
- Enforced coverage percentage gates.

## 7. Risks and mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Decomposition silently breaks behavior because TUI output is hard to assert on | High | Characterization tests assert state transitions (not ANSI bytes); manual smoke required for #2 completion |
| `ruff` auto-fix breaks subtle existing code during #1 | Medium | First `ruff format` commit is isolated and reviewed carefully; no mixing with other changes |
| `interactive_config_menu` (~475 LOC) is hard to move cleanly | Medium | Moved intact in #2; further decomposition is a separate future brainstorm |
| Mode refactor changes byte-for-byte output (system prompt wording, framing) | Medium | Parity characterization tests assert byte-identical outputs against recorded streams |
| Import-compatibility break | Medium | `tests/unit/test_public_api.py` enumerates every public name |
| Async test flakiness on CI | Low–medium | Pin `pytest-asyncio>=0.23`; avoid time-based assertions |
| Auto-open / auto-install hard to test (spawns terminals) | Low | Feature-flagged off in tests; characterization-lite only |
| Pre-commit hook friction | Low | Hook runs only `ruff` (ms-fast) |

### 7.1 Rollback

Each deliverable lands as a series of small PRs. Rollback is `git revert` of the specific PR. Because each PR preserves public imports and passes characterization tests, no downstream unwind is needed.

## 8. Dependencies between deliverables

```
#1 Foundation  ─────────────►  #2 Decompose giants  ─────────────►  #3 Modes
(usable alone: (needs #1 for characterization       (needs #2's core/runner.py
 tests+tools)  harness and ruff gate)                as the collapse target)
```

Each deliverable is independently shippable. Stopping after any is a valid outcome.

## 9. Consolidated non-goals

- New LLM providers beyond OpenRouter.
- Response modes beyond Code and Text (architecture supports, none built).
- Evaluators (C).
- CSV/JSON/HTML analytics renderers (D).
- Decomposing `interactive_config_menu` further.
- New TUI features (E).
- Logging framework migration.
- Type-checker adoption (mypy/pyright).
- Coverage percentage gates.
- Windows CI matrix.

## 10. Shipping unit

- 3 deliverables → 6–9 small PRs.
- Expected effort: 1–2 weeks of focused work.
- Each PR preserves public imports and leaves the tool fully functional.

## 11. Post-completion artifacts

- `docs/architecture.md` reflects post-#3 structure.
- `README.md` "Project Structure" replaced with pointer to `docs/architecture.md`.
- `docs/CONTRIBUTING.md` contains complete "Adding a new mode" guide.
- `tests/fixtures/reference_run/` preserves a pre-refactor reference run for future maintenance.
