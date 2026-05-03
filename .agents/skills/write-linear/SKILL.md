---
name: write-linear
description: Use to post concise comments, blocker notes, final handoffs, and evidence links to Linear issues.
---

# Write Linear

Use this skill when a Symphony task needs a Linear comment or handoff update.

## When to comment

Post concise comments for:

1. **Plan** for non-trivial work: scope, likely files, validation plan.
2. **Blocker** when human input is needed: what you tried and the exact question.
3. **Evidence**: Linear-hosted screenshots, videos, logs, or transcripts.
4. **Final handoff**: changed files, validation results, and evidence links.

Avoid noisy progress comments.

## Post a comment

Use `LINEAR_API_KEY`; never print it. The issue may be an identifier like `COR-5` or a Linear UUID.

```bash
python .agents/skills/write-linear/scripts/post-comment.py \
  --issue COR-5 \
  --body-file /tmp/linear-comment.md
```

For short comments:

```bash
python .agents/skills/write-linear/scripts/post-comment.py \
  --issue COR-5 \
  --body "Blocked: I can reproduce the issue, but need the expected behavior for X."
```

For file evidence, upload it with the `interactive-verification` skill, then include the Linear-hosted link in a comment or final handoff.

## Final handoff template

```markdown
Ready for review.

Changed files:
- `path/to/file.py` — short reason

Validation:
- `pytest path/to/test.py` — passed
- `ruff check .` — passed

Evidence:
- Linear-hosted screenshot/video/transcript links if applicable

Notes/blockers:
- None, or describe remaining human decision.
```
