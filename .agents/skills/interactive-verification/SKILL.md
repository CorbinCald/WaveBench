---
name: interactive-verification
description: Use when a change should be exercised through an app's interactive surface or shown to the human reviewer. Prefer Playwright CLI for browser/webview/preview surfaces, record concise proof when useful, and post evidence directly to the Linear issue.
---

# Interactive Verification

Use this skill when the reviewer should see app behavior for improved human verification.

## Scope

- Use a project's real interactive surface.
- For the WaveBench project, this means running it in interactive mode.
- For any browser-based project, use Playwright CLI (`npm install -g @playwright/cli@latest`; `playwright-cli --help`) when the interactive surface is browser-accessible: web app, local preview, browser-hosted terminal, WebView, or demo page.

## Post evidence to Linear

Use a temp dir, upload to Linear, then delete the temp file/dir.

Use the helper script from this skill:

```bash
python .agents/skills/interactive-verification/scripts/post-linear-file.py \
  --issue COR-5 \
  --file "$TMPDIR/demo.webm" \
  --title "Interactive verification" \
  --body "Demonstrates the final behavior."
rm -rf "$TMPDIR"
```

For text-only proof, use the `write-linear` skill to post a Linear comment instead of uploading a file.

## Final handoff

In the final Linear comment, include:

- what interactive command/surface was used,
- what was verified,
- Linear-hosted screenshot/video links if any,
- test/lint commands and results.
