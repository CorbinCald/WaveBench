# WaveBench agent instructions

Work directly on `main` for maintainer tasks. Commit and push completed changes;
do not create worktrees or pull requests in this repository.

Preserve existing local changes. Keep generated projects, credentials, personal
tracker settings, and runtime databases out of commits. Do not add AI co-author
or session-link trailers to commits.

Before pushing, run the relevant checks and then the default suite:

```bash
python -m pytest
ruff check .
ruff format --check .
```

CI runs the default suite in parallel processes, so tests must not share state:
write only under `tmp_path`, change environment variables or the working
directory only through `monkeypatch`, bind servers to port 0, and never depend on
test order. Mark any test that asserts on elapsed wall-clock time
`@pytest.mark.serial`; CI runs those alone after the parallel pass. See "Writing
tests" in `docs/CONTRIBUTING.md`.

The default suite excludes paid API tests. Exercise interactive changes through
the real terminal UI, using isolated temporary settings and outputs. Keep raw
verification recordings in temporary files; concise, sanitized verification
notes may be committed under `docs/`.

Symphony is optional. When a task is explicitly dispatched through Symphony,
follow its provided workspace and branch instructions in `WORKFLOW.md`. Its
Linear skills under `.agents/skills/` apply to those assigned tasks. Only write
to a tracker when the task requests it; a normal repository task needs no Linear
setup or comments. See `docs/CONTRIBUTING.md` for developer setup.
