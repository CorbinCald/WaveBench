---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: "wavebench-9ecefbaf337c"
  active_states:
    - Todo
    - In Progress
  working_state: In Progress
  review_state: Human Review
  merging_state: Merging
  auto_transition: true
  post_status_comments: true
  terminal_states:
    - Closed
    - Cancelled
    - Canceled
    - Duplicate
    - Done
polling:
  interval_ms: 30000
workspace:
  root: .symphony/workspaces
hooks:
  after_create: |
    git clone --depth 1 https://github.com/CorbinCald/WaveBench.git .
    python3 -m venv .venv
    . .venv/bin/activate
    pip install -e '.[dev]'
  before_run: |
    git status --short
  after_run: |
    git status --short
agent:
  max_concurrent_agents: 2
  max_turns: 3
  max_retry_backoff_ms: 300000
pi:
  command: pi --mode rpc --no-session --model openai/gpt-5.5 --thinking high
  turn_timeout_ms: 3600000
  read_timeout_ms: 5000
  stall_timeout_ms: 300000
---

You are working on a Linear issue for the WaveBench repository.

Issue: {{ issue.identifier }}
Title: {{ issue.title }}
State: {{ issue.state }}
URL: {{ issue.url }}
Labels: {{ issue.labels }}

{% if attempt %}
This is retry/continuation attempt #{{ attempt }}. Resume from the current workspace state and avoid repeating completed work.
{% endif %}

Description:
{% if issue.description %}
{{ issue.description }}
{% else %}
No description was provided.
{% endif %}

Work only inside the provided per-issue workspace.

## Linear control

Use Linear for comments, blockers, handoff notes, and evidence links.

## Required operating procedure

1. Read `AGENTS.md` and any relevant skill under `.agents/skills/` before making changes. Use the `write-linear` skill for Linear comments, blockers, handoff notes, and evidence links. Use the `interactive-verification` skill when interactive validation or a final demo would help review.
2. Inspect the issue, repository state, and current workspace diff. If there are existing changes, preserve and build on them instead of restarting.
3. Post a concise Linear plan comment for non-trivial work: scope, likely files, and validation plan.
4. Reproduce or characterize the requested behavior before changing code, when practical.
5. Make the smallest code/docs/test changes needed for the issue. Do not refactor unrelated code.
6. Run targeted validation first, then broader tests/lint if the change warrants it. For interactive behavior, exercise the real app surface.
7. Post useful logs, screenshots, transcripts, or short videos directly to the Linear issue. Use temporary files when upload tooling requires them, then delete them.
8. If blocked, post a Linear comment with the blocker, what you tried, and what human input is needed. Include interactive evidence when relevant.
9. When done, leave a final Linear comment with the changed files, validation commands/results, and Linear-hosted evidence links. Then stop; do not keep iterating after a complete patch.

Prefer existing WaveBench project conventions.
