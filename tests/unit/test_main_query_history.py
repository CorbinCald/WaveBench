"""Unit coverage for interactive prompt history persistence."""

from __future__ import annotations

from pathlib import Path


class FakeReadline:
    def __init__(self) -> None:
        self.items: list[str] = []
        self.read_paths: list[Path] = []
        self.write_paths: list[Path] = []
        self.history_length: int | None = None

    def clear_history(self) -> None:
        self.items = []

    def read_history_file(self, path: str) -> None:
        history_path = Path(path)
        self.read_paths.append(history_path)
        self.items = history_path.read_text(encoding="utf-8").splitlines()

    def set_history_length(self, length: int) -> None:
        self.history_length = length

    def add_history(self, query: str) -> None:
        self.items.append(query)

    def write_history_file(self, path: str) -> None:
        history_path = Path(path)
        self.write_paths.append(history_path)
        text = "\n".join(self.items)
        if text:
            text += "\n"
        history_path.write_text(text, encoding="utf-8")


def test_query_history_path_is_mode_specific_and_cwd_scoped(tmp_state_dir: Path) -> None:
    from wavebench import __main__ as main_mod

    assert Path(main_mod._query_history_path("code")) == (
        tmp_state_dir / ".benchmark_query_history.code"
    )
    assert Path(main_mod._query_history_path("image")) == (
        tmp_state_dir / ".benchmark_query_history.image"
    )


def test_load_query_history_uses_selected_mode_and_legacy_code_fallback(
    tmp_state_dir: Path, monkeypatch
) -> None:
    from wavebench import __main__ as main_mod

    fake = FakeReadline()
    monkeypatch.setattr(main_mod, "readline", fake)

    (tmp_state_dir / ".benchmark_query_history").write_text("legacy code\n", encoding="utf-8")
    (tmp_state_dir / ".benchmark_query_history.image").write_text(
        "draw a wave\n", encoding="utf-8"
    )

    main_mod._load_query_history("image")
    assert fake.items == ["draw a wave"]
    assert fake.read_paths[-1] == tmp_state_dir / ".benchmark_query_history.image"
    assert fake.history_length == 500

    main_mod._load_query_history("text")
    assert fake.items == []

    main_mod._load_query_history("code")
    assert fake.items == ["legacy code"]
    assert fake.read_paths[-1] == tmp_state_dir / ".benchmark_query_history"

    (tmp_state_dir / ".benchmark_query_history.code").write_text(
        "specific code\n", encoding="utf-8"
    )
    main_mod._load_query_history("code")
    assert fake.items == ["specific code"]
    assert fake.read_paths[-1] == tmp_state_dir / ".benchmark_query_history.code"


def test_save_query_history_writes_only_selected_mode_file(
    tmp_state_dir: Path, monkeypatch
) -> None:
    from wavebench import __main__ as main_mod

    fake = FakeReadline()
    monkeypatch.setattr(main_mod, "readline", fake)

    (tmp_state_dir / ".benchmark_query_history.image").write_text(
        "old image\n", encoding="utf-8"
    )
    main_mod._load_query_history("image")
    main_mod._save_query_history("new image", "image")

    assert (tmp_state_dir / ".benchmark_query_history.image").read_text(
        encoding="utf-8"
    ) == "old image\nnew image\n"
    assert not (tmp_state_dir / ".benchmark_query_history.code").exists()

    main_mod._load_query_history("code")
    main_mod._save_query_history("new code", "code")

    assert (tmp_state_dir / ".benchmark_query_history.code").read_text(
        encoding="utf-8"
    ) == "new code\n"
    assert (tmp_state_dir / ".benchmark_query_history.image").read_text(
        encoding="utf-8"
    ) == "old image\nnew image\n"


def test_save_query_history_migrates_legacy_code_history_on_first_code_use(
    tmp_state_dir: Path, monkeypatch
) -> None:
    from wavebench import __main__ as main_mod

    fake = FakeReadline()
    monkeypatch.setattr(main_mod, "readline", fake)
    legacy_path = tmp_state_dir / ".benchmark_query_history"
    legacy_path.write_text("legacy code\n", encoding="utf-8")

    main_mod._load_query_history("code")
    main_mod._save_query_history("new code", "code")

    assert (tmp_state_dir / ".benchmark_query_history.code").read_text(
        encoding="utf-8"
    ) == "legacy code\nnew code\n"
    assert legacy_path.read_text(encoding="utf-8") == "legacy code\n"


def test_query_history_helpers_noop_without_readline(tmp_state_dir: Path, monkeypatch) -> None:
    from wavebench import __main__ as main_mod

    monkeypatch.setattr(main_mod, "readline", None)

    main_mod._load_query_history("image")
    main_mod._save_query_history("draw a wave", "image")

    assert not (tmp_state_dir / ".benchmark_query_history.image").exists()
