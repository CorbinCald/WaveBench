# Preview identity verification — September 12, 2026

Managed previews now show the model, selection number, prompt, run ID and
execution attempt in a persistent header. The tab/window title and terminal
review links identify the same result. App source files remain unchanged.

Ran the free acceptance helper through a real PTY with isolated temporary
settings and projects:

```bash
PYTHONPATH=. python scripts/verify_preview_destination.py --destination host
```

The helper creates identical app pages and finishes result #2 before #1.
Captured the browser-open URLs through `BROWSER`, then opened those exact URLs
in Chromium using Playwright CLI. Verified:

- Both results had distinct, correct labels despite reversed completion order.
- Repeating the benchmark produced a new run ID in the header and title.
- Buttons and text inputs worked inside the generated app.
- Navigating to another page with a different app title, navigating back, and
  reloading preserved the benchmark identity.
- “Open app only” opened the original page in a separate tab.
- Headers remained readable at 1280×800 and 360×740 without horizontal overflow.
- Enter stopped the managed previews and removed their temporary projects.

Automated checks covered real static, Python and Node sandbox previews, repaired
attempt labels, escaped model/prompt text, relative assets, form POST bodies,
redirects, cookies, WebSockets, laptop handoff, and cleanup. The default suite
passed with sandbox checks required: **1,080 passed, one paid API test
deselected**. Ruff lint and format checks passed. Raw browser evidence stayed
in temporary files; no paid model requests were used.
