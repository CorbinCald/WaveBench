# Symphony for WaveBench with Pi

This repo includes a small Python implementation of the OpenAI Symphony service specification:
<https://github.com/openai/symphony/blob/main/SPEC.md>.

This version uses [Pi](https://pi.dev) as the worker coding agent through Pi RPC mode instead of OpenAI Codex app-server. To find the documentation shipped with your global Pi installation:

```bash
npm root -g
```

Look under `@mariozechner/pi-coding-agent/docs/` in that directory for `rpc.md`
and `sdk.md`. Symphony supports Python 3.10 and later, like the WaveBench CLI.

The Symphony package lives under `symphony/` and exposes a `symphony` console script.

## What is implemented

- `WORKFLOW.md` loading with optional YAML front matter.
- Typed config defaults and `$VAR` indirection for tracker credentials and workspace paths.
- Strict Liquid-like prompt rendering for `{{ issue.* }}`, `{{ attempt }}`, `{% if %}`, and `{% for %}`.
- Linear GraphQL reader/writer for candidate issues, latest issue comments, image URLs in issue text/attachments, state refresh, state transitions, comments, description updates, URL attachments, and the optional raw `linear_graphql` helper.
- Per-issue workspace creation under `workspace.root`, sanitized directory names, root containment checks, native per-issue git branches, clean-worktree rebasing, and lifecycle hooks.
- A polling orchestrator with bounded global/per-state concurrency, blocker checks, reconciliation, stall detection, and exponential retry scheduling.
- A Pi RPC JSONL client that launches `pi --mode rpc --no-session`, sends rendered prompts, consumes Pi events until `agent_end`, and auto-cancels extension UI dialogs so unattended runs do not stall indefinitely.
- Structured Python logging as the operator-visible status surface.
- A Linear state-machine workflow: `Todo` → `In Progress` → `Human Review` → `Merging`, where `Merging` commits any remaining workspace changes, rebases, pushes the issue branch, and creates or finds a GitHub pull request through `gh`.
- Project Pi skills under `.agents/skills/` for Linear ticket handling and optional interactive Playwright validation evidence posted to Linear.

## Trust and safety posture

This implementation is for trusted local automation experiments. Workspace path checks prevent accidental launches outside the configured workspace root, but hooks and Pi runs are still powerful local processes.

Implementation-defined policies:

- Native git automation is trusted repo configuration. It clones `git.repo`, creates/switches per-issue branches, skips rebases while the worktree is dirty, and uses `gh pr` for pull requests when configured.
- Hook scripts are trusted startup configuration and run with `sh -lc` inside the per-issue workspace.
- Pi is launched directly from the quoted executable and arguments in `pi.command`; shell operators and shell startup files are not evaluated. Put required environment variables in the daemon's environment or `.env`.
- Changes to hooks, `pi.command`, workspace root, or Git settings require restarting the daemon. Hot reload rejects changes to these execution settings and keeps the last working configuration. Prompt, tracker, polling, and agent-limit changes can still reload.
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

4. Create a Linear personal API key and make it available to Symphony. You can either export it in your shell:

   ```bash
   export LINEAR_API_KEY=...
   ```

   Or put it in a gitignored `.env` file next to `WORKFLOW.md`:

   ```env
   LINEAR_API_KEY=...
   LINEAR_PROJECT_SLUG=your-project-slug
   ```

   The Symphony CLI auto-loads the workflow-adjacent `.env` before resolving `WORKFLOW.md` and before launching Pi workers. Already-exported environment variables take precedence over `.env` values.

5. For PR automation, install/authenticate the GitHub CLI in the host environment used by Symphony:

   ```bash
   gh auth login
   gh auth status
   ```

6. Set `LINEAR_PROJECT_SLUG` in your shell or `.env`. The supplied workflow resolves
   `project_slug: $LINEAR_PROJECT_SLUG`; it contains no maintainer-specific project ID.

7. Start the daemon only when you are ready to dispatch real Linear work:

   ```bash
   symphony ./WORKFLOW.md
   ```

   If the console script is not on your `PATH`, use the Python module form from the repo root:

   ```bash
   python3 -m symphony ./WORKFLOW.md
   ```

   `python3 -m symphony --once ./WORKFLOW.md` is also a dispatching tick, not a dry run; use it only when you intentionally want Symphony to pick up eligible Linear issues.

The default workflow stores issue workspaces under `.symphony/workspaces/`, which is gitignored.

The optional GitHub issue-sync workflow is disabled in forks by default. To use
it, add repository secrets `LINEAR_API_KEY`, `LINEAR_TEAM_KEY`, and
`LINEAR_PROJECT_ID`, then set the repository variable `LINEAR_SYNC_ENABLED` to
`true`. Use a key scoped to the intended team/project. Public workflow output
does not print Linear project names or workspace URLs.

Automatic GitHub issue sync skips closed or unsynced issues. Manual dispatch
still reports a missing Linear counterpart as an error.

An issue gets at most `agent.max_attempts` execution/retry cycles (default: 3),
with exponential backoff. Reaching the limit leaves its workspace intact and
stops dispatching it for the lifetime of the daemon. Fix the issue or configuration
and restart to retry. With `auto_transition: false`, successful work completes
without changing Linear state and is not immediately dispatched again. Successful
work with an unavailable review state also stops after the bounded attempts.

## Git and branch lifecycle

The workflow uses this git config block:

```yaml
git:
  enabled: true
  repo: https://github.com/CorbinCald/WaveBench.git
  remote: origin
  base_branch: main
  branch_prefix: symphony
  rebase_policy: clean-only
  push_on_merging: true
  pr_on_merging: true
```

Behavior:

- Every Linear issue gets its own workspace under `workspace.root` and its own branch.
- Branch naming prefers Linear's `issue.branch_name`; otherwise Symphony generates `symphony/<issue-id>-<title-slug>`.
- Newly created workspaces are cloned from `git.repo`, switched to the issue branch from `origin/main`, and then `hooks.after_create` runs.
- Existing dirty workspaces that were accidentally left on `main` are migrated by creating the issue branch at the current `HEAD`; dirty changes are preserved.
- Before each worker run, Symphony fetches the remote. If the worktree is clean, it rebases the issue branch onto `origin/main`. If the worktree is dirty, it skips the rebase and logs that fact.
- When a Linear issue is moved to `Merging`, Symphony commits remaining workspace changes, fetches/rebases, pushes the branch, runs `gh pr view` / `gh pr create`, posts the PR URL to Linear, and attaches the PR URL to the issue.

## Pi configuration

The workflow uses this Pi config block:

```yaml
pi:
  command: pi --mode rpc --no-session --model openai/gpt-5.5 --thinking high
  turn_timeout_ms: 3600000
  read_timeout_ms: 5000
  stall_timeout_ms: 300000
  ingest_linear_images: true
  max_linear_images: 6
  max_linear_image_bytes: 8000000
```

When `pi.ingest_linear_images` is enabled, Symphony discovers screenshots/images referenced by Linear issue descriptions, comments, and URL attachments; downloads supported PNG/JPEG/GIF/WebP images up to `pi.max_linear_image_bytes`; and sends up to `pi.max_linear_images` as Pi RPC image attachments on the first turn. Keep this enabled only with a Pi model/provider that supports image input.

Add Pi CLI flags directly to `pi.command` when needed, for example:

```yaml
pi:
  command: pi --mode rpc --no-session --model anthropic/claude-sonnet-4-5 --thinking high
```

Because Symphony starts Pi inside the per-issue workspace, Pi's project-local discovery (`AGENTS.md`, `.pi/extensions/`, `.pi/skills/`, `.agents/skills/`, `.pi/prompts/`) applies to each workspace copy.

## Linear comments and images in prompts

Symphony fetches up to 12 latest Linear comments for each issue and appends them to the Pi prompt after the rendered workflow text. Comments are included verbatim, newest first, with minimal `--- comment ... ---` / `--- end comment ---` delimiters. They are not summarized.

With `pi.ingest_linear_images: true`, Markdown images (`![alt](url)`), HTML `<img>` tags, likely image URLs, and Linear URL attachments are fetched best-effort and sent to Pi as native image attachments. Download failures or unsupported/non-image responses are skipped so the text prompt can still run.

## Agent observability

Symphony emits concise `agent_step` INFO logs for each worker attempt: workspace creation, hooks, Pi startup/shutdown, prompt rendering, Linear image ingestion, Pi turns, state refreshes, and cleanup. Pi RPC milestones such as `agent_start`, `turn_started`, `tool_execution_start`, `tool_execution_end`, and `agent_end` are also logged with the Linear issue identifier.

When a successful run leaves a git-backed workspace with no dirty files and no commits ahead of the base branch, Symphony leaves the issue active and posts a Linear comment with a short run summary: Pi turns completed, images sent, tool execution count, the first 100 words of the first response when available, or the provider stop/error status exposed by Pi RPC when no response text was emitted. It also adds a clear note when no tools ran. This makes no-op runs inspectable without exposing prompt text, image data, or secrets.

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
- `Human Review`: ready for human review. Before Symphony moves a successful run here, git-backed workspaces must have reviewable changes: either a dirty worktree or commits ahead of the configured base branch. If no changes are found, Symphony comments and leaves the issue active instead of preemptively requesting review; move the issue back to `Todo` when it should be retried.
- `Merging`: ready for PR automation. Symphony does not dispatch a worker; it prepares the issue branch, pushes it, creates/finds a PR, comments with the PR URL, and leaves the issue in `Merging`.
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

Real Linear/Pi execution requires a valid `LINEAR_API_KEY` exported in the shell or present in the workflow-adjacent `.env`, a real Linear project slug, network access, an authenticated/configured Pi installation, and authenticated `gh` for PR automation.
