# WaveBench

A terminal tool for comparing large language models side by side through the [OpenRouter](https://openrouter.ai/) API. Send one prompt to multiple models, compare their generated projects, prose, speech, or images, and track performance and cost from your terminal. Runtime checks measure whether a project starts or runs successfully; you judge its quality.

[![CI](https://github.com/CorbinCald/WaveBench/actions/workflows/ci.yml/badge.svg)](https://github.com/CorbinCald/WaveBench/actions/workflows/ci.yml)

## Prerequisites

- Python 3.10+
- Git and [pipx](https://pipx.pypa.io/stable/how-to/install-pipx.html)
- An [OpenRouter API key](https://openrouter.ai/keys)

## Installation

Install once with pipx, then launch with `wavebench`. Pipx manages a private
Python environment and all dependencies automatically; no environment activation
is needed, and your system Python stays unchanged.

```bash
pipx install git+https://github.com/CorbinCald/WaveBench.git
```

If pipx is new to your machine, run `pipx ensurepath` once and open a new terminal
so the `wavebench` command is available. On Ubuntu, install pipx with
`sudo apt install pipx`; on macOS, use `brew install pipx`. Other platforms are
covered in the [pipx installation guide](https://pipx.pypa.io/stable/how-to/install-pipx.html).

Update with `pipx upgrade wavebench`, or remove it with `pipx uninstall wavebench`.

For an existing checkout that should pick up source changes immediately:

```bash
cd WaveBench
pipx install --editable .
wavebench
```

After dependency changes, run `pipx reinstall wavebench` to refresh that
installation. Contributors who need test/lint tools can use the separate
[development setup](docs/CONTRIBUTING.md#local-setup).

Runtime dependencies are `aiohttp` and `tiktoken` (for context-size estimates). The tokenizer downloads its public vocabulary on first use and caches it locally. WaveBench plays TTS outputs natively through the OS audio backend without launching external apps.

## Configuration

Provide your OpenRouter API key via **environment variable** or a **`.env` file** in the directory where you launch WaveBench:

```env
OPENROUTER_API_KEY=your_key_here
```

In a checkout, copy `.env.example` to `.env` and fill in your key. Keep real keys
and private prompts out of commits and public issue reports.

WaveBench stores model selection, user settings, analytics history, prompt history,
and generated projects in the current working directory. Launch from your existing
WaveBench directory to keep using its `.env`, settings, and history; new users can
choose any working directory. See [Persistent Files](#persistent-files).

## Quick Start

```bash
wavebench
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
| `--image-aspect-ratio RATIO` | Set the image aspect ratio, such as `1:1` or `16:9`; enables custom image settings |
| `--image-size 1K\|2K\|4K` | Set the requested image size; enables custom image settings |
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
wavebench --prompt "Read this aloud in a calm tone" --mode tts
wavebench --prompt "A watercolor ocean wave" --mode image --image-aspect-ratio 16:9
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

Harness uses explicit prompt caching for Anthropic, GPT-5.6 and later, and Gemini 2.5 and later, with stable routing for other models. Above 240,000 context tokens (earlier for smaller model windows), GPT-5.6 Luna at High effort summarizes older history while preserving the first user message and latest complete assistant response with its tool results. See [cache and compaction behavior](docs/harness.md#prompt-caching-and-context-compaction), including budget accounting.

## Configuration Menu

Open the interactive config menu with `wavebench --config` or by pressing `c` at the startup mode prompt.

The menu has four tabs:

- **Models** — Search, browse, and toggle non-TTS models from the OpenRouter catalog. Models are ranked by provider tier, pricing, recency, supported capabilities, and context length. Press `+` to manually add a model by its OpenRouter ID.
- **TTS** — Search, browse, and toggle speech-output models separately from the main model list. If no TTS models are selected, TTS mode falls back to the bundled OpenRouter TTS defaults.
- **Image** — Select image-output models separately from text and speech models. If none are selected, image mode uses its bundled defaults.
- **Settings** — Configure:
  - **Reasoning effort** — `max`, `xhigh`, `high`, `medium`, `low`, or `off`. Unsupported values are mapped per model where possible.
  - **Analytics sort** — `runs`, `avg_time`, `rate`, `avg_tokens`, or `cost`.
  - **Theme** — 9 color schemes: `default`, `plum`, `lemon`, `blueberry`, `grape`, `pear`, `acai`, `tangerine`, and `lime`, live-previewed while cycling.
  - **Directory naming** — `llm` for the fast OpenRouter fallback chain, or `slug` for a deterministic local parser.
  - **Auto-open files** — `off`, `incremental`, or `after_all`.
  - **Auto-install deps** — `off` or `on`; always visible, including when Auto-open is off. Applies to harness `requirements.txt` manifests.
  - **Harness limits** — Preview review timeout and separate build/repair time and token budgets. See [Harness limits](docs/harness.md#budgets-and-records).
  - **TTS voice / format / speed** — default voice, audio format, and playback speed for TTS mode. Voice identifiers are provider-specific.
  - **Image settings** — Provider defaults or custom aspect ratio and image size.

Selections persist across runs in local JSON files.

Model availability changes over time. WaveBench warns when selected IDs are
missing from a successfully fetched catalog. Use `wavebench --config` to replace
retired selections. A warning keeps your selection intact because private model
IDs may still work. If the catalog cannot be fetched, the app uses your saved or
bundled selection without claiming that those IDs are unavailable.

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
        ├── browser.log          # Browser output when opening a preview
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
├── query_history.py            # Portable prompt history and legacy import
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
| `.benchmark_query_history.<mode>.json` | Portable prompt history (last 500 entries per mode); Harness uses `code`, alongside `text`, `tts`, and `image` |

Existing `.benchmark_query_history.<mode>` files from GNU Readline or libedit are imported automatically, including escaped spaces and Unicode. Harness also supports the older `.benchmark_query_history` fallback. The next submitted prompt saves that mode's history as JSON; original files are kept. History works across Python installations without requiring either readline backend.

Because state paths are based on `os.getcwd()`, running WaveBench from different directories creates separate project-local state. TTS mode automatically uses selected TTS-capable models, falling back to the bundled TTS defaults when none are selected.

## Development

Local setup, test commands, style conventions, and contribution guidelines live in [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md). Quick start for contributors:

```bash
pip install -e '.[dev]'
pre-commit install
pytest
```

## License

Licensed under the [MIT License](LICENSE).
