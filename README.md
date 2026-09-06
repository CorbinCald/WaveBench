# WaveBench

A terminal-based tool for benchmarking Large Language Models side-by-side via the [OpenRouter](https://openrouter.ai/) API. Send one prompt to multiple models in parallel, compare their generated code, prose, or TTS audio, and track lifetime performance analytics from your terminal.

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

The only required runtime dependency is `aiohttp`. WaveBench plays TTS outputs natively through the OS audio backend without launching external apps.

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

Interactive startup shows a **Harness / Text / TTS / Image** mode selector, a summary of active models, and a prompt input with mode-specific history. Harness replaces one-shot code generation with isolated, multi-file projects. Type `c` at the mode prompt to open the configuration menu.

Harness requires **Linux, Bubblewrap, `/usr/bin/python3`, and `/usr/bin/node`**. Install `bubblewrap`, `python3`, and `nodejs` with your distribution's package manager. Auto-install also requires system `python3-pip`. The host must permit unprivileged user namespaces. A failed sandbox preflight is reported before model generation; there is no unsandboxed fallback. Text, TTS, and image modes keep their existing platform support.

[Watch a preview of the animated progress display.](docs/wave-animation.md)

### CLI Flags

| Flag | Description |
|---|---|
| `--prompt "…"` | Skip interactive input and run immediately |
| `--mode harness\|text\|tts\|image` | Select the response mode; defaults to `harness`. `--mode code` remains a compatibility alias |
| `--text` | Alias for `--mode text` |
| `--tts-voice VOICE` | Voice for TTS mode; defaults to `alloy` for OpenAI models; known non-OpenAI TTS models use provider voices automatically when the default is selected (for example Gemini `Kore`, Zonos `american_female`, Voxtral `en_paul_neutral`) |
| `--tts-format mp3\|pcm` | Preferred audio format for TTS mode; defaults to `mp3` and is adjusted for providers such as Gemini that require `pcm` |
| `--tts-speed FLOAT` | TTS playback speed multiplier for providers that support it |
| `--config` / `--models` | Open the configuration menu and exit after saving/cancelling |
| `--open off\|incremental\|after_all` / `--auto-open …` | Schedule harness validation and present managed previews. `off` still validates, headlessly; `after_all` waits for initial generation. New configurations default to `incremental` |
| `--auto-install` | Install `requirements.txt` PyPI wheels in each model's isolated dependency directory; generated package scripts/build hooks are never installed or run |
| `--stats` | Display lifetime analytics and exit |
| `--clear-history` | Reset all analytics history |

Examples:

```bash
wavebench --prompt "Create a multi-file Python CSV summary program"
wavebench --mode harness --auto-open off --prompt "Build a static counter website with HTML, CSS and JavaScript"
wavebench --prompt "Explain quantum computing" --mode text
wavebench --prompt "Explain quantum computing" --text
wavebench --prompt "Read this aloud in a calm tone" --mode tts --tts-voice nova
wavebench --config
wavebench --stats
```

## How It Works

1. **Prompt** — You enter a description of what you want built or answered.
2. **Build** — Harness allocates a fresh project per model. Models use the same `wb` file and lint tools over an OpenRouter conversation, then submit a runtime and entry point with `done`.
3. **Schedule** — `incremental` validates submitted projects immediately. `after_all` waits until every model has submitted or reached a terminal generation outcome. `off` validates immediately without opening previews. Waiting projects release API slots.
4. **Validate** — WaveBench admits one sandboxed project run. Exit code 0 passes console programs; an HTTP readiness check passes web/server startup. These checks measure runtime/startup, not subjective project quality.
5. **Repair** — Only a failed first run gives the same model/conversation one bounded repair phase, then one final run. Lint never consumes a run. Cancellation never unlocks a retry.
6. **Inspect** — Successful web previews attach to the already running process. Enter, Ctrl-C, or the review deadline stops it and its children. Projects, diagnostics, and attempts survive failures and cancellation.
7. **Results** — History includes all build/repair usage and cost, generation and runtime outcomes, workspace/entry point, lint results, configuration, and separate generation, tool, queue, setup, runtime, and repair times. Missing usage remains unknown. Harness analytics are labeled separately from historical one-shot records.

Text mode still saves Markdown, TTS saves audio and provides native playback, and image mode saves images and its gallery. See [Harness commands, runtimes, limits, and verification](docs/harness.md).

## Configuration Menu

Open the interactive config menu with `wavebench --config` or by pressing `c` at the startup mode prompt.

The menu has three tabs:

- **Models** — Search, browse, and toggle non-TTS models from the OpenRouter catalog. Models are ranked by provider tier, pricing, recency, supported capabilities, and context length. Press `+` to manually add a model by its OpenRouter ID.
- **TTS** — Search, browse, and toggle speech-output models separately from the main model list. If no TTS models are selected, TTS mode falls back to the bundled OpenRouter TTS defaults.
- **Settings** — Configure:
  - **Reasoning effort** — `max`, `xhigh`, `high`, `medium`, `low`, or `off`. Unsupported values are mapped per model where possible.
  - **Analytics sort** — `runs`, `avg_time`, `rate`, `avg_tokens`, or `cost`.
  - **Theme** — 9 color schemes: `default`, `plum`, `lemon`, `blueberry`, `grape`, `pear`, `acai`, `tangerine`, and `lime`, live-previewed while cycling.
  - **Directory naming** — `llm` for the fast OpenRouter fallback chain, or `slug` for a deterministic local parser.
  - **Auto-open files** — `off`, `incremental`, or `after_all`.
  - **Auto-install deps** — `off` or `on`; always visible, including when Auto-open is off. Applies to harness `requirements.txt` manifests.
  - **TTS voice / format / speed** — default voice, audio format, and playback speed for TTS mode. Voice identifiers are provider-specific.

Selections persist across runs in local JSON files.

## Output

Harness results use a modality folder, an exclusive invocation directory, and independent model slots:

```text
benchmarkResults/
└── harness/<prompt>/<run-id>/
    ├── prompt.txt
    ├── 001-model-a-<id>/project/
    │   ├── main.py
    │   └── helpers.py
    ├── 002-model-b-<id>/project/
    │   ├── index.html
    │   ├── styles.css
    │   └── app.js
    └── metadata/<model-slot>/   # Controller-owned, outside model roots
        ├── result.json
        ├── conversation.json
        ├── tool-0001.json
        └── run-1-<id>.log
```

In text mode, outputs are saved as `.md` files. In TTS mode, outputs are saved as provider-compatible audio files (`.mp3` by default for OpenAI/Voxtral/Zonos and most speech models, `.pcm` for Gemini TTS), then an interactive arrow-key browser lets you move between outputs with ↑/↓ or ←/→ and press Enter/Space to play one through WaveBench's native audio backend.

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
│   ├── harness.py              # Harness mode exports (code is a compatibility alias)
│   ├── text.py                 # TextMode prompt framing + Markdown pass-through
│   └── tts.py                  # TTSMode prompt framing + audio-byte pass-through
├── harness/                    # Bounded projects, tools, conversations, managed execution
│   ├── workspace.py            # Root-bound file operations and exclusive allocation
│   ├── commands.py             # Shared wb CLI/model dispatcher
│   ├── transport.py            # OpenRouter streamed conversations/tool arguments
│   ├── session.py              # Budgets, scheduling, attempts, repair, results
│   ├── runtime.py              # Sandbox, dependencies, supervision, preview proxy
│   └── trusted.py              # Read-only sandbox checks and launch helper
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
    ├── tts_player.py           # Arrow-key TTS output browser/player
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
| `.benchmark_config.json` | Settings such as theme, reasoning effort, analytics sort, directory naming, auto-open, auto-install, and TTS voice/format/speed |
| `.benchmark_history.json` | Lifetime run history for analytics |
| `.benchmark_query_history.<mode>` | Mode-specific readline-style prompt history for `code`, `text`, `tts`, and `image` prompts |

If a legacy `.benchmark_query_history` file exists, Code mode reads it until `.benchmark_query_history.code` is created on the first new Code prompt.

Because state paths are based on `os.getcwd()`, running WaveBench from different directories creates separate project-local state. TTS mode automatically uses selected TTS-capable models, falling back to the bundled TTS defaults when none are selected.

## Development

Local setup, test commands, style conventions, and contribution guidelines live in [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md). Quick start for contributors:

```bash
pip install -e '.[dev]'
pre-commit install
pytest
```
