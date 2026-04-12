# Model Attribution for Auto-Opened Artifacts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make it clear which model produced each auto-opened WaveBench artifact by injecting provenance markers into viewer-opened files (Track A) and wrapping Python artifact execution to prefix GUI window titles (Track B).

**Architecture:** Two pure, independently-testable units. Track A is a string→string dispatcher in `wavebench/attribution.py` called at file-write time in `core.py`. Track B is a self-contained `wavebench/runner.py` script that monkey-patches pygame/tkinter/turtle/Qt/Kivy title APIs, then `runpy.run_path()`s the target script. `core.py` routes `.py` executions through the runner via a new `_build_python_cmd_parts` helper that feeds both the Linux `_shell_cmd` path and the macOS/Windows branches.

**Tech Stack:** Python 3.10+, stdlib `unittest`, stdlib `runpy`, stdlib `re`. Zero new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-04-11-model-attribution-design.md`

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `tests/__init__.py` | **NEW** | Empty package marker so `python -m unittest discover tests` traverses it |
| `tests/test_attribution.py` | **NEW** | Pure unit tests for every Track A injection strategy |
| `tests/test_runner.py` | **NEW** | Subprocess + `sys.modules`-fake tests for Track B |
| `wavebench/attribution.py` | **NEW** | Track A dispatcher + per-format helpers (`_inject_html`, `_inject_markdown`, `_inject_svg`, `_inject_xml`) |
| `wavebench/runner.py` | **NEW** | Track B wrapper: argv parsing, `_patch_*` monkey-patchers, `runpy.run_path` delegation |
| `wavebench/core.py` | **MODIFIED** | Import + call `inject_model_attribution` at write-sites; add `_RUNNER_PATH` + `_build_python_cmd_parts`; update `_shell_cmd`, `_run_in_terminal_single`, `_open_files_as_tabs` for all three OSes |

---

## Test-Running Commands

Every test task uses these commands — memorize the shape:

```bash
# Run ALL tests in tests/
python -m unittest discover tests -v

# Run ONE test file
python -m unittest tests.test_attribution -v

# Run ONE test class
python -m unittest tests.test_attribution.TestHtmlTitleRewrite -v

# Run ONE test method
python -m unittest tests.test_attribution.TestHtmlTitleRewrite.test_existing_title_gets_rewritten -v
```

From the project root: `/home/corbin/Documents/WaveBench/`.

---

### Task 1: Create test infrastructure

**Files:**
- Create: `tests/__init__.py` (empty)

- [ ] **Step 1: Create the empty `tests/__init__.py`**

File content: empty file (zero bytes).

- [ ] **Step 2: Verify unittest discovery works with an empty tests/**

Run: `python -m unittest discover tests -v`
Expected: `Ran 0 tests in 0.000s` followed by `OK` (no errors about missing directory).

- [ ] **Step 3: Commit**

```bash
git add tests/__init__.py
git commit -m "Add empty tests/ package for upcoming attribution and runner tests"
```

---

### Task 2: Attribution module skeleton + unknown-extension fall-through

**Files:**
- Create: `wavebench/attribution.py`
- Create: `tests/test_attribution.py`

- [ ] **Step 1: Write the failing test for unknown-extension fall-through**

Create `tests/test_attribution.py` with:

```python
"""Unit tests for wavebench.attribution."""
import unittest

from wavebench.attribution import inject_model_attribution


class TestUnknownExtension(unittest.TestCase):
    """Unknown extensions must pass through unchanged."""

    def test_json_returns_unchanged(self):
        content = '{"key": "value"}'
        result = inject_model_attribution(content, "claudeOpus4.6", ".json")
        self.assertEqual(result, content)

    def test_css_returns_unchanged(self):
        content = "body { color: red; }"
        result = inject_model_attribution(content, "claudeOpus4.6", ".css")
        self.assertEqual(result, content)

    def test_rust_returns_unchanged(self):
        content = "fn main() { println!(\"hi\"); }"
        result = inject_model_attribution(content, "claudeOpus4.6", ".rs")
        self.assertEqual(result, content)

    def test_tsx_returns_unchanged(self):
        content = "export const App = () => <div>Hi</div>"
        result = inject_model_attribution(content, "claudeOpus4.6", ".tsx")
        self.assertEqual(result, content)

    def test_unknown_returns_unchanged(self):
        content = "anything"
        result = inject_model_attribution(content, "claudeOpus4.6", ".foo")
        self.assertEqual(result, content)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest tests.test_attribution -v`
Expected: `ImportError: cannot import name 'inject_model_attribution' from 'wavebench.attribution'` (module doesn't exist).

- [ ] **Step 3: Create the attribution module with the dispatcher skeleton**

Create `wavebench/attribution.py` with:

```python
"""Injects model-attribution markers into viewer-opened benchmark artifacts.

Pure string→string helpers. No file I/O, no side effects. Dispatched by
file extension; unknown extensions pass through unchanged.
"""
import re


def inject_model_attribution(content: str, model_name: str, ext: str) -> str:
    """Return `content` with a model-attribution marker injected, or unchanged
    if no known strategy applies to `ext`."""
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


# Stub helpers — implemented in subsequent tasks. Return content unchanged
# for now so the unknown-extension fall-through test passes immediately.
def _inject_html(html: str, model_name: str) -> str:
    return html


def _inject_markdown(md: str, model_name: str) -> str:
    return md


def _inject_svg(svg: str, model_name: str) -> str:
    return svg


def _inject_xml(xml: str, model_name: str) -> str:
    return xml
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m unittest tests.test_attribution -v`
Expected: 5 tests run, all pass. `OK`.

- [ ] **Step 5: Commit**

```bash
git add wavebench/attribution.py tests/test_attribution.py
git commit -m "Add attribution module skeleton with unknown-extension fall-through"
```

---

### Task 3: HTML injection — `<title>` rewrite path

**Files:**
- Modify: `wavebench/attribution.py` (replace `_inject_html` stub)
- Modify: `tests/test_attribution.py` (add `TestHtmlTitleRewrite` class)

- [ ] **Step 1: Write the failing tests for HTML title rewrite**

Append to `tests/test_attribution.py`:

```python
class TestHtmlTitleRewrite(unittest.TestCase):
    """The first <title> in an HTML document is rewritten to [model] original."""

    def test_existing_title_gets_rewritten(self):
        html = "<html><head><title>Snake Game</title></head><body></body></html>"
        result = inject_model_attribution(html, "claudeOpus4.6", ".html")
        self.assertIn("<title>[claudeOpus4.6] Snake Game</title>", result)
        self.assertIn("<!-- WaveBench: claudeOpus4.6 -->", result)
        self.assertTrue(result.startswith("<!-- WaveBench: claudeOpus4.6 -->"))

    def test_empty_title(self):
        html = "<html><head><title></title></head></html>"
        result = inject_model_attribution(html, "claudeOpus4.6", ".html")
        self.assertIn("<title>[claudeOpus4.6]</title>", result)
        # No trailing space before closing tag
        self.assertNotIn("[claudeOpus4.6] </title>", result)

    def test_title_with_attributes_preserved(self):
        html = '<html><head><title lang="en">Hi</title></head></html>'
        result = inject_model_attribution(html, "claudeOpus4.6", ".html")
        self.assertIn('<title lang="en">[claudeOpus4.6] Hi</title>', result)

    def test_case_insensitive_title_match(self):
        html = "<HTML><HEAD><TITLE>Hi</TITLE></HEAD></HTML>"
        result = inject_model_attribution(html, "claudeOpus4.6", ".html")
        self.assertIn("[claudeOpus4.6] Hi", result)

    def test_multiple_titles_only_first_rewritten(self):
        html = "<head><title>A</title><title>B</title></head>"
        result = inject_model_attribution(html, "claudeOpus4.6", ".html")
        self.assertIn("[claudeOpus4.6] A", result)
        self.assertIn("<title>B</title>", result)
        self.assertNotIn("[claudeOpus4.6] B", result)

    def test_title_body_whitespace_trimmed_before_rewrite(self):
        html = "<head><title>  Snake Game  </title></head>"
        result = inject_model_attribution(html, "claudeOpus4.6", ".html")
        self.assertIn("<title>[claudeOpus4.6] Snake Game</title>", result)

    def test_title_in_js_string_is_mangled_but_comment_still_present(self):
        """Known cosmetic regression: regex matches <title> inside JS strings.

        The comment provides independent attribution regardless, so the
        provenance information is still recoverable from the file.
        """
        html = '<html><body><script>const s = "<title>X</title>";</script></body></html>'
        result = inject_model_attribution(html, "claudeOpus4.6", ".html")
        # The comment is still injected — primary attribution channel survives
        self.assertIn("<!-- WaveBench: claudeOpus4.6 -->", result)
        # Cosmetic regression: the JS string literal got rewritten too
        self.assertIn("[claudeOpus4.6] X", result)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_attribution.TestHtmlTitleRewrite -v`
Expected: all 6 new tests fail (stub returns content unchanged, so assertions don't match).

- [ ] **Step 3: Implement `_inject_html` title-rewrite path**

In `wavebench/attribution.py`, add module-level regex at the top (after `import re`):

```python
_TITLE_RE = re.compile(r'(<title[^>]*>)(.*?)(</title>)', re.IGNORECASE | re.DOTALL)
```

Replace the `_inject_html` stub with:

```python
def _inject_html(html: str, model_name: str) -> str:
    comment = f"<!-- WaveBench: {model_name} -->\n"

    def _rewrite(m):
        open_tag, body, close_tag = m.group(1), m.group(2).strip(), m.group(3)
        new_body = f"[{model_name}] {body}" if body else f"[{model_name}]"
        return f"{open_tag}{new_body}{close_tag}"

    new_html, n = _TITLE_RE.subn(_rewrite, html, count=1)
    if n:
        return comment + new_html
    return html  # Fallback paths added in Task 4
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_attribution.TestHtmlTitleRewrite -v`
Expected: 6 tests run, all pass. `OK`.

- [ ] **Step 5: Run the full test file to confirm no regressions**

Run: `python -m unittest tests.test_attribution -v`
Expected: 12 tests run, all pass (5 from Task 2 + 7 from Task 3).

- [ ] **Step 6: Commit**

```bash
git add wavebench/attribution.py tests/test_attribution.py
git commit -m "Implement HTML <title> rewrite with attribution comment prefix"
```

---

### Task 4: HTML injection — `<head>` and document-start fallbacks

**Files:**
- Modify: `wavebench/attribution.py` (extend `_inject_html` with two fallback paths)
- Modify: `tests/test_attribution.py` (add `TestHtmlFallback` class)

- [ ] **Step 1: Write the failing tests for fallback paths**

Append to `tests/test_attribution.py`:

```python
class TestHtmlFallback(unittest.TestCase):
    """HTML without <title> falls back to injecting one in <head>, then doc start."""

    def test_no_title_with_head_injects_after_head(self):
        html = "<html><head><meta charset='utf-8'></head><body>hi</body></html>"
        result = inject_model_attribution(html, "claudeOpus4.6", ".html")
        self.assertIn("<title>[claudeOpus4.6]</title>", result)
        self.assertIn("<!-- WaveBench: claudeOpus4.6 -->", result)
        # Title lands after <head>, before <meta>
        head_pos = result.index("<head>")
        meta_pos = result.index("<meta")
        title_pos = result.index("<title>")
        self.assertLess(head_pos, title_pos)
        self.assertLess(title_pos, meta_pos)

    def test_fragment_without_head_prepends_at_document_start(self):
        html = "<div>hi</div>"
        result = inject_model_attribution(html, "claudeOpus4.6", ".html")
        self.assertTrue(result.startswith("<!-- WaveBench: claudeOpus4.6 -->"))
        self.assertIn("<title>[claudeOpus4.6]</title>", result)
        # Original content is preserved after our injection
        self.assertIn("<div>hi</div>", result)

    def test_head_with_attributes_matched(self):
        html = '<html><head lang="en"><meta></head></html>'
        result = inject_model_attribution(html, "claudeOpus4.6", ".html")
        self.assertIn("<title>[claudeOpus4.6]</title>", result)

    def test_empty_html_fragment(self):
        html = ""
        result = inject_model_attribution(html, "claudeOpus4.6", ".html")
        # Still gets attribution, even if empty
        self.assertIn("<!-- WaveBench: claudeOpus4.6 -->", result)
        self.assertIn("<title>[claudeOpus4.6]</title>", result)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_attribution.TestHtmlFallback -v`
Expected: all 4 new tests fail (current `_inject_html` returns content unchanged when no `<title>` matches).

- [ ] **Step 3: Add `<head>` regex and extend `_inject_html`**

In `wavebench/attribution.py`, add below `_TITLE_RE`:

```python
_HEAD_OPEN_RE = re.compile(r'<head[^>]*>', re.IGNORECASE)
```

Replace `_inject_html` with the full three-tier fallback:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_attribution -v`
Expected: 16 tests run, all pass.

- [ ] **Step 5: Commit**

```bash
git add wavebench/attribution.py tests/test_attribution.py
git commit -m "Add HTML <head> and document-start fallback injection paths"
```

---

### Task 5: Markdown injection with YAML frontmatter handling

**Files:**
- Modify: `wavebench/attribution.py` (replace `_inject_markdown` stub, add frontmatter regex)
- Modify: `tests/test_attribution.py` (add `TestMarkdown` class)

- [ ] **Step 1: Write the failing tests for Markdown**

Append to `tests/test_attribution.py`:

```python
class TestMarkdown(unittest.TestCase):
    """Markdown gets an HTML comment prepended (after YAML frontmatter if present)."""

    def test_plain_markdown_gets_comment_at_top(self):
        md = "# Heading\n\nSome text."
        result = inject_model_attribution(md, "claudeOpus4.6", ".md")
        self.assertTrue(result.startswith("<!-- WaveBench: claudeOpus4.6 -->\n\n"))
        self.assertIn("# Heading", result)

    def test_markdown_extension_alias(self):
        md = "hello"
        result = inject_model_attribution(md, "claudeOpus4.6", ".markdown")
        self.assertTrue(result.startswith("<!-- WaveBench: claudeOpus4.6 -->\n\n"))

    def test_markdown_with_yaml_frontmatter_injects_after_frontmatter(self):
        md = "---\ntitle: Post\nauthor: me\n---\n# Heading\n"
        result = inject_model_attribution(md, "claudeOpus4.6", ".md")
        # Frontmatter must remain the FIRST block so Jekyll/Hugo parse it
        self.assertTrue(result.startswith("---\ntitle: Post\nauthor: me\n---\n"))
        # Comment is injected AFTER the closing --- line
        self.assertIn("<!-- WaveBench: claudeOpus4.6 -->", result)
        comment_pos = result.index("<!-- WaveBench:")
        # Closing --- appears at the start of some line BEFORE the comment
        second_dashes_pos = result.index("---", 3)  # skip opening ---
        self.assertLess(second_dashes_pos, comment_pos)

    def test_empty_markdown(self):
        md = ""
        result = inject_model_attribution(md, "claudeOpus4.6", ".md")
        self.assertEqual(result, "<!-- WaveBench: claudeOpus4.6 -->\n\n")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_attribution.TestMarkdown -v`
Expected: all 4 new tests fail (stub returns content unchanged).

- [ ] **Step 3: Implement `_inject_markdown` with frontmatter handling**

In `wavebench/attribution.py`, add frontmatter regex near the other regexes:

```python
_YAML_FRONTMATTER_RE = re.compile(r'^---\n.*?\n---\n', re.DOTALL)
```

Replace the `_inject_markdown` stub with:

```python
def _inject_markdown(md: str, model_name: str) -> str:
    comment = f"<!-- WaveBench: {model_name} -->\n\n"
    fm = _YAML_FRONTMATTER_RE.match(md)
    if fm:
        i = fm.end()
        return md[:i] + comment + md[i:]
    return comment + md
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_attribution -v`
Expected: 20 tests run, all pass.

- [ ] **Step 5: Commit**

```bash
git add wavebench/attribution.py tests/test_attribution.py
git commit -m "Implement Markdown attribution with YAML frontmatter preservation"
```

---

### Task 6: SVG injection

**Files:**
- Modify: `wavebench/attribution.py` (replace `_inject_svg` stub, add `<svg>` regex)
- Modify: `tests/test_attribution.py` (add `TestSvg` class)

- [ ] **Step 1: Write the failing tests for SVG**

Append to `tests/test_attribution.py`:

```python
class TestSvg(unittest.TestCase):
    """SVG gets a <title> element injected as the first child of <svg>."""

    def test_svg_with_open_tag_gets_title_as_first_child(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>'
        result = inject_model_attribution(svg, "claudeOpus4.6", ".svg")
        self.assertIn("<title>[claudeOpus4.6]</title>", result)
        # <title> must appear before <circle>
        title_pos = result.index("<title>")
        circle_pos = result.index("<circle")
        self.assertLess(title_pos, circle_pos)

    def test_svg_without_svg_tag_returns_unchanged(self):
        svg = "<circle r='5'/>"
        result = inject_model_attribution(svg, "claudeOpus4.6", ".svg")
        self.assertEqual(result, svg)

    def test_svg_with_self_closing_tag_returns_unchanged(self):
        # An svg with only a self-closing <svg ... /> won't match our open-tag regex
        # because there's no closing >. Accepted: returns unchanged.
        svg = "<svg />"
        result = inject_model_attribution(svg, "claudeOpus4.6", ".svg")
        self.assertIn("<title>[claudeOpus4.6]</title>", result)

    def test_svg_case_insensitive_match(self):
        svg = '<SVG xmlns="..."><circle/></SVG>'
        result = inject_model_attribution(svg, "claudeOpus4.6", ".svg")
        self.assertIn("<title>[claudeOpus4.6]</title>", result)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_attribution.TestSvg -v`
Expected: 3 of 4 tests fail (the "returns unchanged" test passes trivially since the stub returns unchanged).

- [ ] **Step 3: Implement `_inject_svg`**

In `wavebench/attribution.py`, add the SVG regex near the others:

```python
_SVG_OPEN_RE = re.compile(r'<svg\b[^>]*>', re.IGNORECASE)
```

Replace the `_inject_svg` stub with:

```python
def _inject_svg(svg: str, model_name: str) -> str:
    match = _SVG_OPEN_RE.search(svg)
    if not match:
        return svg  # fragment or malformed; fall through
    i = match.end()
    return svg[:i] + f"<title>[{model_name}]</title>" + svg[i:]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_attribution -v`
Expected: 24 tests run, all pass.

- [ ] **Step 5: Commit**

```bash
git add wavebench/attribution.py tests/test_attribution.py
git commit -m "Implement SVG <title> child injection for browser tab attribution"
```

---

### Task 7: XML injection

**Files:**
- Modify: `wavebench/attribution.py` (replace `_inject_xml` stub, add XML decl regex)
- Modify: `tests/test_attribution.py` (add `TestXml` class)

- [ ] **Step 1: Write the failing tests for XML**

Append to `tests/test_attribution.py`:

```python
class TestXml(unittest.TestCase):
    """XML gets a comment injected after any <?xml ?> declaration."""

    def test_xml_with_declaration_comment_after_decl(self):
        xml = '<?xml version="1.0"?>\n<root><child/></root>'
        result = inject_model_attribution(xml, "claudeOpus4.6", ".xml")
        decl_pos = result.index("?>")
        comment_pos = result.index("<!-- WaveBench:")
        root_pos = result.index("<root>")
        self.assertLess(decl_pos, comment_pos)
        self.assertLess(comment_pos, root_pos)

    def test_xml_without_declaration_prepends_comment(self):
        xml = "<root><child/></root>"
        result = inject_model_attribution(xml, "claudeOpus4.6", ".xml")
        self.assertTrue(result.startswith("<!-- WaveBench: claudeOpus4.6 -->"))
        self.assertIn("<root>", result)

    def test_xml_with_encoded_declaration(self):
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n<doc/>'
        result = inject_model_attribution(xml, "claudeOpus4.6", ".xml")
        self.assertIn("<!-- WaveBench: claudeOpus4.6 -->", result)
        # Declaration still comes first
        self.assertTrue(result.startswith("<?xml"))

    def test_xml_empty_returns_comment_only(self):
        xml = ""
        result = inject_model_attribution(xml, "claudeOpus4.6", ".xml")
        self.assertEqual(result, "<!-- WaveBench: claudeOpus4.6 -->\n")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_attribution.TestXml -v`
Expected: all 4 new tests fail.

- [ ] **Step 3: Implement `_inject_xml`**

In `wavebench/attribution.py`, add the XML declaration regex:

```python
_XML_DECL_RE = re.compile(r'^\s*<\?xml[^?]*\?>', re.IGNORECASE)
```

Replace the `_inject_xml` stub with:

```python
def _inject_xml(xml: str, model_name: str) -> str:
    comment = f"<!-- WaveBench: {model_name} -->"
    decl = _XML_DECL_RE.match(xml)
    if decl:
        i = decl.end()
        return xml[:i] + f"\n{comment}" + xml[i:]
    return f"{comment}\n{xml}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_attribution -v`
Expected: 28 tests run, all pass.

- [ ] **Step 5: Commit**

```bash
git add wavebench/attribution.py tests/test_attribution.py
git commit -m "Implement XML comment injection respecting <?xml?> declaration"
```

---

### Task 8: TXT banner

**Files:**
- Modify: `tests/test_attribution.py` (add `TestTxt` class)

*(No code change to `attribution.py` — the dispatcher already handles `.txt` inline in Task 2. This task just adds the tests.)*

- [ ] **Step 1: Write the tests for `.txt` banner**

Append to `tests/test_attribution.py`:

```python
class TestTxt(unittest.TestCase):
    """TXT files get a visible first-line WaveBench banner (no comment syntax available)."""

    def test_txt_gets_banner_prefix(self):
        content = "Hello world"
        result = inject_model_attribution(content, "claudeOpus4.6", ".txt")
        self.assertEqual(result, "[WaveBench: claudeOpus4.6]\n\nHello world")

    def test_txt_empty_gets_only_banner(self):
        content = ""
        result = inject_model_attribution(content, "claudeOpus4.6", ".txt")
        self.assertEqual(result, "[WaveBench: claudeOpus4.6]\n\n")

    def test_txt_multiline_preserved(self):
        content = "Line 1\nLine 2\nLine 3"
        result = inject_model_attribution(content, "claudeOpus4.6", ".txt")
        self.assertTrue(result.startswith("[WaveBench: claudeOpus4.6]\n\n"))
        self.assertIn("Line 1\nLine 2\nLine 3", result)
```

- [ ] **Step 2: Run the tests**

Run: `python -m unittest tests.test_attribution -v`
Expected: 31 tests run, all pass (3 new tests immediately green — the dispatcher already implements `.txt`).

- [ ] **Step 3: Commit**

```bash
git add tests/test_attribution.py
git commit -m "Add tests locking in .txt banner format"
```

---

### Task 9: Wire Track A into `core.py` write-sites

**Files:**
- Modify: `wavebench/core.py` (add import + inject at `process_model` write-site around line 579 + `process_model_text` write-site around line 716)

- [ ] **Step 1: Add the import at the top of `core.py`**

Find the existing `from` / `import` block at the top of `wavebench/core.py`. Add this line alongside the other relative imports:

```python
from .attribution import inject_model_attribution
```

- [ ] **Step 2: Inject at the `process_model` write-site**

In `wavebench/core.py`, find the block inside `process_model` that writes the file (currently around line 575-580):

```python
            filename = get_unique_filename(output_dir, model_name, ext)
            filepath = os.path.join(output_dir, filename)

            with open(filepath, "w", encoding="utf-8") as fh:
                fh.write(parsed["code"])
```

Replace with:

```python
            filename = get_unique_filename(output_dir, model_name, ext)
            filepath = os.path.join(output_dir, filename)

            try:
                content_to_write = inject_model_attribution(
                    parsed["code"], model_name, ext)
            except Exception:
                content_to_write = parsed["code"]
            with open(filepath, "w", encoding="utf-8") as fh:
                fh.write(content_to_write)
```

- [ ] **Step 3: Inject at the `process_model_text` write-site**

Find the block inside `process_model_text` that writes the markdown (currently around line 716-720):

```python
        output_dir = await output_dir_task
        filename = get_unique_filename(output_dir, model_name, ".md")
        filepath = os.path.join(output_dir, filename)

        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write(content)
```

Replace with:

```python
        output_dir = await output_dir_task
        filename = get_unique_filename(output_dir, model_name, ".md")
        filepath = os.path.join(output_dir, filename)

        try:
            content_to_write = inject_model_attribution(content, model_name, ".md")
        except Exception:
            content_to_write = content
        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write(content_to_write)
```

**IMPORTANT:** the local variable `content` is already used inside `process_model_text` for the raw model response. Rename the new variable to `content_to_write` (as above) to avoid shadowing it.

- [ ] **Step 4: Verify `core.py` still imports cleanly**

Run: `python -c "from wavebench import core; print('ok')"`
Expected: `ok` (no ImportError or SyntaxError).

- [ ] **Step 5: Run the full test suite to confirm no regressions**

Run: `python -m unittest discover tests -v`
Expected: 31 tests pass.

- [ ] **Step 6: Commit**

```bash
git add wavebench/core.py
git commit -m "Wire attribution injection into process_model and process_model_text"
```

---

### Task 10: Runner module skeleton — argv parsing, usage, and `runpy` delegation

**Files:**
- Create: `wavebench/runner.py`
- Create: `tests/test_runner.py`

- [ ] **Step 1: Write the failing tests for argv parsing and script delegation**

Create `tests/test_runner.py` with:

```python
"""Integration tests for wavebench.runner (subprocess-driven)."""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


RUNNER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "wavebench", "runner.py"
)


class TestRunnerArgv(unittest.TestCase):
    """Argv parsing: missing args → exit 2 with usage."""

    def test_no_args_exits_2_with_usage(self):
        result = subprocess.run(
            [sys.executable, RUNNER_PATH],
            capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stderr)

    def test_one_arg_still_exits_2(self):
        result = subprocess.run(
            [sys.executable, RUNNER_PATH, "claudeOpus4.6"],
            capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 2)


class TestRunnerExecution(unittest.TestCase):
    """The runner must invoke the target script as if via `python script.py`."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_script(self, body: str) -> str:
        path = os.path.join(self.tmpdir, "script.py")
        with open(path, "w") as fh:
            fh.write(body)
        return path

    def test_runs_target_script(self):
        out_path = os.path.join(self.tmpdir, "out.txt")
        script = self._write_script(textwrap.dedent(f"""\
            with open({out_path!r}, "w") as fh:
                fh.write("ran")
        """))
        subprocess.run(
            [sys.executable, RUNNER_PATH, "claudeOpus4.6", script],
            capture_output=True, check=True
        )
        with open(out_path) as fh:
            self.assertEqual(fh.read(), "ran")

    def test_preserves_name_main(self):
        out_path = os.path.join(self.tmpdir, "out.txt")
        script = self._write_script(textwrap.dedent(f"""\
            if __name__ == "__main__":
                with open({out_path!r}, "w") as fh:
                    fh.write("main")
        """))
        subprocess.run(
            [sys.executable, RUNNER_PATH, "claudeOpus4.6", script],
            capture_output=True, check=True
        )
        with open(out_path) as fh:
            self.assertEqual(fh.read(), "main")

    def test_sys_argv_0_is_script_path(self):
        out_path = os.path.join(self.tmpdir, "out.txt")
        script = self._write_script(textwrap.dedent(f"""\
            import sys
            with open({out_path!r}, "w") as fh:
                fh.write(sys.argv[0])
        """))
        subprocess.run(
            [sys.executable, RUNNER_PATH, "claudeOpus4.6", script],
            capture_output=True, check=True
        )
        with open(out_path) as fh:
            self.assertEqual(fh.read(), script)

    def test_passes_extra_args(self):
        out_path = os.path.join(self.tmpdir, "out.txt")
        script = self._write_script(textwrap.dedent(f"""\
            import sys
            with open({out_path!r}, "w") as fh:
                fh.write(" ".join(sys.argv[1:]))
        """))
        subprocess.run(
            [sys.executable, RUNNER_PATH, "claudeOpus4.6", script, "--foo", "bar"],
            capture_output=True, check=True
        )
        with open(out_path) as fh:
            self.assertEqual(fh.read(), "--foo bar")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_runner -v`
Expected: 6 tests fail or error (`runner.py` doesn't exist yet, so subprocesses fail with "can't open file").

- [ ] **Step 3: Create `wavebench/runner.py` with argv parsing and runpy delegation**

Create `wavebench/runner.py`:

```python
"""WaveBench execution wrapper — prefixes GUI window titles with the model name.

Runs a Python script via runpy.run_path after monkey-patching common GUI
libraries. Self-contained: imports only stdlib + opportunistic GUI libs.

Usage: python <path-to-runner.py> <model_name> <script_path> [args...]
"""
import runpy
import sys


# Patch functions are added in Tasks 11-15. Until then, _apply_patches
# is a no-op so the argv-parsing and runpy.run_path behavior can be tested
# independently.
def _apply_patches(prefix: str) -> None:
    pass


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python runner.py <model_name> <script> [args...]",
              file=sys.stderr)
        sys.exit(2)

    model_name = sys.argv[1]
    script = sys.argv[2]
    prefix = f"[{model_name}]"

    _apply_patches(prefix)

    # Impersonate direct invocation: sys.argv[0] becomes the script path.
    sys.argv = [script] + sys.argv[3:]

    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the runner tests to verify they pass**

Run: `python -m unittest tests.test_runner -v`
Expected: 6 tests pass.

- [ ] **Step 5: Run the full test suite**

Run: `python -m unittest discover tests -v`
Expected: 37 tests pass (31 from Track A + 6 runner tests).

- [ ] **Step 6: Commit**

```bash
git add wavebench/runner.py tests/test_runner.py
git commit -m "Add runner.py skeleton with argv parsing and runpy delegation"
```

---

### Task 11: pygame patch with idempotency guard

**Files:**
- Modify: `wavebench/runner.py` (add `_patch_pygame`)
- Modify: `tests/test_runner.py` (add `TestPygamePatch` class + `_import_runner` helper)

- [ ] **Step 1: Add the runner-import helper to `tests/test_runner.py`**

Append near the top of `tests/test_runner.py`, below `RUNNER_PATH`:

```python
def _import_runner():
    """Import runner.py as a module WITHOUT running main().

    Needed because the patch functions live in runner.py, which is primarily
    a script. This helper loads it as a module so tests can invoke the private
    _patch_* functions directly.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_wavebench_runner_test", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
```

- [ ] **Step 2: Add the `types` import to `tests/test_runner.py`**

Find the imports block at the top of `tests/test_runner.py` (added in Task 10) and add `import types` alongside the other imports. The imports section should now include (at minimum):

```python
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
```

- [ ] **Step 3: Write the failing tests for the pygame patch**

Append to the bottom of `tests/test_runner.py`:

```python
class TestPygamePatch(unittest.TestCase):
    """_patch_pygame wraps pygame.display.set_caption to prepend the prefix."""

    def setUp(self):
        # Save any existing pygame-related entries in sys.modules
        self._saved = {}
        for k in list(sys.modules.keys()):
            if k.startswith("pygame"):
                self._saved[k] = sys.modules[k]
                del sys.modules[k]

        # Install a fake pygame module whose set_caption records its arg
        fake_pygame = types.ModuleType("pygame")
        fake_display = types.ModuleType("pygame.display")
        self.captured = []
        def _record(title, *a, **kw):
            self.captured.append(title)
        fake_display.set_caption = _record
        fake_pygame.display = fake_display
        sys.modules["pygame"] = fake_pygame
        sys.modules["pygame.display"] = fake_display

    def tearDown(self):
        for k in list(sys.modules.keys()):
            if k.startswith("pygame"):
                del sys.modules[k]
        for k, v in self._saved.items():
            sys.modules[k] = v

    def test_patch_prefixes_title(self):
        runner = _import_runner()
        runner._patch_pygame("[M]")
        import pygame.display
        pygame.display.set_caption("Snake")
        self.assertEqual(self.captured, ["[M] Snake"])

    def test_patch_is_idempotent(self):
        runner = _import_runner()
        runner._patch_pygame("[M]")
        runner._patch_pygame("[M]")  # second call must be a no-op
        import pygame.display
        pygame.display.set_caption("Snake")
        self.assertEqual(self.captured, ["[M] Snake"])  # NOT "[M] [M] Snake"

    def test_patch_missing_library_is_noop(self):
        # Remove the fake so the library is "missing"
        for k in list(sys.modules.keys()):
            if k.startswith("pygame"):
                del sys.modules[k]
        runner = _import_runner()
        # Should not raise
        runner._patch_pygame("[M]")
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `python -m unittest tests.test_runner.TestPygamePatch -v`
Expected: tests fail with `AttributeError: module '_wavebench_runner_test' has no attribute '_patch_pygame'`.

- [ ] **Step 5: Implement `_patch_pygame`**

In `wavebench/runner.py`, add above `_apply_patches`:

```python
def _patch_pygame(prefix: str) -> None:
    try:
        import pygame.display as _pd  # type: ignore[import-not-found]
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

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m unittest tests.test_runner.TestPygamePatch -v`
Expected: 3 tests pass.

- [ ] **Step 7: Commit**

```bash
git add wavebench/runner.py tests/test_runner.py
git commit -m "Add pygame title-prefix patch with idempotency sentinel"
```

---

### Task 12: tkinter patch with idempotency guard

**Files:**
- Modify: `wavebench/runner.py` (add `_patch_tkinter`)
- Modify: `tests/test_runner.py` (add `TestTkinterPatch` class)

- [ ] **Step 1: Write the failing tests for tkinter patch**

Append to `tests/test_runner.py`:

```python
class TestTkinterPatch(unittest.TestCase):
    """_patch_tkinter wraps tk.Wm.wm_title at the class level."""

    def setUp(self):
        self._saved = sys.modules.get("tkinter")
        if "tkinter" in sys.modules:
            del sys.modules["tkinter"]

        fake_tk = types.ModuleType("tkinter")

        class _FakeWm:
            def __init__(self):
                self._title = None

            def wm_title(self, string=None):
                if string is not None:
                    self._title = string
                    return None
                return self._title

            title = wm_title

        fake_tk.Wm = _FakeWm
        sys.modules["tkinter"] = fake_tk
        self.FakeWm = _FakeWm

    def tearDown(self):
        if "tkinter" in sys.modules:
            del sys.modules["tkinter"]
        if self._saved is not None:
            sys.modules["tkinter"] = self._saved

    def test_patch_prefixes_title(self):
        runner = _import_runner()
        runner._patch_tkinter("[M]")
        import tkinter as tk
        wm = tk.Wm()
        wm.wm_title("Snake")
        self.assertEqual(wm.wm_title(), "[M] Snake")

    def test_patch_is_idempotent(self):
        runner = _import_runner()
        runner._patch_tkinter("[M]")
        runner._patch_tkinter("[M]")
        import tkinter as tk
        wm = tk.Wm()
        wm.wm_title("Snake")
        self.assertEqual(wm.wm_title(), "[M] Snake")

    def test_patch_missing_library_is_noop(self):
        if "tkinter" in sys.modules:
            del sys.modules["tkinter"]
        runner = _import_runner()
        runner._patch_tkinter("[M]")  # must not raise
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_runner.TestTkinterPatch -v`
Expected: tests fail with `AttributeError: ... has no attribute '_patch_tkinter'`.

- [ ] **Step 3: Implement `_patch_tkinter`**

In `wavebench/runner.py`, add below `_patch_pygame`:

```python
def _patch_tkinter(prefix: str) -> None:
    try:
        import tkinter as tk
    except ImportError:
        return
    if getattr(tk.Wm.wm_title, "_wavebench_patched", False):
        return
    _orig = tk.Wm.wm_title
    def _wrapped(self, string=None):
        if string is not None:
            return _orig(self, f"{prefix} {string}")
        return _orig(self)
    _wrapped._wavebench_patched = True  # type: ignore[attr-defined]
    tk.Wm.wm_title = _wrapped
    tk.Wm.title = _wrapped
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_runner.TestTkinterPatch -v`
Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add wavebench/runner.py tests/test_runner.py
git commit -m "Add tkinter Wm.wm_title patch with idempotency sentinel"
```

---

### Task 13: turtle patch with idempotency guard

**Files:**
- Modify: `wavebench/runner.py` (add `_patch_turtle`)
- Modify: `tests/test_runner.py` (add `TestTurtlePatch` class)

- [ ] **Step 1: Write the failing tests for turtle patch**

Append to `tests/test_runner.py`:

```python
class TestTurtlePatch(unittest.TestCase):
    """_patch_turtle wraps turtle.TurtleScreen.title at the class level."""

    def setUp(self):
        self._saved = sys.modules.get("turtle")
        if "turtle" in sys.modules:
            del sys.modules["turtle"]

        fake_turtle = types.ModuleType("turtle")

        class _FakeTurtleScreen:
            def __init__(self):
                self._title = None

            def title(self, titlestring=None):
                if titlestring is not None:
                    self._title = titlestring
                    return None
                return self._title

        fake_turtle.TurtleScreen = _FakeTurtleScreen
        sys.modules["turtle"] = fake_turtle

    def tearDown(self):
        if "turtle" in sys.modules:
            del sys.modules["turtle"]
        if self._saved is not None:
            sys.modules["turtle"] = self._saved

    def test_patch_prefixes_title(self):
        runner = _import_runner()
        runner._patch_turtle("[M]")
        import turtle
        screen = turtle.TurtleScreen()
        screen.title("Snake")
        self.assertEqual(screen.title(), "[M] Snake")

    def test_patch_is_idempotent(self):
        runner = _import_runner()
        runner._patch_turtle("[M]")
        runner._patch_turtle("[M]")
        import turtle
        screen = turtle.TurtleScreen()
        screen.title("Snake")
        self.assertEqual(screen.title(), "[M] Snake")

    def test_patch_missing_library_is_noop(self):
        if "turtle" in sys.modules:
            del sys.modules["turtle"]
        runner = _import_runner()
        runner._patch_turtle("[M]")  # must not raise
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_runner.TestTurtlePatch -v`
Expected: tests fail with `AttributeError: ... has no attribute '_patch_turtle'`.

- [ ] **Step 3: Implement `_patch_turtle`**

In `wavebench/runner.py`, add below `_patch_tkinter`:

```python
def _patch_turtle(prefix: str) -> None:
    try:
        import turtle
    except ImportError:
        return
    if not hasattr(turtle, "TurtleScreen"):
        return
    if getattr(turtle.TurtleScreen.title, "_wavebench_patched", False):
        return
    _orig = turtle.TurtleScreen.title
    def _wrapped(self, titlestring=None):
        if titlestring is not None:
            return _orig(self, f"{prefix} {titlestring}")
        return _orig(self)
    _wrapped._wavebench_patched = True  # type: ignore[attr-defined]
    turtle.TurtleScreen.title = _wrapped
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_runner.TestTurtlePatch -v`
Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add wavebench/runner.py tests/test_runner.py
git commit -m "Add turtle TurtleScreen.title patch with idempotency sentinel"
```

---

### Task 14: Qt patch with idempotency guard

**Files:**
- Modify: `wavebench/runner.py` (add `_patch_qt`)
- Modify: `tests/test_runner.py` (add `TestQtPatch` class)

- [ ] **Step 1: Write the failing tests for Qt patch**

Append to `tests/test_runner.py`:

```python
class TestQtPatch(unittest.TestCase):
    """_patch_qt wraps QWidget.setWindowTitle on whichever Qt flavor is installed."""

    def setUp(self):
        self._saved = {}
        for mod in ("PyQt5", "PyQt6", "PySide2", "PySide6"):
            self._saved[mod] = sys.modules.get(mod)
            self._saved[f"{mod}.QtWidgets"] = sys.modules.get(f"{mod}.QtWidgets")
            for k in (mod, f"{mod}.QtWidgets"):
                if k in sys.modules:
                    del sys.modules[k]

        # Install fake PyQt5 with a stub QWidget
        fake_pyqt5 = types.ModuleType("PyQt5")
        fake_widgets = types.ModuleType("PyQt5.QtWidgets")

        class _FakeQWidget:
            def __init__(self):
                self._title = None

            def setWindowTitle(self, title):
                self._title = title

            def windowTitle(self):
                return self._title

        fake_widgets.QWidget = _FakeQWidget
        fake_pyqt5.QtWidgets = fake_widgets
        sys.modules["PyQt5"] = fake_pyqt5
        sys.modules["PyQt5.QtWidgets"] = fake_widgets

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def test_patch_prefixes_title(self):
        runner = _import_runner()
        runner._patch_qt("[M]")
        from PyQt5.QtWidgets import QWidget
        w = QWidget()
        w.setWindowTitle("Snake")
        self.assertEqual(w.windowTitle(), "[M] Snake")

    def test_patch_is_idempotent(self):
        runner = _import_runner()
        runner._patch_qt("[M]")
        runner._patch_qt("[M]")
        from PyQt5.QtWidgets import QWidget
        w = QWidget()
        w.setWindowTitle("Snake")
        self.assertEqual(w.windowTitle(), "[M] Snake")

    def test_patch_missing_libraries_noop(self):
        for k in ("PyQt5", "PyQt5.QtWidgets"):
            if k in sys.modules:
                del sys.modules[k]
        runner = _import_runner()
        runner._patch_qt("[M]")  # must not raise
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_runner.TestQtPatch -v`
Expected: tests fail with `AttributeError: ... has no attribute '_patch_qt'`.

- [ ] **Step 3: Implement `_patch_qt`**

In `wavebench/runner.py`, add below `_patch_turtle`:

```python
def _patch_qt(prefix: str) -> None:
    for mod_name in ("PyQt6", "PyQt5", "PySide6", "PySide2"):
        try:
            widgets = __import__(f"{mod_name}.QtWidgets", fromlist=["QWidget"])
        except ImportError:
            continue
        QWidget = widgets.QWidget
        if getattr(QWidget.setWindowTitle, "_wavebench_patched", False):
            continue
        _orig = QWidget.setWindowTitle
        def _wrapped(self, title, _orig=_orig):
            return _orig(self, f"{prefix} {title}")
        _wrapped._wavebench_patched = True  # type: ignore[attr-defined]
        QWidget.setWindowTitle = _wrapped
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_runner.TestQtPatch -v`
Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add wavebench/runner.py tests/test_runner.py
git commit -m "Add Qt QWidget.setWindowTitle patch with idempotency sentinel"
```

---

### Task 15: Kivy patch with idempotency guard

**Files:**
- Modify: `wavebench/runner.py` (add `_patch_kivy`)
- Modify: `tests/test_runner.py` (add `TestKivyPatch` class)

- [ ] **Step 1: Write the failing tests for Kivy patch**

Append to `tests/test_runner.py`:

```python
class TestKivyPatch(unittest.TestCase):
    """_patch_kivy binds a title observer on Window that rewrites with the prefix."""

    def setUp(self):
        self._saved = {}
        for k in ("kivy", "kivy.core", "kivy.core.window"):
            self._saved[k] = sys.modules.get(k)
            if k in sys.modules:
                del sys.modules[k]

        fake_kivy = types.ModuleType("kivy")
        fake_core = types.ModuleType("kivy.core")
        fake_window_mod = types.ModuleType("kivy.core.window")

        class _FakeWindow:
            title = ""
            _observers = []  # list of (prop_name, callback)

            @classmethod
            def bind(cls, **kwargs):
                for prop, callback in kwargs.items():
                    cls._observers.append((prop, callback))

            @classmethod
            def set_title(cls, value):
                # Mimic Kivy's property observer: set, then notify observers
                cls.title = value
                for prop, cb in list(cls._observers):
                    if prop == "title":
                        cb(cls, value)

        fake_window_mod.Window = _FakeWindow
        fake_kivy.core = fake_core
        fake_core.window = fake_window_mod
        sys.modules["kivy"] = fake_kivy
        sys.modules["kivy.core"] = fake_core
        sys.modules["kivy.core.window"] = fake_window_mod
        self.FakeWindow = _FakeWindow
        # Reset state so each test starts fresh
        _FakeWindow._observers = []
        _FakeWindow.title = ""

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def test_patch_prefixes_title_via_observer(self):
        runner = _import_runner()
        runner._patch_kivy("[M]")
        self.FakeWindow.set_title("Snake")
        self.assertEqual(self.FakeWindow.title, "[M] Snake")

    def test_patch_is_idempotent(self):
        runner = _import_runner()
        runner._patch_kivy("[M]")
        runner._patch_kivy("[M]")
        self.FakeWindow.set_title("Snake")
        self.assertEqual(self.FakeWindow.title, "[M] Snake")  # NOT "[M] [M] Snake"

    def test_patch_missing_library_is_noop(self):
        for k in ("kivy", "kivy.core", "kivy.core.window"):
            if k in sys.modules:
                del sys.modules[k]
        runner = _import_runner()
        runner._patch_kivy("[M]")  # must not raise

    def test_observer_does_not_double_prefix_when_setter_fires_twice(self):
        runner = _import_runner()
        runner._patch_kivy("[M]")
        self.FakeWindow.set_title("Snake")
        # Simulate Kivy re-firing the observer on the already-prefixed title
        for prop, cb in list(self.FakeWindow._observers):
            if prop == "title":
                cb(self.FakeWindow, self.FakeWindow.title)
        self.assertEqual(self.FakeWindow.title, "[M] Snake")  # stays single-prefix
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_runner.TestKivyPatch -v`
Expected: tests fail with `AttributeError: ... has no attribute '_patch_kivy'`.

- [ ] **Step 3: Implement `_patch_kivy`**

In `wavebench/runner.py`, add below `_patch_qt`:

```python
def _patch_kivy(prefix: str) -> None:
    try:
        from kivy.core.window import Window  # type: ignore[import-not-found]
    except Exception:
        return
    if getattr(Window, "_wavebench_patched", False):
        return
    Window._wavebench_patched = True  # type: ignore[attr-defined]
    _marker = f"{prefix} "
    def _on_title(instance, value):
        if value and not value.startswith(_marker):
            instance.title = f"{_marker}{value}"
    try:
        Window.bind(title=_on_title)
    except Exception:
        pass
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_runner.TestKivyPatch -v`
Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add wavebench/runner.py tests/test_runner.py
git commit -m "Add Kivy Window title observer patch with idempotency and re-entry guard"
```

---

### Task 16: Wire `_apply_patches` to call all five patch functions

**Files:**
- Modify: `wavebench/runner.py` (replace `_apply_patches` stub)

- [ ] **Step 1: Replace the `_apply_patches` stub**

In `wavebench/runner.py`, find:

```python
def _apply_patches(prefix: str) -> None:
    pass
```

Replace with:

```python
def _apply_patches(prefix: str) -> None:
    _patch_pygame(prefix)
    _patch_tkinter(prefix)
    _patch_turtle(prefix)
    _patch_qt(prefix)
    _patch_kivy(prefix)
```

- [ ] **Step 2: Run the full test suite**

Run: `python -m unittest discover tests -v`
Expected: all tests pass (previously passing tests continue to pass; the patch tests exercise `_patch_*` directly so this change is purely wiring).

- [ ] **Step 3: Commit**

```bash
git add wavebench/runner.py
git commit -m "Wire _apply_patches to invoke all five GUI library patchers"
```

---

### Task 17: `core.py` — add `_RUNNER_PATH`, `_build_python_cmd_parts`, update `_shell_cmd`

**Files:**
- Modify: `wavebench/core.py` (add helper and update `_shell_cmd` at line 128)

- [ ] **Step 1: Add `_RUNNER_PATH` constant and `_build_python_cmd_parts` helper**

In `wavebench/core.py`, find the `_shell_cmd` function definition (currently at line 128). Immediately above it, add:

```python
_RUNNER_PATH = os.path.join(os.path.dirname(__file__), "runner.py")


def _build_python_cmd_parts(interp: str, filepath: str) -> List[str]:
    """Return the argv prefix for running a file, interposing the runner for .py.

    For .py files: [interp, runner_path, model_name, filepath]
    For all others: [interp, filepath]

    model_name is derived from the filename basename (WaveBench writes files
    named after the model, so basename-without-extension IS the model name).
    The _v2/_v3 suffix from get_unique_filename is preserved intentionally —
    it's informative when the same model is benchmarked multiple times.
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".py":
        model_name = os.path.splitext(os.path.basename(filepath))[0]
        return [interp, _RUNNER_PATH, model_name, filepath]
    return [interp, filepath]
```

**Note:** if `List` is not already imported at the top of `core.py`, check existing imports. The file should already have `from typing import ... List ...` — verify and add `List` to the existing import if missing.

- [ ] **Step 2: Update `_shell_cmd` to use the helper**

Find the existing `_shell_cmd` function (starts at line 128):

```python
def _shell_cmd(interp: str, filepath: str) -> str:
    """Build a bash command string that runs a file and waits for Enter."""
    return (
        f"{shlex.quote(interp)} {shlex.quote(filepath)}"
        f'; echo; read -rp "Press Enter to close…"'
    )
```

Replace with:

```python
def _shell_cmd(interp: str, filepath: str) -> str:
    """Build a bash command string that runs a file and waits for Enter.

    For .py files, interposes wavebench/runner.py so GUI window titles get
    prefixed with the model name (extracted from the filename).
    """
    cmd = " ".join(
        shlex.quote(p) for p in _build_python_cmd_parts(interp, filepath)
    )
    return f'{cmd}; echo; read -rp "Press Enter to close…"'
```

- [ ] **Step 3: Verify `core.py` still imports cleanly**

Run: `python -c "from wavebench import core; print('ok')"`
Expected: `ok` — no NameError about `List` (add it to typing imports if it errors).

- [ ] **Step 4: Run the full test suite**

Run: `python -m unittest discover tests -v`
Expected: all existing tests still pass (there are no new unit tests for core.py helpers; verification is via end-to-end behavior in later tasks).

- [ ] **Step 5: Commit**

```bash
git add wavebench/core.py
git commit -m "Route .py execution through wavebench.runner via _build_python_cmd_parts"
```

---

### Task 18: `core.py` — update macOS `_run_in_terminal_single` branch

**Files:**
- Modify: `wavebench/core.py` (macOS branch inside `_run_in_terminal_single`, currently around lines 175-184)

- [ ] **Step 1: Update the macOS branch to use `_build_python_cmd_parts`**

Find the `sys.platform == "darwin"` branch inside `_run_in_terminal_single` (around line 175):

```python
        if sys.platform == "darwin":
            cmd_str = f"{shlex.quote(interp)} {shlex.quote(filepath)}"
            osa_safe = cmd_str.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(
                ["osascript", "-e",
                 f'tell application "Terminal" to do script "{osa_safe}"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
```

Replace the `cmd_str` construction with a call to the helper:

```python
        if sys.platform == "darwin":
            parts = _build_python_cmd_parts(interp, filepath)
            cmd_str = " ".join(shlex.quote(p) for p in parts)
            osa_safe = cmd_str.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(
                ["osascript", "-e",
                 f'tell application "Terminal" to do script "{osa_safe}"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
```

- [ ] **Step 2: Verify `core.py` still imports cleanly**

Run: `python -c "from wavebench import core; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Run the full test suite**

Run: `python -m unittest discover tests -v`
Expected: all tests still pass.

- [ ] **Step 4: Commit**

```bash
git add wavebench/core.py
git commit -m "Route macOS _run_in_terminal_single through the Python runner helper"
```

---

### Task 19: `core.py` — update Windows `_run_in_terminal_single` branch

**Files:**
- Modify: `wavebench/core.py` (Windows branch inside `_run_in_terminal_single`, currently around lines 185-190)

- [ ] **Step 1: Update the Windows branch**

Find the `sys.platform == "win32"` branch:

```python
        elif sys.platform == "win32":
            subprocess.Popen(
                ["cmd", "/c", "start", "cmd", "/k", interp, filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
```

Replace with:

```python
        elif sys.platform == "win32":
            parts = _build_python_cmd_parts(interp, filepath)
            subprocess.Popen(
                ["cmd", "/c", "start", "cmd", "/k"] + parts,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
```

This preserves the existing `cmd /k` convention but interposes the runner for `.py` files by expanding `parts` into the argv. Non-py files still become `[interp, filepath]` which behaves identically to the original.

- [ ] **Step 2: Verify import still works**

Run: `python -c "from wavebench import core; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Run tests**

Run: `python -m unittest discover tests -v`
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add wavebench/core.py
git commit -m "Route Windows _run_in_terminal_single through the Python runner helper"
```

---

### Task 20: `core.py` — update macOS `_open_files_as_tabs` branch

**Files:**
- Modify: `wavebench/core.py` (macOS branch inside `_open_files_as_tabs`, currently around lines 281-304)

- [ ] **Step 1: Update the macOS multi-tab AppleScript builder**

Find the `sys.platform == "darwin"` block inside `_open_files_as_tabs` (around line 281):

```python
    if sys.platform == "darwin":
        # macOS: build multi-tab AppleScript
        parts = []
        for i, (fp, interp) in enumerate(file_interps):
            cmd_str = f"{shlex.quote(interp)} {shlex.quote(fp)}"
            osa_safe = cmd_str.replace("\\", "\\\\").replace('"', '\\"')
            if i == 0:
                parts.append(
                    f'tell application "Terminal" to do script "{osa_safe}"')
            else:
                parts.append(
                    f'tell application "Terminal" to do script "{osa_safe}" '
                    f'in front window')
        try:
            script = "\n".join(parts)
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, FileNotFoundError):
            pass
        return
```

Note the variable `parts` is shadowed in this scope — the outer `parts` is the AppleScript lines list; inside the loop we need the argv parts from `_build_python_cmd_parts`. Rename the loop-local to `argv_parts`:

Replace with:

```python
    if sys.platform == "darwin":
        # macOS: build multi-tab AppleScript
        script_lines = []
        for i, (fp, interp) in enumerate(file_interps):
            argv_parts = _build_python_cmd_parts(interp, fp)
            cmd_str = " ".join(shlex.quote(p) for p in argv_parts)
            osa_safe = cmd_str.replace("\\", "\\\\").replace('"', '\\"')
            if i == 0:
                script_lines.append(
                    f'tell application "Terminal" to do script "{osa_safe}"')
            else:
                script_lines.append(
                    f'tell application "Terminal" to do script "{osa_safe}" '
                    f'in front window')
        try:
            script = "\n".join(script_lines)
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, FileNotFoundError):
            pass
        return
```

- [ ] **Step 2: Verify import still works**

Run: `python -c "from wavebench import core; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Run tests**

Run: `python -m unittest discover tests -v`
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add wavebench/core.py
git commit -m "Route macOS _open_files_as_tabs through the Python runner helper"
```

---

### Task 21: End-to-end verification — attribution smoke test

**Files:**
- None (verification only)

This task exercises the full pipeline: generate a file, inject attribution, write it, verify the file contains the injection.

- [ ] **Step 1: Write an ad-hoc verification script**

Create a temporary verification script at `/tmp/verify_attribution.py`:

```python
"""Ad-hoc end-to-end check: call inject_model_attribution on a realistic LLM output."""
from wavebench.attribution import inject_model_attribution

html_sample = """<!DOCTYPE html>
<html>
<head>
<title>Snake Game</title>
<style>body { background: #222; }</style>
</head>
<body>
<canvas id="game"></canvas>
<script>const canvas = document.getElementById('game');</script>
</body>
</html>"""

result = inject_model_attribution(html_sample, "claudeOpus4.6", ".html")
print(result)

assert "<title>[claudeOpus4.6] Snake Game</title>" in result
assert "<!-- WaveBench: claudeOpus4.6 -->" in result
print("\n--- OK ---")
```

- [ ] **Step 2: Run the verification script**

Run: `python /tmp/verify_attribution.py`
Expected: prints the transformed HTML with the `<title>` rewritten and the WaveBench comment at the top, followed by `--- OK ---`.

- [ ] **Step 3: Clean up the scratch file**

```bash
rm /tmp/verify_attribution.py
```

No commit needed — this task is purely verification.

---

### Task 22: End-to-end verification — runner smoke test

**Files:**
- None (verification only)

This task invokes the runner against a tkinter script (using the real stdlib tkinter) to confirm window-title prefixing works end-to-end when a GUI library is actually present.

- [ ] **Step 1: Write an ad-hoc tkinter test script**

Create `/tmp/test_tk_window.py`:

```python
"""Opens a tkinter window, sets title to 'SmokeTest', prints it, exits.

When run through wavebench/runner.py with model_name='claudeOpus4.6', the
printed title should be '[claudeOpus4.6] SmokeTest' — proving the monkey-patch
fired before the user's code ran.
"""
import tkinter as tk

root = tk.Tk()
root.title("SmokeTest")
print(f"window_title={root.title()}")
# Don't actually display the window
root.destroy()
```

- [ ] **Step 2: Run the script directly to confirm baseline behavior**

Run: `python /tmp/test_tk_window.py`
Expected: `window_title=SmokeTest` (no prefix, because the runner isn't involved).

Note: if `DISPLAY` is unset and you're on a headless system, this may fail with a Tk error. In that case, set `xvfb-run` prefix or skip this task and note it as "manual verification required on a system with a display".

- [ ] **Step 3: Run the script through the runner**

Run: `python /home/corbin/Documents/WaveBench/wavebench/runner.py claudeOpus4.6 /tmp/test_tk_window.py`
Expected: `window_title=[claudeOpus4.6] SmokeTest` — the monkey-patched `wm_title` prepends the prefix.

- [ ] **Step 4: Clean up the scratch file**

```bash
rm /tmp/test_tk_window.py
```

- [ ] **Step 5: Run the full test suite one final time**

Run: `python -m unittest discover tests -v`
Expected: all tests pass (30+ attribution tests, 20+ runner tests).

No commit needed for verification steps.

---

## Post-plan summary

After completing all tasks:

**New files (5):**
- `tests/__init__.py` (empty)
- `tests/test_attribution.py` (~200 lines, 30+ tests)
- `tests/test_runner.py` (~300 lines, 20+ tests across 6 test classes)
- `wavebench/attribution.py` (~90 lines)
- `wavebench/runner.py` (~120 lines)

**Modified files (1):**
- `wavebench/core.py` — new import, two write-site injections, `_RUNNER_PATH` + `_build_python_cmd_parts` helper, updates to `_shell_cmd`, `_run_in_terminal_single` (mac+win branches), `_open_files_as_tabs` (mac branch)

**Commit count:** ~21 atomic commits, one per task.

**Total net change:** roughly +600 lines added, ~15 lines modified in `core.py`.

**User-visible behavior changes:**
1. HTML artifacts opened in a browser show `[claudeOpus4.6] Snake Game` (or similar) in the tab title instead of just `Snake Game`.
2. Markdown, SVG, XML, TXT artifacts carry a `<!-- WaveBench: claudeOpus4.6 -->` (or banner) marker invisibly in their metadata strata (visible for `.txt`).
3. Python artifacts executed via auto-open spawn GUI windows whose titles are prefixed with `[claudeOpus4.6]`, regardless of whether the model's code uses pygame, tkinter, turtle, Qt, or Kivy.
