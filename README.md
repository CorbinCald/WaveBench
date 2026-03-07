# LLM Benchmark Generator

A terminal-based tool for benchmarking Large Language Models side-by-side via the [OpenRouter](https://openrouter.ai/) API. Send a single prompt to multiple models in parallel, compare their generated code or prose, and track lifetime performance analytics — all from an interactive TUI.

## Prerequisites

- Python 3.8+
- An [OpenRouter API key](https://openrouter.ai/keys)

## Installation

```bash
git clone <repository-url>
cd LLM-Benchmarks

python -m venv .venv
source .venv/bin/activate   # macOS / Linux
# .venv\Scripts\activate    # Windows

pip install .
```

The only runtime dependency is `aiohttp`.

## Configuration

Provide your OpenRouter API key via **environment variable** or a **`.env` file** in the project root:

```
OPENROUTER_API_KEY=your_key_here
```

## Quick Start

```bash
llm-benchmarks
# or
python -m llm_benchmarks
# or
./run_benchmarks.sh
```

You'll be greeted with an interactive mode selector (**Code** or **Text**), a summary of active models, and a prompt input with readline history support. Type `c` at the mode prompt to open the configuration menu.

### CLI Flags

| Flag | Description |
|---|---|
| `--prompt "…"` | Skip interactive input and run immediately |
| `--text` | Text mode — get prose/Markdown answers instead of code |
| `--config` / `--models` | Open the configuration menu (model selection & settings) |
| `--stats` | Display lifetime analytics and exit |
| `--clear-history` | Reset all analytics history |

Examples:

```bash
llm-benchmarks --prompt "Create a snake game in Python"
llm-benchmarks --prompt "Explain quantum computing" --text
llm-benchmarks --config
llm-benchmarks --stats
```

## How It Works

1. **Prompt** — You enter a description of what you want built (or answered).
2. **Directory naming** — A fast model generates a short `snake_case` directory name from your prompt.
3. **Parallel execution** — All selected models are queried concurrently (up to 12 at a time) with streaming responses and a live progress display.
4. **Parsing** — In code mode, responses are parsed to extract the code block and detect the file extension. In text mode, raw Markdown is saved directly.
5. **Results** — A ranked leaderboard shows pass/fail status, file names, token counts, and timing for each model.
6. **Analytics** — Every run is recorded and lifetime stats (success rate, average time, token usage) are displayed.

## Configuration Menu

The interactive config menu (`--config` or `c` at the mode prompt) has two tabs:

- **Models** — Search, browse, and toggle models from the OpenRouter catalog. Models are scored and ranked by provider tier, pricing, recency, and capabilities.
- **Settings** — Configure `reasoning_effort` (high / medium / low / off) and other options like `auto_use_venv`.

Selections persist across runs in local JSON files.

## Output

Results are saved to `benchmarkResults/<prompt_dir>/`:

```
benchmarkResults/
└── snake_game/
    ├── prompt.txt              # The original prompt
    ├── gemini3_0Pro.html       # Code output from each model
    ├── claudeOpus4.6.py
    ├── kimik2_5.html
    └── ...
```

In text mode, outputs are saved as `.md` files instead.

## Project Structure

```
llm_benchmarks/
├── __init__.py
├── __main__.py        # CLI entry point & argument parsing
├── api.py             # OpenRouter API client (streaming & non-streaming)
├── core.py            # Benchmark orchestration & result display
├── models.py          # Default model mapping & scoring algorithm
├── parsers.py         # LLM output parsing & directory name generation
├── storage.py         # JSON persistence (models, config, history)
└── tui/
    ├── __init__.py
    ├── components.py  # ProgressTracker & analytics display
    ├── interactive.py # Model selection & settings menus
    └── styles.py      # ANSI styles, box drawing, formatting helpers
```

### Key Modules

| Module | Purpose |
|---|---|
| `__main__.py` | Parses CLI args, loads state, handles interactive mode/prompt selection, dispatches to `core.main_async()` |
| `api.py` | Manages OpenRouter calls — API key loading, SSE streaming with progress callbacks, model catalog fetching, retry logic |
| `core.py` | Runs all models concurrently, manages the progress tracker, writes output files, records analytics |
| `models.py` | Defines `MODEL_MAPPING` (the default set of models) and `_model_score()` for ranking catalog models |
| `parsers.py` | Extracts code from LLM responses (JSON, fenced blocks, fallback), detects language/extension, generates directory names |
| `storage.py` | Reads/writes `.benchmark_models.json`, `.benchmark_config.json`, and `.benchmark_history.json` |
| `tui/components.py` | Animated multi-line `ProgressTracker` with token bars and per-model status; `display_analytics()` for stats tables |
| `tui/interactive.py` | Full-screen model selector with search, pagination, and toggling; tabbed config menu |
| `tui/styles.py` | ANSI color definitions, box-drawing primitives, duration formatting, ANSI-aware string utilities |

### Persistent Files

These are created in the project root and are gitignored:

| File | Contents |
|---|---|
| `.benchmark_models.json` | Currently selected models |
| `.benchmark_config.json` | Settings (reasoning effort, etc.) |
| `.benchmark_history.json` | Lifetime run history for analytics |
| `.benchmark_query_history` | Readline prompt history |
