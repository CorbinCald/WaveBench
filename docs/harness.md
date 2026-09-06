# Harness mode

Harness is the default code benchmark. `--mode code` is an alias for
`--mode harness`; old history, prompt history (`.benchmark_query_history.code`),
and single-file artifacts remain readable and are not converted or executed.
Existing saved Auto-open and Auto-install settings are preserved. A new config
defaults to incremental opening and dependencies off.

## File tools

Install WaveBench with `pip install -e .` to get `wb`, or use
`python -m wavebench.harness`. The developer binds an **existing** project root:

```bash
mkdir -p /tmp/my-project
wb --root /tmp/my-project write src/main.py <<'PY'
from helpers import answer
print(answer())
PY
wb --root /tmp/my-project write src/helpers.py <<'PY'
def answer():
    return 42
PY
wb --root /tmp/my-project ls src
wb --root /tmp/my-project read src/main.py 1:2
wb --root /tmp/my-project edit src/helpers.py <<'JSON'
{"old":"return 42","new":"return 43"}
JSON
wb --root /tmp/my-project parallel "read src/main.py" "read src/helpers.py"
wb --root /tmp/my-project lint
wb --root /tmp/my-project done <<'JSON'
{"runtime":"python","entry":"src/main.py"}
JSON
wb --root /tmp/my-project delete src --recursive
```

`done` validates and submits a launch descriptor. The standalone file CLI prints
the descriptor and does not launch a project; only a benchmark's controller
admits execution. Model calls have one function, `wb`, with a structured
`command` argument. There is no model-supplied root, environment, shell, or run
command. `--json` accepts one command object or an array on stdin:

```json
[
  {"command":"write","path":"a.txt","content":"quotes ' \" and\nnewlines\n"},
  {"command":"read","path":"a.txt"},
  {"command":"read","path":"missing.txt"}
]
```

Every call gets an identified result in submitted order, even on validation
failure. Independent reads and disjoint-file operations overlap (four at a
time); conflicting paths and subtree operations wait in order. Lint waits for
pending writes and blocks subsequent writes. A batch containing `done` and any
other operation is rejected in full. Call IDs are deduplicated for the session;
reusing an ID with different arguments fails. An exact edit must match once or
the file stays unchanged. Output is bounded and explicitly marked when
truncated; full tool records and subprocess logs are saved in `metadata/`.
Providers that fill optional schema properties may send unused fields; the
dispatcher uses only the fields for the selected command. Unknown fields,
including any attempt to change the root, are rejected.

## Supported project runners

| Runtime | Descriptor | Success rule |
|---|---|---|
| `python` | `.py` entry, optional literal `args` | Program exits with code 0 within 60 seconds |
| `node` | `.js`, `.mjs`, or `.cjs` entry, optional literal `args` | Program exits with code 0 within 60 seconds |
| `python-server` / `node-server` | Entry as above; HTTP listener on `PORT` (8000 inside the private namespace); optional `preview` URL path | An HTTP 2xx/3xx response within 20 seconds |
| `static` | `.html`/`.htm` entry; optional `preview` URL path | Trusted static server loads the entry over HTTP within 20 seconds |

For example: `{"command":"done","runtime":"python-server","entry":"server.py","preview":"/health"}`.
Arguments are passed after the entry point as literal strings. Shell strings,
package scripts, arbitrary executables, GUI/interactive desktop runners, npm
dependencies, and development reloaders are unsupported. Use plain HTTP server
entry points without debug/watch/reload behavior. WaveBench sets `CI=1`,
`NODE_ENV=production`, and `FLASK_DEBUG=0`, and never launches a watcher or
restarts a preview. These are startup/runtime checks, not browser interaction
tests or a project quality grade. A static HTML load alone does not validate its
JavaScript interactions; inspect them in the managed preview.

An initial pass completes the model session. Only first-run failure opens one
repair phase in the same conversation. It may include many file/lint calls,
followed by `done` and one final execution. Failed process startup counts as an
admitted attempt. Missing tooling, unsupported launch descriptors, dependency
setup errors, failed lint, generation failure, and budget exhaustion before a
launch do not consume attempts or unlock repair. A cancelled or abandoned
repair keeps the first failure, with no fabricated second attempt.

## Isolation and dependency policy

Ubuntu's AppArmor policy may require enabling the distribution's Bubblewrap
profile. A preflight error such as `bwrap: loopback: Failed RTM_NEWADDR: Operation
not permitted` can indicate this restriction. On Ubuntu 24.04, an administrator
can install and load the packaged profile:

```bash
sudo apt-get install bubblewrap nodejs python3-pip apparmor-profiles
sudo install -m 0644 /usr/share/apparmor/extra-profiles/bwrap-userns-restrict /etc/apparmor.d/bwrap-userns-restrict
sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict
```

Our Ubuntu CI loads this profile before requiring the real sandbox tests.
The profile allows Bubblewrap's namespace setup and removes capabilities from
its children. See [AppArmor's profile](https://gitlab.com/apparmor/apparmor/-/blob/master/profiles/apparmor/profiles/extras/bwrap-userns-restrict)
and [Ubuntu's namespace policy](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/).

The first runner requires Linux, Bubblewrap, system Python 3 and Node, with
working unprivileged namespaces. Auto-install additionally requires system
pip. Preflight fails explicitly; WaveBench never falls back to a working
directory alone. Tool definitions and runtime availability are the same for
every selected model.

All file operations walk from an open root directory descriptor with
`O_NOFOLLOW`. Absolute paths, traversal, symlinks, linked-file reads/writes,
and special-file operations are rejected. Paths are limited to 4,096 UTF-8 bytes
and 128 components; there is no small file-count cap. Writes replace atomically, and
directory creation/deletion use directory descriptors, including under link
replacement races. Prompt-derived directory names are single sanitized
components; exclusive invocation and model directories prevent collisions.

Lint and generated code see only their project, isolated runtime data, a
read-only trusted helper, and the read-only system toolchain (`/usr`, `/bin`,
`/lib`, `/lib64`). A private PID/network namespace, cleared environment, and
closed inherited descriptors keep host credentials, host services, controller
state, history, and sibling outputs out of reach. The only external preview
connection is a controller-owned loopback proxy to the existing sandbox server
over a pinned Unix socket descriptor. Socket path replacement cannot redirect
the controller into a host service. A preview does not relaunch the project. All subprocesses
are stopped on timeout/cancellation, before repair, and at review completion.

`.wb/` is reserved inside the project for disposable, per-operation runtime
state and dependency directories. Model file tools cannot alter it, and the
sandbox hides it at its project mount; the active runtime directory is mounted
at `/state`, and dependencies at `/deps`. Neither contains attempt counters or
controller state. Python imports its entry directory, project root, and `/deps`;
dependency `.pth`/`sitecustomize` hooks are not loaded.

Auto-install is visible and effective even with Auto-open off. When enabled,
`requirements.txt` accepts PyPI names and version constraints. Pip downloads and
installs **wheels only** in a fresh per-model target, using an isolated config
and the PyPI index. No source builds, local paths, URLs, includes, extra indices,
generated setup scripts, or package scripts are allowed. Only this trusted pip
setup has networking; generated code and lint do not. A changed manifest on
repair gets a fresh dependency target. With Auto-install off, a nonempty
requirements manifest fails setup explicitly. No LLM guesses imports or shares
an environment between models.

Lint uses Python compilation without imports, `node --check`, JSON parsing, and
HTML parsing. It ignores package scripts, project plugins, and configuration
hooks. HTML checks are structural parsing, not full HTML/CSS validation.

## Budgets and records

Defaults are configured in `wavebench/harness/config.py`. Override them under
`"harness"` in `.benchmark_config.json`; all values must be positive integers.

| Limit | Default |
|---|---:|
| Build / repair model turns | 32 / 12 |
| Total tokens, including every input and output | 256,000 |
| Output tokens per turn | 16,384 |
| Active build / repair time | 900 / 300 seconds |
| Program / startup / lint / dependency setup | 60 / 20 / 30 / 120 seconds |
| Managed preview review | 600 seconds, or Enter/Ctrl-C |
| Tool response / saved subprocess diagnostics | 16,000 characters / 8 MiB per subprocess |
| Calls per batch / concurrent file calls | 64 / 4 |
| Concurrent API requests / subprocess checks or launches | 12 / 4 |
| File data / project source data | 8 MiB per file / 128 MiB |
| Total source, runtime and dependency storage | 512 MiB, monitored during subprocesses |
| Project execution attempts | **One initial run, plus one retry only after failure** |

The token budget uses provider usage where available and a conservative UTF-8
byte bound otherwise. Context admission includes the whole conversation and
schema plus a reserve; provider context/output caps and reasoning adjustments
are recorded per turn. Missing usage and cost are persisted as unknown, never
invented as zero. HTTP retries are bounded separately and never replay completed
tool effects. Truncated or malformed streamed arguments do not execute.
Python execution also has a 1 GiB address-space limit; Node uses a 512 MiB V8
heap. A subprocess has a 128 MiB individual-file write limit. The aggregate
storage monitor polls every 100 ms, so a fast writer can briefly overshoot it;
the project is retained when the process is stopped.

History and each model's `metadata/result.json` record generation completion,
runtime attempts/outcomes, workspace/entry, lint/setup logs, configuration,
actual provider/model, all turn usage and costs, API retries, and phase
timestamps. API/tool time, initial generation, repair, scheduling wait, setup,
and runtime durations are separate. Leaderboard `time_s` is active build plus
repair time; an `after_all` wait does not inflate model performance time.
Lifetime analytics label harness records and do not mix them into historical
one-shot model rows. Failed runs' known costs are also included.

## Verification

The default suite is offline. Lifecycle tests use scripted conversations and
real sandboxed Python/Node/static subprocesses; protocol tests use a local HTTP
SSE server. CI installs Bubblewrap and requires the sandbox tests.

```bash
python -m pytest -m 'not slow'
ruff check .
ruff format --check .
```

The explicit live matrix uses two model families, real CLI invocations, all
three Auto-open settings, multi-file Python with intentional failure/repair,
and static web projects. It writes full logs, usage totals, timestamps,
attempt counts, and project paths into the chosen directory:

```bash
python scripts/verify_harness_live.py --live \
  --model openai/gpt-5.6-luna --model anthropic/claude-haiku-4.5 \
  --output /tmp/wavebench-live-check
```

This command uses paid OpenRouter requests. Its headless viewer loads the
managed URL without rerunning the project; browser interaction and the TUI
should also be exercised locally. See [verification evidence](harness-verification.md).

Protocol references: [OpenRouter tool calling](https://openrouter.ai/docs/guides/features/tool-calling),
[Anthropic parallel-call semantics](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use),
and [Bubblewrap's security model](https://github.com/containers/bubblewrap).
