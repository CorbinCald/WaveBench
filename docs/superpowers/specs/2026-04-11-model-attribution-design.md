# Model Attribution for Auto-Opened Artifacts

**Date:** 2026-04-11
**Status:** Design — awaiting implementation plan
**Author:** Brainstormed with Claude

## Problem

When WaveBench auto-opens generated artifacts (in `incremental` or `after_all` mode), the user often cannot tell which model produced which artifact. Two distinct sub-problems:

**Track A — Viewer-opened artifacts** (`.html`, `.md`, `.svg`, `.xml`, `.txt`): the browser or OS viewer derives its tab/window title from the file's internal metadata (e.g., HTML `<title>` tag), not from the filename. Even though the filename is `claudeOpus4.6.html`, the browser tab shows whatever the LLM wrote in the `<title>` element (e.g., `Snake Game`). The model-identifying filename is obscured.

**Track B — Executed code files** (`.py` primarily): the terminal tab WaveBench spawns already has `--title claudeOpus4.6` via `_open_file_in_tab`, so the terminal is fine. But when the Python script spawns a GUI window via pygame, tkinter, turtle, PyQt, PySide, or Kivy, that GUI window's title is set by the model's code (e.g., `pygame.display.set_caption("Snake")`) and contains no model attribution. The user sees an unattributed game window on screen.

The practical consequence: when `--open incremental` is active and several models finish one after another, the user — whose attention is on WaveBench's wave animation — ends up with a stack of browser tabs and/or GUI windows and no reliable way to correlate any of them back to the producing model.

## Goals

1. **Track A:** Inject minimal provenance markers into viewer-opened artifacts at file-write time so that when the viewer opens them, the visible title area reads `[modelName] OriginalTitle` (or an analogous format per file type).
2. **Track B:** Ensure any GUI window spawned by an executed `.py` file has its window title prefixed with `[modelName]`, *without modifying the `.py` file itself*, by running the script through a thin wrapper that monkey-patches common GUI title APIs before invoking the user's code.
3. Both tracks must be pure, deterministic, and independently unit-testable — Track A as a string→string function, Track B as a standalone runner module.

## Non-goals

- **No visible in-page banners, watermarks, or headers** on top of rendered HTML/Markdown content. Attribution lives in the metadata strata (`<title>`, HTML comments, XML comments), not the rendered body.
- **No retroactive rewriting** of existing artifacts in `benchmarkResults/`.
- **No attempts to patch `.js` or `.sh` GUI windows** — outside the benchmark-artifact universe, and the terminal tab title already handles the common case.
- **No deep HTML/XML parsing.** Regex-based surgical edits only. The risk of a `<title>` inside a JS string is accepted in exchange for zero parser dependencies.
- **No coverage of exotic GUI libraries** beyond the big five (pygame, tkinter, turtle, Qt-family, Kivy). Others gracefully fall through with no attribution.

## Design — Track A: Non-Code File Dispatcher

### Module: `wavebench/attribution.py` (new, ~90 lines)

A pure string→string dispatcher selected by file extension. Called from `core.py:process_model` and `process_model_text` immediately before `fh.write(...)`. Returns input unchanged for unknown extensions.

```python
def inject_model_attribution(content: str, model_name: str, ext: str) -> str:
    """Return `content` with a model-attribution marker injected, or unchanged
    if no known strategy applies to `ext`. Pure string→string."""
```

### Per-format strategies

| Extension | Strategy |
|---|---|
| `.html`, `.htm` | **HTML `<title>` rewrite.** Regex-find the first `<title>...</title>` (case-insensitive, DOTALL); rewrite its body to `[{model_name}] {original}`. If no `<title>`, inject `<title>[{model_name}]</title>` at top of `<head>`. If no `<head>`, prepend at document start. In all three cases, also prepend `<!-- WaveBench: {model_name} -->`. |
| `.md`, `.markdown` | **HTML comment prepend.** `<!-- WaveBench: {model_name} -->\n\n` at top, treated as invisible by all major Markdown renderers. **Exception:** if the file begins with YAML frontmatter (`---\n...\n---\n`), the comment is injected *after* the closing `---\n` so frontmatter remains the first block. |
| `.svg` | **SVG `<title>` injection.** Insert `<title>[{model_name}]</title>` as the first child of the root `<svg ...>` element. Browsers displaying an SVG as a top-level document use this as the tab title. |
| `.xml` | **XML comment prepend.** Insert `<!-- WaveBench: {model_name} -->` after any leading `<?xml ...?>` declaration. If no declaration, prepend at document start. |
| `.txt` | **Single-line banner.** `[WaveBench: {model_name}]\n\n` at top. Unavoidably visible — `.txt` has no comment syntax — but clearly branded and trivially strippable. |
| anything else | **Fall through unchanged.** `.json`, `.css`, `.ts`, `.tsx`, `.rs`, `.go`, etc. are not mutated. |

### Integration points in `core.py`

Track A wires into two write-sites. Minimal change — inject one transformation line before the write call:

**`process_model` (code mode, around `core.py:579`):**
```python
filename = get_unique_filename(output_dir, model_name, ext)
filepath = os.path.join(output_dir, filename)
try:
    content = inject_model_attribution(parsed["code"], model_name, ext)
except Exception:
    content = parsed["code"]  # defensive fallback
with open(filepath, "w", encoding="utf-8") as fh:
    fh.write(content)
```

**`process_model_text` (text mode, around `core.py:716`):**
```python
try:
    content = inject_model_attribution(raw_markdown, model_name, ".md")
except Exception:
    content = raw_markdown
```

**Import** at top of `core.py`:
```python
from .attribution import inject_model_attribution
```

### Helper internals

```python
import re

_TITLE_RE = re.compile(r'(<title[^>]*>)(.*?)(</title>)', re.IGNORECASE | re.DOTALL)
_HEAD_OPEN_RE = re.compile(r'<head[^>]*>', re.IGNORECASE)
_SVG_OPEN_RE = re.compile(r'<svg\b[^>]*>', re.IGNORECASE)
_XML_DECL_RE = re.compile(r'^\s*<\?xml[^?]*\?>', re.IGNORECASE)
_YAML_FRONTMATTER_RE = re.compile(r'^---\n.*?\n---\n', re.DOTALL)


def inject_model_attribution(content: str, model_name: str, ext: str) -> str:
    ext = ext.lower()
    if ext in (".html", ".htm"):
        return _inject_html(content, model_name)
    if ext in (".md", ".markdown"):
        return _inject_markdown(content, model_name)
    if ext == ".svg":
        return _inject_svg(content, model_name)
    if ext == ".xml":
        return _inject_xml(content, model_name)
    if ext == ".txt":
        return f"[WaveBench: {model_name}]\n\n{content}"
    return content


def _inject_html(html: str, model_name: str) -> str:
    comment = f"<!-- WaveBench: {model_name} -->\n"

    def _rewrite(m):
        open_tag, body, close_tag = m.group(1), m.group(2).strip(), m.group(3)
        new_body = f"[{model_name}] {body}" if body else f"[{model_name}]"
        return f"{open_tag}{new_body}{close_tag}"

    new_html, n = _TITLE_RE.subn(_rewrite, html, count=1)
    if n:
        return comment + new_html

    head = _HEAD_OPEN_RE.search(html)
    if head:
        i = head.end()
        return html[:i] + f"\n{comment}<title>[{model_name}]</title>" + html[i:]

    return f"{comment}<title>[{model_name}]</title>\n{html}"


def _inject_markdown(md: str, model_name: str) -> str:
    comment = f"<!-- WaveBench: {model_name} -->\n\n"
    fm = _YAML_FRONTMATTER_RE.match(md)
    if fm:
        i = fm.end()
        return md[:i] + comment + md[i:]
    return comment + md


def _inject_svg(svg: str, model_name: str) -> str:
    match = _SVG_OPEN_RE.search(svg)
    if not match:
        return svg  # fragment or malformed; fall through
    i = match.end()
    return svg[:i] + f"<title>[{model_name}]</title>" + svg[i:]


def _inject_xml(xml: str, model_name: str) -> str:
    comment = f"<!-- WaveBench: {model_name} -->"
    decl = _XML_DECL_RE.match(xml)
    if decl:
        i = decl.end()
        return xml[:i] + f"\n{comment}" + xml[i:]
    return f"{comment}\n{xml}"
```

## Design — Track B: Python Wrapper Runner

### Module: `wavebench/runner.py` (new, ~90 lines)

A standalone, self-contained script that:
1. Parses `model_name` and `script_path` from `sys.argv`
2. Monkey-patches common GUI window-title APIs to prefix titles with `[{model_name}]`
3. Uses `runpy.run_path(script_path, run_name="__main__")` to execute the user's script faithfully

**Self-contained requirement:** `runner.py` imports only stdlib modules (`runpy`, `sys`) plus dynamic imports of the GUI libraries it patches. It must **not** import from other `wavebench` modules, so it runs correctly in any Python environment where `wavebench` itself may or may not be installed (including auto-created venvs).

### Libraries patched

| Library | API patched | Coverage note |
|---|---|---|
| **pygame** | `pygame.display.set_caption` | Most common game library in benchmark outputs |
| **tkinter** | `tkinter.Wm.wm_title` + `tkinter.Wm.title` alias | Class-level patch on the `Wm` mixin catches `Tk`, `Toplevel`, and all subclasses |
| **turtle** | `turtle.TurtleScreen.title` | Patches at the TurtleScreen level to catch both direct `Screen().title()` and indirect uses |
| **PyQt5 / PyQt6 / PySide2 / PySide6** | `QtWidgets.QWidget.setWindowTitle` | Inherited by `QMainWindow`, `QDialog`, `QDockWidget`, etc. One patch covers all subclasses |
| **Kivy** | `kivy.core.window.Window` title observer | Singleton; bind a `title` observer that rewrites the value to `f"{prefix} {value}"` whenever the model's code sets it, guarded by a `startswith(prefix)` check to prevent recursive re-entry |

Each patch wrapped in `try/except ImportError` so missing libraries are silently skipped.

### Idempotency guard

Each patch function checks for a `_wavebench_patched` sentinel attribute on the target before wrapping, and sets that attribute on its wrapper. Running patches twice in the same process (e.g., during test runs) does not stack prefixes.

```python
def _patch_pygame(prefix: str) -> None:
    try:
        import pygame.display as _pd
    except ImportError:
        return
    if getattr(_pd.set_caption, "_wavebench_patched", False):
        return
    _orig = _pd.set_caption
    def _wrapped(title, *args, **kwargs):
        return _orig(f"{prefix} {title}", *args, **kwargs)
    _wrapped._wavebench_patched = True  # type: ignore[attr-defined]
    _pd.set_caption = _wrapped
```

The other four patch functions (`_patch_tkinter`, `_patch_turtle`, `_patch_qt`, `_patch_kivy`) follow the **same structural template**: optional `ImportError` guard → sentinel check → capture `_orig` → define `_wrapped` that prefixes the title arg → mark `_wrapped._wavebench_patched = True` → reassign. Kivy is the one exception (binds a title observer instead of wrapping a method) and checks its sentinel on the `Window` object itself.

### Runner skeleton

```python
"""WaveBench execution wrapper — prefixes GUI window titles with the model name.

Runs a Python script via runpy.run_path after monkey-patching common GUI
libraries. Self-contained: imports only stdlib + opportunistic GUI libs.

Usage: python <path-to-runner.py> <model_name> <script_path> [args...]
"""
import runpy
import sys


# See "Idempotency guard" section above for the full implementation template.
# Each of these follows the same pattern: ImportError-guard → sentinel check
# → wrap/bind with prefix-prepending callable → mark sentinel → reassign.
def _patch_pygame(prefix: str) -> None: ...
def _patch_tkinter(prefix: str) -> None: ...
def _patch_turtle(prefix: str) -> None: ...
def _patch_qt(prefix: str) -> None: ...
def _patch_kivy(prefix: str) -> None: ...


def _apply_patches(prefix: str) -> None:
    _patch_pygame(prefix)
    _patch_tkinter(prefix)
    _patch_turtle(prefix)
    _patch_qt(prefix)
    _patch_kivy(prefix)


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python runner.py <model_name> <script> [args...]",
              file=sys.stderr)
        sys.exit(2)

    model_name = sys.argv[1]
    script = sys.argv[2]
    prefix = f"[{model_name}]"

    _apply_patches(prefix)

    sys.argv = [script] + sys.argv[3:]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
```

### Integration points in `core.py`

Track B interposes the runner only for `.py` files. All platform branches (Linux / macOS / Windows) get the same treatment via a small extracted helper.

**New helper** near `_shell_cmd`:

```python
_RUNNER_PATH = os.path.join(os.path.dirname(__file__), "runner.py")


def _build_python_cmd_parts(interp: str, filepath: str) -> List[str]:
    """Return the argv prefix for running a file, interposing the runner for .py.

    For .py files: [interp, runner_path, model_name, filepath]
    For all others: [interp, filepath]
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".py":
        model_name = os.path.splitext(os.path.basename(filepath))[0]
        return [interp, _RUNNER_PATH, model_name, filepath]
    return [interp, filepath]
```

Note: the `_v2`/`_v3` suffix from `get_unique_filename` is **not** stripped — it's informative when the same model is run multiple times in one session.

**`_shell_cmd` (Linux primary, `core.py:128`):** simplifies to:
```python
def _shell_cmd(interp: str, filepath: str) -> str:
    """Build a bash command string that runs a file and waits for Enter."""
    cmd = " ".join(shlex.quote(p) for p in _build_python_cmd_parts(interp, filepath))
    return f'{cmd}; echo; read -rp "Press Enter to close…"'
```

All six Linux callsites (`core.py:198, 258, 325, 337, 350, 365`) transparently inherit the wrapper.

**macOS paths (`_run_in_terminal_single` `core.py:176`, `_open_files_as_tabs` `core.py:281-304`):** both build `cmd_str` directly without going through `_shell_cmd`. They're updated to use `_build_python_cmd_parts` for consistency:
```python
parts = _build_python_cmd_parts(interp, filepath)
cmd_str = " ".join(shlex.quote(p) for p in parts)
```

**Windows path (`core.py:185-190`):** same treatment — build parts via the helper, then pass individually to `subprocess.Popen` or join for `cmd /k`.

### Graceful degradation

If the runner fails to import (missing file, corrupted install), the spawned terminal shows the error. This is intentionally loud — a missing runner indicates a broken install, not a recoverable situation. The terminal-tab title (`--title claudeOpus4.6`) still provides fallback attribution.

## Edge Cases

### Track A

- **`<title>` inside a JS string** in an HTML file: regex will mangle it cosmetically. Accepted. The `<!-- WaveBench: ... -->` comment provides independent attribution.
- **Multiple `<title>` elements** (invalid HTML5): only the first is rewritten (`count=1`). Browsers use the first anyway.
- **Empty `<title></title>`**: produces `<title>[claudeOpus4.6]</title>` (no trailing space).
- **HTML with no `<head>`**: falls through to prepending at document start. Browsers handle this.
- **Markdown with YAML frontmatter**: comment injected after closing `---\n` so frontmatter remains first.
- **SVG without `<svg>` open tag** (fragment): unchanged.
- **XML with DOCTYPE**: comment goes after `<?xml?>` declaration but may land before DOCTYPE, which is valid (comments may precede DOCTYPE).
- **`.txt` with shebang**: banner inserts before the shebang. Accepted limitation — text mode almost always means prose, not executables.

### Track B

- **Library we don't patch** (wxPython, Pyglet, Arcade, Panda3D, Dear PyGui, ctypes Win32/Cocoa/X11 direct calls): script runs normally; no prefix. Graceful degradation.
- **Script sets `pygame.display.set_caption` after our patch**: model code wins. Rare and acceptable.
- **Script with `sys.exit()`**: `SystemExit` propagates cleanly through `runpy.run_path`; shell wrapper still shows "Press Enter to close…".
- **Script with unhandled exception**: traceback printed, exit code nonzero, shell wrapper's `read -rp` still runs (bash `;` separator).
- **Script reads `sys.argv[0]` or `__file__`**: `runpy.run_path` sets these to the script path; `sys.argv` is reassigned before the run.
- **Auto-install venv scenario**: runner is invoked by absolute path and imports only stdlib + dynamic GUI libs, so it works regardless of which Python is the interpreter.
- **Patch functions called twice**: idempotency sentinel prevents double-prefixing.

## Testing

**Test framework:** stdlib `unittest`. WaveBench currently has no test infrastructure and no test-framework dependency declared in `pyproject.toml`; using `unittest` adds zero new dependencies. Tests run via `python -m unittest discover tests`. Adding pytest as a dev dependency is a valid alternative but explicitly out of scope for this spec.

**Directory:** The `tests/` directory does not currently exist in the repo and must be created as part of this work, along with an empty `tests/__init__.py` so `unittest discover` can traverse it.

### Unit tests — `tests/test_attribution.py` (~150 lines)

Pure string→string tests of `inject_model_attribution`. No file I/O, no mocks, no GUI. Headless-safe.

Cases covered:
- HTML with existing `<title>` (basic, empty, attributed, multiple, case-insensitive)
- HTML without `<title>` but with `<head>`
- HTML fragment (no `<head>`, no `<html>`)
- HTML with `<title>` inside a JS string (documents the known cosmetic regression)
- Markdown basic
- Markdown with YAML frontmatter (verifies comment goes *after* frontmatter)
- SVG with and without `<svg>` open tag
- XML with and without `<?xml?>` declaration
- `.txt` banner
- Unknown extensions (`.json`, `.css`, `.tsx`, `.rs`, `.go`, `.foo`) return unchanged
- Empty input for each supported extension
- `None` content raises `TypeError` (exercises caller's fallback)

**Target:** 100% line coverage of `wavebench/attribution.py`.

### Unit tests — `tests/test_runner.py` (~80 lines)

Tests the runner's argv parsing, script delegation, and individual patch functions. **GUI libraries are tested via `sys.modules` fakes**, not real instantiation — keeps all tests headless and fast.

Cases covered:
- Runner prints usage and exits 2 on missing args
- Runner runs the target script and writes expected output
- Runner sets `sys.argv[0]` to the script path (not the runner path)
- Runner preserves `__name__ == "__main__"` behavior
- Runner passes through extra args via `sys.argv[1:]`
- Each patch function wraps the target library's title API correctly using an in-memory fake module
- Missing library: patch function no-ops cleanly
- Idempotency: calling each patch function twice produces a single-prefixed title, not `[M] [M] X`

### Smoke test (manual)

```bash
wavebench --prompt "Create a pygame window that opens and waits for keyboard input" \
          --open incremental
```

Expected: GUI window opens with title `[claudeOpus4.6] <whatever>`. With multiple models selected, each gets a distinct titled window.

## Summary of changed files

| File | Change | Size |
|---|---|---|
| `wavebench/attribution.py` | **NEW** — Track A dispatcher + per-format helpers | ~90 lines |
| `wavebench/runner.py` | **NEW** — Track B Python wrapper (self-contained) | ~90 lines |
| `wavebench/core.py` | **MODIFIED** — import, 2 write-site injections, `_build_python_cmd_parts` helper, update `_shell_cmd` and macOS/Windows branches | ~30 lines added/changed |
| `tests/test_attribution.py` | **NEW** — unit tests for Track A | ~150 lines |
| `tests/test_runner.py` | **NEW** — unit tests for Track B | ~80 lines |

**Net:** roughly +440 lines, with all `core.py` modifications confined to ~30 lines in well-localized spots.

## Known Limitations

1. **Regex-based HTML mutation** can mangle the cosmetic `<title>` rewrite when the LLM embeds `<title>` inside a JS/CSS string literal. Mitigated by the dual-layer `<!-- WaveBench: ... -->` comment, which is injected independently.
2. **GUI libraries outside the big five** (wxPython, Pyglet, Arcade, Panda3D, Dear PyGui, raw Win32/Cocoa/X11 via ctypes) are not patched. Scripts using them show unprefixed window titles, degrading to filename-in-terminal-tab attribution.
3. **Scripts that fight the patch** by reassigning `pygame.display.set_caption` after our wrapper get the model's title. Rare and accepted.
4. **`.txt` files get a visible first-line banner** rather than an invisible comment, because plain text has no comment syntax. The banner is clearly branded (`[WaveBench: {model_name}]`) and trivially strippable.
5. **Retroactive attribution is not supported.** Only files written *after* this feature ships get attribution; existing `benchmarkResults/` artifacts remain unattributed.
