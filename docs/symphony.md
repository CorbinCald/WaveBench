# Symphony for WaveBench with Pi

This repo includes a small Python implementation of the OpenAI Symphony service specification:
<https://github.com/openai/symphony/blob/main/SPEC.md>.

This version uses [Pi](https://pi.dev) as the worker coding agent through Pi RPC mode instead of OpenAI Codex app-server. The relevant local Pi docs are:

- `/home/corbin/.npm-global/lib/node_modules/@mariozechner/pi-coding-agent/docs/rpc.md`
- `/home/corbin/.npm-global/lib/node_modules/@mariozechner/pi-coding-agent/docs/sdk.md`

The Symphony package lives under `symphony/` and exposes a `symphony` console script.

## What is implemented

- `WORKFLOW.md` loading with optional YAML front matter.
- Typed config defaults and `$VAR` indirection for tracker credentials and workspace paths.
- Strict Liquid-like prompt rendering for `{{ issue.* }}`, `{{ attempt }}`, `{% if %}`, and `{% for %}`.
- Linear GraphQL reader/writer for candidate issues, latest issue comments, state refresh, state transitions, comments, description updates, URL attachments, and the optional raw `linear_graphql` helper.
- Per-issue workspace creation under `workspace.root`, sanitized directory names, root containment checks, and lifecycle hooks.
- A polling orchestrator with bounded global/per-state concurrency, blocker checks, reconciliation, stall detection, and exponential retry scheduling.
- A Pi RPC JSONL client that launches `pi --mode rpc --no-session`, sends rendered prompts, consumes Pi events until `agent_end`, and auto-cancels extension UI dialogs so unattended runs do not stall indefinitely.
- Structured Python logging as the operator-visible status surface.
- A Linear state-machine workflow: `Todo` → `In Progress` → `Human Review`, with `Merging` reserved for future PR automation and terminal states cleaned up.
- Project Pi skills under `.agents/skills/` for Linear ticket handling and optional interactive Playwright validation evidence posted to Linear.

## Trust and safety posture

This implementation is for trusted local automation experiments. Workspace path checks prevent accidental launches outside the configured workspace root, but hooks and Pi runs are still powerful local processes.

Implementation-defined policies:

- Hook scripts are trusted repo configuration and run with `sh -lc` inside the per-issue workspace.
- Pi is launched with `bash -lc <pi.command>` in the per-issue workspace.
- Pi authentication, model selection, tools, extensions, skills, and provider policy come from your normal Pi setup and/or flags in `pi.command`.
- Pi RPC extension UI dialog requests (`select`, `confirm`, `input`, `editor`) are automatically cancelled in unattended Symphony runs. Fire-and-forget UI notifications are logged/ignored.
- Tracker state writes are built into the orchestrator only. Agents may use the `write-linear` skill for concise plans, blockers, evidence links, and final handoff comments.

Before production use, harden the host environment: run under a dedicated OS user, restrict credentials, choose conservative Pi tools/extensions, and keep workspaces outside sensitive directories.

## Setup

1. Install this repo in editable mode:

   ```bash
   pip install -e '.[dev]'
   ```

2. Install and authenticate Pi:

   ```bash
   npm install -g @mariozechner/pi-coding-agent
   pi
   /login
   ```

   Or configure provider API keys such as `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `OPENROUTER_API_KEY`.

3. Confirm Pi RPC starts:

   ```bash
   pi --mode rpc --no-session
   ```

   Press `Ctrl+C` to stop it after confirming it launches.

4. Create a Linear personal API key and export it:

   ```bash
   export LINEAR_API_KEY=...
   ```

5. Edit the repo-root `WORKFLOW.md` and replace `project_slug` with your Linear project slug.

6. Start the daemon only when you are ready to dispatch real Linear work:

   ```bash
   symphony ./WORKFLOW.md
   ```

   `symphony --once ./WORKFLOW.md` is also a dispatching tick, not a dry run; use it only when you intentionally want Symphony to pick up eligible Linear issues.

The default workflow stores issue workspaces under `.symphony/workspaces/`, which is gitignored.

## Pi configuration

The workflow uses this Pi config block:

```yaml
pi:
  command: pi --mode rpc --no-session
  turn_timeout_ms: 3600000
  read_timeout_ms: 5000
  stall_timeout_ms: 300000
```

Add Pi CLI flags directly to `pi.command` when needed, for example:

```yaml
pi:
  command: pi --mode rpc --no-session --model anthropic/claude-sonnet-4-5 --thinking high
```

Because Symphony starts Pi inside the per-issue workspace, Pi's project-local discovery (`AGENTS.md`, `.pi/extensions/`, `.pi/skills/`, `.agents/skills/`, `.pi/prompts/`) applies to each workspace copy.

## Linear comments in prompts

Symphony fetches up to 12 latest Linear comments for each issue and appends them to the Pi prompt after the rendered workflow text. Comments are included verbatim, newest first, with minimal `--- comment ... ---` / `--- end comment ---` delimiters. They are not summarized.

## Linear state machine

`WORKFLOW.md` configures Linear as the operator control plane:

```yaml
tracker:
  active_states:
    - Todo
    - In Progress
  working_state: In Progress
  review_state: Human Review
  merging_state: Merging
  auto_transition: true
  post_status_comments: true
```

Behavior:

- `Todo`: ready for Symphony pickup.
- `In Progress`: active work. Symphony moves picked-up `Todo` issues here.
- `Human Review`: ready for human review. Symphony moves successful runs here and does not redispatch them.
- `Merging`: reserved for future PR creation/merge automation; it is not an active state today.
- terminal states (`Done`, `Closed`, `Cancelled`, `Canceled`, `Duplicate`): workspaces may be cleaned up.

Create the configured Linear states before enabling a long-running daemon. If a configured Linear state does not exist, Symphony logs a warning and leaves the issue in its current state.

## Agent skills and evidence

The repo includes project skills discovered by Pi from `.agents/skills/`:

- `write-linear`: Linear comments, blocker reporting, evidence links, and final handoff template. It does not change issue state.
- `interactive-verification`: optional Playwright CLI-driven interactive validation/demo evidence, with screenshots/videos uploaded to Linear.

Use interactive evidence when tests are not enough or the reviewer should see the final behavior. For WaveBench, exercise `wavebench`, `wavebench --config`, or the issue-specific command. Playwright CLI applies when the app surface is browser-accessible, such as a web app, local preview, browser-hosted terminal, WebView, or demo page. Evidence should be posted to Linear, not kept under the repo or `.symphony/`; temporary files are only for upload and should be deleted.

## Validation

Run the deterministic tests:

```bash
pytest tests/unit/test_symphony_*.py
```

Real Linear/Pi execution requires valid `LINEAR_API_KEY`, a real Linear project slug, network access, and an authenticated/configured Pi installation.
