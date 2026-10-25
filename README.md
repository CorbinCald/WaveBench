# WaveBench

Compare large language models from your terminal through [OpenRouter](https://openrouter.ai/). Send one prompt to multiple models, watch them work side by side, inspect their projects, prose, speech, or images, and compare time, token usage, and cost.

[![CI](https://github.com/CorbinCald/WaveBench/actions/workflows/ci.yml/badge.svg)](https://github.com/CorbinCald/WaveBench/actions/workflows/ci.yml)

WaveBench is an interactive comparison tool. Harness checks whether generated projects run or start successfully; you judge their correctness, design, and usefulness. It does not produce an automatic quality score or a model leaderboard.

## What you can compare

| Mode | What it does | Output |
|---|---|---|
| **Harness** (default) | Models build multi-file projects with file tools, lint, sandboxed execution, and one repair after a failed run | Independent project directories, diagnostics, and managed web previews |
| **Text** | Sends the same prompt to your selected text models | Markdown files |
| **TTS** | Synthesizes your text with speech models | Audio files and an interactive player |
| **Image** | Generates images with optional aspect ratio and size settings | Image files and a gallery |

Harness also supports optional Brave web research, parallel subagents, separate orchestrator and worker models, and reasoning effort chosen per subagent. Live progress and lifetime analytics include their usage. Reasoning settings follow model capabilities from the live catalog, and optional **Fast providers** routing favors tokens per second.

[Watch the animated progress display.](docs/wave-animation.md)

## Requirements

- **All modes:** Python 3.10+, Git for installation from this repository, and an [OpenRouter API key](https://openrouter.ai/keys) with access to your chosen models.
- **Harness:** Linux, Bubblewrap, `/usr/bin/python3`, `/usr/bin/node`, and working unprivileged user namespaces. Auto-install additionally needs system `python3-pip`. See the [sandbox setup](docs/harness.md#isolation-and-dependency-policy), including Ubuntu's AppArmor requirements.
- **Text, TTS, and Image:** No Bubblewrap requirement. Audio playback and automatic file opening depend on the host's audio/desktop support. CI exercises Linux with Python 3.10, 3.12, and 3.14.

Harness has no unsandboxed fallback. On a system without its runtime, choose another mode, for example `wavebench --mode text`.

## Install and start

Install [pipx](https://pipx.pypa.io/stable/how-to/install-pipx.html), then install WaveBench in its own Python environment. Current pipx releases require Python 3.11+; for Python 3.10, use the [virtual-environment setup](docs/CONTRIBUTING.md#local-setup).

```bash
pipx install git+https://github.com/CorbinCald/WaveBench.git
pipx ensurepath
```

Open a new terminal if `wavebench` is not yet on your PATH. On Ubuntu, pipx is available through `sudo apt install pipx`; on macOS, through `brew install pipx`.

Choose a working directory for your settings, history, and results. Set `OPENROUTER_API_KEY` in your environment, or put this line in a `.env` file in that directory:

```env
OPENROUTER_API_KEY=your_key_here
```

In a checkout, you can copy `.env.example` to `.env`. Keep real keys and private prompts out of commits and public issue reports.

```bash
wavebench --config
wavebench
```

Choose the models you want before your first run: a fresh installation otherwise uses its bundled selection and may call several paid models. The startup menu offers **Harness / Text / TTS / Image**, shows the active selection, and keeps separate prompt history for each mode. Press **c** for configuration, **s** for subagents, or **w** for web search.

Examples:

```bash
wavebench --prompt "Create a multi-file Python CSV summary program"
wavebench --mode harness --open off --prompt "Build a static counter website with HTML, CSS and JavaScript"
wavebench --mode text --prompt "Explain quantum computing"
wavebench --mode tts --prompt "Read this aloud in a calm tone"
wavebench --mode image --image-aspect-ratio 16:9 --prompt "A watercolor ocean wave"
wavebench --stats
```

### Updating

For an installation from Git, use `pipx reinstall wavebench` to fetch the current source and refresh dependencies, including when the package version has not changed. Remove the installation with `pipx uninstall wavebench`. Settings and generated results stay in your working directory.

For an existing checkout, `pipx install --editable .` makes source edits immediately available. Pull changes in that checkout and run `pipx reinstall wavebench` when dependencies change. Contributors should use the [development setup](docs/CONTRIBUTING.md#local-setup).

## Harness workflow

1. **Build:** Each selected model gets a fresh project, file tools, lint, and the same configured request/time limits. Optional research and delegation tools are shared across the comparison.
2. **Submit and validate:** The model submits an entry point. Console programs pass on exit code 0; static sites and HTTP servers pass a readiness check. These checks do not test application interactions or grade quality.
3. **Repair:** A failed initial execution gives that model one bounded repair phase and one final execution. Failed generation, dependency setup, and cancellation do not unlock extra runs.
4. **Inspect:** Successful web projects can open in managed previews labeled with the model, prompt, run, and attempt. Enter, Ctrl-C, or the review deadline stops managed processes. Projects and diagnostics remain on disk.
5. **Compare:** Results separate generation and runtime outcomes, record retries and failures, and track time, usage, cache hits, tool activity, and reported cost. Unknown usage stays unknown.

**Auto-open** controls scheduling: `incremental` validates projects as they arrive; `after_all` waits for initial generation to finish; `off` validates headlessly without presenting previews. A new configuration uses `incremental` with dependency installation off.

Supported runners are Python, Node.js, Python/Node HTTP servers, and static HTML. There is no arbitrary shell tool, npm installation, package-script execution, desktop GUI runner, or development reloader. With Auto-install on, `requirements.txt` may install PyPI wheels into isolated per-model directories. Generated programs have no network access. See [runners and isolation](docs/harness.md#supported-project-runners).

For remote/headless use, `--open off` needs no desktop integration. Automatic preview forwarding to a connected laptop requires the optional Herdr companion; ordinary SSH alone does not provide it. Local desktop previews can use **Settings → Preview destination → Wavebench host**. See [preview destinations](docs/harness.md#preview-destination).

### Subagents and model roles

Press **s** at startup or open **Settings → Subagents (Harness)**. Subagents are off by default. When enabled, the default is four concurrent workers and eight total per benchmark model; concurrency can be set from two to five.

- **Orchestrator LLM:** Defaults to each selected benchmark model. An explicit model replaces that selection with one orchestrator run.
- **Subagent LLM:** Defaults to the orchestrator's model. Choose another model to compare a lead/worker combination.
- **Reasoning effort:** The orchestrator may choose `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max` for each spawned worker. Omitted effort inherits the lead's setting; unsupported levels are mapped to supported ones.

Workers share the lead's workspace and phase time, receive a fresh task context, and return bounded reports. Only the lead submits the project; workers cannot spawn more agents. Their calls and costs count toward the orchestrator's result, so a mixed-model result measures the whole team.

```bash
wavebench --agent-cap 4 --prompt "Build a three-page static site; delegate each page to a subagent"
# Replace these placeholders with available OpenRouter model IDs:
wavebench --subagents --orchestrator-model provider/lead --subagent-model provider/worker --prompt "Build a small app"
```

Model-role choices apply only when subagents are enabled. See [subagent behavior and limits](docs/harness.md#subagents).

Workers get 40 working requests by default, plus one final handoff request within
their ten-minute time limit. Invalid handoffs get one correction attempt.
File-content search and remaining-budget reminders help them finish within that
allowance. Workers explicitly report complete,
incomplete, or blocked work; the lead receives remaining work and tool errors.
The terminal and saved results preserve those outcomes even if the lead later
repairs the project. “Reported complete” is a worker's claim, not a quality score.

### Optional web research

Run `wavebench --setup-web-search` or press **w** at startup to configure [Brave Search](https://api-dashboard.search.brave.com/app/keys). Setup accepts a masked key, tests it with one search, and enables Harness `web_search` and `web_fetch`. It works without an OpenRouter key.

Setup saves the key in owner-only `.benchmark_secrets.json`. Alternatively, export `BRAVE_SEARCH_API_KEY` in your shell; the environment takes precedence and is not copied to disk. Disabling search keeps the saved key. `--web-search` and `--no-web-search` override the setting for one run.

Search returns source links; page reads extract public HTML/text without logging in or running JavaScript. Research calls have quotas, and research closes as the build approaches its limits. These tools apply to Harness only. See [search, page-reading, and data handling](docs/harness.md#optional-web-search).

## Configuration

`wavebench --config` has **Models**, **TTS**, **Image**, and **Settings** tabs. Search and toggle models from OpenRouter's catalog, or press **+** to add an ID manually. TTS and Image fall back to bundled defaults when their selection is empty. Missing catalog IDs produce a warning without deleting saved selections; private IDs may still work.

| Setting | Behavior |
|---|---|
| Reasoning effort | `max`, `xhigh`, `high`, `medium`, `low`, or `off`; validated against live model capabilities, with compatibility fallbacks |
| Fast providers (tokens/sec) | Off by default; favors high-throughput providers for Harness, Text, and Image. May change price; does not guarantee lowest total latency |
| Analytics sort | Runs, time, rate, tokens, cost, and Harness efficiency/reliability metrics |
| Theme | Nine color schemes with live previews |
| Directory naming | `llm` (default) uses a separate model request; `slug` derives names locally |
| Auto-open / Preview destination | When projects validate and where their web previews open |
| Web search / Subagents | Harness research, model roles, and delegation limits |
| Auto-install deps | Off by default; isolated PyPI wheels, including during headless validation |
| Harness limits | Build/repair time, build requests, output tokens per request, and preview review timeout |
| TTS voice / format / speed | Provider-specific voices; MP3 or PCM, adjusted where required |
| Image settings | Provider defaults, or custom aspect ratio and size |

Gemini tool conversations stay with their initial provider to preserve signed history. Prompt caching and context compaction are handled automatically: older context is summarized above 240,000 estimated tokens, or earlier for smaller windows, while preserving the original request and recent complete tool exchange. Compaction currently uses GPT-5.6 Luna at High effort. See [cache and compaction details](docs/harness.md#prompt-caching-and-context-compaction).

### CLI reference

Use `wavebench --help` for the full current help.

| Flag | Description |
|---|---|
| `--prompt "…"` | Run with a supplied prompt |
| `--mode harness\|text\|tts\|image` | Choose a mode; default is Harness. `--mode code` remains a compatibility alias |
| `--text` | Alias for `--mode text` |
| `--config` / `--models` | Open configuration, then exit |
| `--open off\|incremental\|after_all` / `--auto-open …` | Set validation/presentation timing; `off` still validates Harness projects |
| `--auto-install` | Enable isolated Harness dependency installation |
| `--setup-web-search` | Configure and test Brave search, then exit |
| `--web-search` / `--no-web-search` | Override Harness research for this run |
| `--subagents` / `--no-subagents` | Override Harness delegation for this run |
| `--agent-cap N` | Set total workers per model; implies `--subagents` unless `--no-subagents` is given |
| `--orchestrator-model ID` | Override the lead model when subagents are enabled |
| `--subagent-model ID` | Override the worker model when subagents are enabled |
| `--tts-voice VOICE` | Choose a provider voice; the default maps automatically for known models |
| `--tts-format mp3\|pcm` | Preferred audio format; adjusted for provider requirements |
| `--tts-speed FLOAT` | Speech speed for providers that support it |
| `--image-aspect-ratio RATIO` | Request an aspect ratio such as `1:1` or `16:9` |
| `--image-size 1K\|2K\|4K` | Request an image size |
| `--stats` | Show lifetime analytics; no API key required |
| `--clear-history` | Reset analytics, keeping the previous history as `.bak`; no API key required |

## Costs and data

Benchmark requests spend API credits. Harness defaults allow **50 build requests, 20 repair requests, 30 minutes of active build time, and up to 128,000 output tokens per request**, subject to provider limits. Workers have their own request caps within the lead's phase time. These are workload limits, **not a dollar spending cap**. Start with a small model selection and review [all limits](docs/harness.md#limits-and-records).

Harness totals include reported build, repair, subagent, and compaction usage. Brave charges and LLM directory-naming requests are separate from the displayed benchmark totals. Use **Directory naming → slug** to avoid the naming request. Provider routing, cache state, reasoning settings, delegation, and retries affect comparisons; saved results record those details.

Prompts, generated conversations, and tool results are sent to OpenRouter and the providers serving those requests, including the compaction model. Optional searches go to Brave, and page reads contact source websites. Runtime dependencies are `aiohttp` and `tiktoken`; the tokenizer downloads and caches its public vocabulary on first use.

Generated web pages run JavaScript in the browser you use to inspect them. The Linux process sandbox does not isolate that browser or its network access. Treat generated apps as untrusted and avoid entering credentials. See [Security](SECURITY.md) for the reporting policy.

## Output and persistent files

All state belongs to the **directory where you launch WaveBench**. Launch from the same directory to reuse its `.env`, model selection, settings, and history.

Harness allocates a separate run and project per model:

```text
benchmarkResults/harness/<prompt>/<run-id>/
├── prompt.txt
├── 001-model-a-<id>/project/
├── 002-model-b-<id>/project/
└── metadata/<model-slot>/
    ├── result.json
    ├── conversation.json
    ├── tool-0001.json
    ├── run-1-<id>.log
    └── subagents/<number>-<name>/
```

Text outputs are Markdown. TTS saves provider-compatible audio and offers arrow-key selection with Enter/Space playback. Image mode saves images and its gallery. Generated output goes under `benchmarkResults/`.

### Persistent files

| File | Contents |
|---|---|
| `.benchmark_models.json` | Selected names and OpenRouter model IDs |
| `.benchmark_config.json` | Settings, model roles, modality options, and Harness limits |
| `.benchmark_secrets.json` | Brave key from guided setup; never sent to models |
| `.benchmark_history.json` | Lifetime run results, including prompts and costs |
| `.benchmark_query_history.<mode>.json` | Last 500 prompts per mode; Harness uses the legacy `code` category |

Legacy Readline/libedit prompt histories are imported without deleting the originals. This repository ignores local state, `.env` files, generated projects, browser-test artifacts, agent instructions/skills, local workflow configuration, and development session records. If you run WaveBench inside another repository, add equivalent ignore rules there. Review saved prompts, conversations, and logs before sharing them.

## Development

See [Contributing](docs/CONTRIBUTING.md) for the virtual-environment setup and test-isolation rules. In an activated development environment:

```bash
pip install -e '.[dev]'
pre-commit install
python -m pytest
ruff check .
ruff format --check .
```

The default test suite excludes paid API tests. Sandbox integration tests require the Harness runtime; `WAVEBENCH_REQUIRE_SANDBOX_TESTS=1` makes missing support fail instead of skip.

The [architecture guide](docs/architecture.md) maps the CLI, API client, Harness, modes, TUI, and persistence. The installed `wb` command exposes Harness file tools for local development. [Symphony](docs/symphony.md) is a separate, optional Linear-driven automation service; ordinary WaveBench usage and contribution require no tracker setup.

## License

Licensed under the [MIT License](LICENSE).
