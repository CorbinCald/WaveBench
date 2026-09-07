"""Prompt history survives switching Python installations and readline backends."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wavebench import __main__ as main_mod
from wavebench import query_history

# Written by Python's libedit readline backend with write_history_file().
LIBEDIT_HISTORY = (
    b"_HiStOrY_V2_\n"
    b"\\040halo\\040piston\n"
    b"Keep\\040literal\\040\\134040\\040and\\040\\134n\\040and\\040C:\\134new\\134test\n"
    + "café\\040日本語\\040👋\n".encode()
    + b"one\\011two\\012three\\^Mfour\n"
    b"controls:\\040\\^A\\^G\\^H\\^K\\^L\\^[\\^?\n"
)
LIBEDIT_PROMPTS = [
    " halo piston",
    r"Keep literal \040 and \n and C:\new\test",
    "café 日本語 👋",
    "one\ttwo\nthree\rfour",
    "controls: \x01\x07\x08\x0b\x0c\x1b\x7f",
]


@pytest.mark.parametrize("mode", ["code", "harness", "text", "tts", "image"])
def test_query_history_path_is_mode_specific_and_cwd_scoped(tmp_state_dir: Path, mode) -> None:
    suffix = "code" if mode == "harness" else mode
    assert Path(main_mod._query_history_path(mode)) == (
        tmp_state_dir / f".benchmark_query_history.{suffix}.json"
    )
    assert main_mod._load_query_history(mode) == []


def test_load_query_history_uses_selected_mode_and_legacy_code_fallback(
    tmp_state_dir: Path,
) -> None:
    (tmp_state_dir / ".benchmark_query_history").write_text("legacy code\n", encoding="utf-8")
    (tmp_state_dir / ".benchmark_query_history.image").write_text("draw a wave\n", encoding="utf-8")

    assert main_mod._load_query_history("image") == ["draw a wave"]
    assert main_mod._load_query_history("text") == []
    assert main_mod._load_query_history("harness") == ["legacy code"]

    (tmp_state_dir / ".benchmark_query_history.code").write_text(
        "specific code\n", encoding="utf-8"
    )
    assert main_mod._load_query_history("code") == ["specific code"]
    assert main_mod._load_query_history("harness") == ["specific code"]


@pytest.mark.parametrize(
    "legacy_name", [".benchmark_query_history", ".benchmark_query_history.code"]
)
def test_libedit_import_preserves_prompts_and_original_file(
    tmp_state_dir: Path, legacy_name
) -> None:
    legacy = tmp_state_dir / legacy_name
    legacy.write_bytes(LIBEDIT_HISTORY)

    assert main_mod._load_query_history("harness") == LIBEDIT_PROMPTS
    main_mod._save_query_history("new prompt", "harness")

    assert main_mod._load_query_history("harness") == [*LIBEDIT_PROMPTS, "new prompt"]
    assert main_mod._load_query_history("code") == [*LIBEDIT_PROMPTS, "new prompt"]
    assert legacy.read_bytes() == LIBEDIT_HISTORY
    saved = json.loads(Path(main_mod._query_history_path("harness")).read_text(encoding="utf-8"))
    assert saved == {"version": 1, "entries": [*LIBEDIT_PROMPTS, "new prompt"]}


def test_libedit_vis_encoded_utf8(tmp_path: Path) -> None:
    history = tmp_path / "history"
    history.write_bytes(b"_HiStOrY_V2_\ncaf\\M-C\\M-)\\040\\303\\251\n")
    assert query_history.load(str(history)) == ["café é"]


def test_plain_readline_history_does_not_decode_literal_escapes(tmp_state_dir: Path) -> None:
    prompts = [r"\040halo\040piston", r"C:\new\test", '{"version": 1}', "café 日本語 👋"]
    legacy = tmp_state_dir / ".benchmark_query_history.image"
    original = ("\r\n".join(prompts) + "\r\n").encode()
    legacy.write_bytes(original)

    assert main_mod._load_query_history("image") == prompts
    main_mod._save_query_history(r"another \040", "image")
    assert main_mod._load_query_history("image") == [*prompts, r"another \040"]
    assert legacy.read_bytes() == original
    assert main_mod._load_query_history("code") == []
    assert not Path(main_mod._query_history_path("code")).exists()


def test_json_history_takes_precedence_and_save_reads_latest_entries(tmp_state_dir: Path) -> None:
    main_mod._save_query_history("first prompt", "text")
    assert main_mod._load_query_history("text") == ["first prompt"]
    (tmp_state_dir / ".benchmark_query_history.text").write_bytes(LIBEDIT_HISTORY)

    main_mod._save_query_history("second prompt", "text")
    main_mod._save_query_history("third prompt", "text")

    assert main_mod._load_query_history("text") == ["first prompt", "second prompt", "third prompt"]
    main_mod._save_query_history("a separate image prompt", "image")
    assert main_mod._load_query_history("image") == ["a separate image prompt"]
    assert main_mod._load_query_history("text") == ["first prompt", "second prompt", "third prompt"]


def test_history_limits_recalled_and_saved_entries(tmp_state_dir: Path) -> None:
    prompts = [f"prompt {i}" for i in range(510)]
    (tmp_state_dir / ".benchmark_query_history.code").write_text(
        "\n".join(prompts) + "\n", encoding="utf-8"
    )
    assert main_mod._load_query_history("harness") == prompts[-500:]

    main_mod._save_query_history("new prompt", "harness")
    assert main_mod._load_query_history("harness") == [*prompts[-499:], "new prompt"]
    assert len(json.loads(Path(main_mod._query_history_path()).read_bytes())["entries"]) == 500


def test_empty_prompt_does_not_create_history(tmp_state_dir: Path) -> None:
    main_mod._save_query_history("", "text")
    assert not Path(main_mod._query_history_path("text")).exists()


@pytest.mark.parametrize(
    "contents",
    [
        b"{broken",
        b'{"version": 2, "entries": ["keep me"]}',
        b'{"version": 1, "entries": [2]}',
        b"[]",
    ],
)
def test_unreadable_json_history_is_not_overwritten(tmp_state_dir: Path, contents: bytes) -> None:
    path = Path(main_mod._query_history_path())
    path.write_bytes(contents)
    assert main_mod._load_query_history() == []
    main_mod._save_query_history("new prompt")
    assert path.read_bytes() == contents
    assert not list(tmp_state_dir.glob("*.tmp"))


def test_unreadable_legacy_history_is_not_replaced(tmp_state_dir: Path) -> None:
    legacy = tmp_state_dir / ".benchmark_query_history.code"
    legacy.write_bytes(b"_HiStOrY_V2_\ninvalid UTF-8: \xff\n")
    assert main_mod._load_query_history() == []
    main_mod._save_query_history("new prompt")
    assert not Path(main_mod._query_history_path()).exists()
    assert legacy.read_bytes() == b"_HiStOrY_V2_\ninvalid UTF-8: \xff\n"


def test_failed_history_write_preserves_previous_file(tmp_state_dir: Path, monkeypatch) -> None:
    main_mod._save_query_history("keep me")
    path = main_mod._query_history_path()
    original = Path(path).read_bytes()

    def fail_replace(*_args):
        raise OSError("simulated write failure")

    monkeypatch.setattr(query_history.os, "replace", fail_replace)
    assert not query_history.save(path, "new prompt", source=path)
    assert Path(path).read_bytes() == original
    assert not list(tmp_state_dir.glob("*.tmp"))
