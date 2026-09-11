# Preview destination verification — September 11, 2026

The default test suite passed locally with sandbox tests required: **957 passed,
one paid API test deselected**. Ruff lint and format checks passed.

Real PTY tests at 48 and 100 columns exercised the configuration menu, all three
destinations, persistence across reopening, cancellation, and preservation of
Auto-open. Routing tests covered local desktops, SSH, headless hosts, connected
and disconnected persistent Herdr sessions, and explicit host selection.

The Harness integration checks used actual sandboxed static projects and
controller-owned HTTP proxies. They covered multiple previews, all Auto-open
policies, one preview closing without affecting another, helper expiry,
cancellation, and a missing helper preserving runtime success. Six integration
checks also passed against the actual candidate `herdr-review offer` helper,
using isolated state; the regular suite uses a subprocess protocol fixture so
Wavebench does not require Herdr to run its tests.

The Herdr companion's 42 tests passed separately, including real SSH forwarding,
cookies, port collisions, reconnects, and the new registration pipe's EOF,
termination, and timeout cleanup. Existing laptop clients require no update.

The disposable verification script opened two real sandbox previews through the
host browser's `BROWSER` interface, checked their HTTP content, and cleaned them
up. Automatic mode then published two real Harness previews to the connected
laptop. Fresh acknowledgements matched both preview tokens and ports with no
errors; the actual review screen changed from waiting to opened on the laptop.

Repeat the free acceptance check from the checkout with its Python environment:

```bash
PYTHONPATH=. python scripts/verify_preview_destination.py --menu
```

Choose Settings → Preview destination, cycle with Space, and save with Enter.
Two deterministic generated-project fixtures exercise the normal Harness
runtime and presentation path without model API calls or personal settings.
Click Increment and type a review note in each page. Enter or Ctrl-C in the
terminal ends review and removes the temporary projects. The outer task runner
and per-preview handoff cap lifetimes at 15 minutes; the normal review window
defaults to 10 minutes.
