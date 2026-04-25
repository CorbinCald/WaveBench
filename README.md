# WaveBench

A terminal-based tool for benchmarking Large Language Models side-by-side via the [OpenRouter](https://openrouter.ai/) API. Send one prompt to multiple models in parallel, compare their generated code or prose, and track lifetime performance analytics from your terminal.

## Prerequisites

- Python 3.10+
- An [OpenRouter API key](https://openrouter.ai/keys)

## Installation

```bash
git clone <repository-url>
cd WaveBench

python -m venv .venv
source .venv/bin/activate   # macOS / Linux
# .venv\Scripts\activate    # Windows

pip install .
```

The only runtime dependency is `aiohttp`.

## Configuration

Provide your OpenRouter API key via **environment variable** or a **`.env` file** in the project root:

```env
OPENROUTER_API_KEY=your_key_here
```

WaveBench stores model selection, user settings, analytics history, and prompt history in gitignored files in the current working directory. See [Persistent Files](#persistent-files).

## Quick Start

```bash
wavebench
# or
python -m wavebench
```

Interactive startup shows a **Code / Text** mode selector, a summary of active models, and a prompt input with readline-style history. Type `c` at the mode prompt to open the configuration menu.

### CLI Flags

| Flag | Description |
|---|---|
| `--prompt "…"` | Skip interactive input and run immediately |
| `--mode code\|text` | Select the response mode; defaults to `code` |
| `--text` | Alias for `--mode text` |
| `--config` / `--models` | Open the configuration menu and exit after saving/cancelling |
| `--open incremental\|after_all` / `--auto-open …` | Auto-open generated files as they complete or after all models finish |
| `--auto-install` | In code mode, permit PyPI dependencies and auto-install detected Python packages into a per-run venv before opening files |
| `--stats` | Display lifetime analytics and exit |
| `--clear-history` | Reset all analytics history |

Examples:

```bash
wavebench --prompt "Create a snake game in Python"
wavebench --prompt "Explain quantum computing" --mode text
wavebench --prompt "Explain quantum computing" --text
wavebench --config
wavebench --stats
```

## How It Works

1. **Prompt** — You enter a description of what you want built or answered.
2. **Mode framing** — The selected mode (`CodeMode` or `TextMode`) wraps the user prompt with mode-specific instructions.
3. **Directory naming** — The configured naming mode creates a short directory name from the prompt: either the LLM fallback chain or a local slug parser.
4. **Parallel execution** — All selected models are queried concurrently, up to `MAX_CONCURRENCY = 12`, with streaming responses and a live progress display.
5. **Parsing** — Code mode extracts a single savable artifact from JSON, fenced code blocks, malformed fences, or whole-response fallback. Text mode saves raw Markdown.
6. **Results** — Outputs are saved to `benchmarkResults/<prompt_dir>/`; a leaderboard shows pass/fail status, file names, token counts, timing, and estimated cost.
7. **Analytics** — Every run is recorded and lifetime stats show success rate, average time, token usage, and cost.

## Configuration Menu

Open the interactive config menu with `wavebench --config` or by pressing `c` at the startup mode prompt.

The menu has two tabs:

- **Models** — Search, browse, and toggle models from the OpenRouter catalog. Models are ranked by provider tier, pricing, recency, supported capabilities, and context length. Press `+` to manually add a model by its OpenRouter ID.
- **Settings** — Configure:
  - **Reasoning effort** — `max`, `xhigh`, `high`, `medium`, `low`, or `off`. Unsupported values are mapped per model where possible.
  - **Analytics sort** — `runs`, `avg_time`, `rate`, `avg_tokens`, or `cost`.
  - **Theme** — 9 color schemes: `default`, `plum`, `lemon`, `blueberry`, `grape`, `pear`, `acai`, `tangerine`, and `lime`, live-previewed while cycling.
  - **Directory naming** — `llm` for the fast OpenRouter fallback chain, or `slug` for a deterministic local parser.
  - **Auto-open files** — `off`, `incremental`, or `after_all`.
  - **Auto-install deps** — `off` or `on`; shown only when auto-open is enabled. Applies to Python code-mode outputs.

Selections persist across runs in local JSON files.

## Output

Results are saved to `benchmarkResults/<prompt_dir>/`:

```text
benchmarkResults/
└── snake_game/
    ├── prompt.txt              # The original prompt
    ├── gemini3_0Pro.html       # Code output from each model
    ├── claudeOpus4.6.py
    ├── kimik2_5.html
    └── ...
```

In text mode, outputs are saved as `.md` files.

## Project Structure

```text
wavebench/
├── __main__.py                 # CLI entry point, interactive startup, dispatch
├── api.py                      # OpenRouter API client: streaming, retries, model catalog
├── models.py                   # Default model mapping and catalog scoring
├── parsers.py                  # Code extraction and prompt-derived directory names
├── storage.py                  # JSON persistence for models/config/history
├── modes/                      # Response modes and registry
│   ├── __init__.py             # Mode protocol, ParsedOutput, MODES
│   ├── code.py                 # CodeMode prompt framing + parser wrapper
│   └── text.py                 # TextMode prompt framing + Markdown pass-through
├── core/                       # Benchmark orchestration and artifact handling
│   ├── __init__.py             # Public re-exports
│   ├── orchestrator.py         # main_async run coordinator
│   ├── runner.py               # per-model run_model and unique filenames
│   ├── auto_open.py            # viewer/terminal/tab launching
│   └── auto_install.py         # dependency detection and per-output-dir venvs
└── tui/
    ├── styles.py               # Themes, ANSI helpers, box drawing, formatting
    ├── input.py                # Raw keyboard reads
    ├── line_editor.py          # Readline-style prompt editor
    ├── progress/               # Live progress tracker and wave rendering
    ├── analytics/              # Cost helper and lifetime stats table
    └── menus/                  # Model browser and tabbed config menu
```

A more detailed architectural map — including data flow, public seams, and testing tiers — lives in [`docs/architecture.md`](docs/architecture.md).

## Persistent Files

These are created in the current working directory and are gitignored:

| File | Contents |
|---|---|
| `.benchmark_models.json` | Currently selected `{short_name: openrouter_id}` model mapping |
| `.benchmark_config.json` | Settings such as theme, reasoning effort, analytics sort, directory naming, auto-open, and auto-install |
| `.benchmark_history.json` | Lifetime run history for analytics |
| `.benchmark_query_history` | Readline-style prompt history |

Because state paths are based on `os.getcwd()`, running WaveBench from different directories creates separate project-local state.

## Development

Local setup, test commands, style conventions, and contribution guidelines live in [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md). Quick start for contributors:

```bash
pip install -e '.[dev]'
pre-commit install
pytest
```
