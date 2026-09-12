"""Prompt previews stay readable without consuming the live table's rows."""

from __future__ import annotations

import re

import pytest

from wavebench.tui.progress import ProgressTracker


def plain(text):
    return re.sub(r"\033\[[0-9;?]*[a-zA-Z]", "", text)


def test_pasted_prompt_fits_one_row_and_reveals_more_after_resize():
    prompt = "  Build a dashboard.\n\nShow\tCPU and memory usage; refresh every second.  "
    tracker = ProgressTracker(1, {}, prompt=prompt)
    normalized = "Build a dashboard. Show CPU and memory usage; refresh every second."
    previous = ""
    for width in (12, 32, 52, 72, 112):
        preview = plain(tracker._format_prompt(width))
        assert preview.startswith("  PROMPT  ")
        assert len(preview) <= width and len(preview.splitlines()) == 1
        content = preview[10:]
        assert content.rstrip("…").startswith(previous)
        assert normalized.startswith(content.rstrip("…"))
        if len(normalized) > width - 10:
            assert content.endswith("…")
        else:
            assert content == normalized
        previous = content.rstrip("…")


@pytest.mark.parametrize("prompt", ["", " \n\t "])
def test_empty_prompt_omits_the_preview(prompt):
    tracker = ProgressTracker(1, {}, prompt=prompt)
    assert tracker._format_prompt(72) is None


def test_prompt_is_plain_text_and_does_not_depend_on_output_directory():
    prompt = "Print `hello` with **bold labels** and a [link](https://example.com)."
    tracker = ProgressTracker(1, {}, prompt=prompt)
    preview = plain(tracker._format_prompt(112))
    assert preview == "  PROMPT  " + prompt
    tracker.set_output_dir("/tmp/a-long-output-path")
    assert plain(tracker._format_prompt(112)) == preview
