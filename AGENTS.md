# WaveBench agent instructions

This repo is set up for Symphony-driven Pi agents working from Linear issues.

## Symphony workflow

Symphony owns Linear issue state. Agents should not move, close, reopen, or otherwise change issue state; use Linear only for comments, blockers, handoff notes, and evidence links.

When running under Symphony, work only in the per-issue workspace and do not modify files outside it.

## Useful skills

Pi can load project skills from `.agents/skills/`:

- `write-linear`: Linear issue comments, blocker notes, final handoff, and evidence links. Symphony owns state transitions.
- `interactive-verification`: Playwright CLI workflow for interactive validation/demo evidence, with files posted to Linear.

Use these when relevant instead of inventing ad-hoc tracker or verification procedures.

## Validation commands

Prefer targeted validation first, then broader checks when appropriate:

```bash
pytest tests/unit/test_symphony_*.py
pytest tests
ruff check .
```

If a change affects interactive behavior, use the real app surface when practical. For WaveBench, exercise interactive mode (`wavebench`, `wavebench --config`, or the issue-specific command). Post screenshots/videos/transcripts to Linear; do not store review evidence in the repo.

## Safety

- Never print or commit secrets such as `LINEAR_API_KEY` or provider API keys.
- Keep changes minimal and scoped to the Linear issue.
- Preserve existing workspace changes unless the issue explicitly asks to discard them.
