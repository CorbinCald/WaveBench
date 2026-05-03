from __future__ import annotations

from pathlib import Path

import pytest

from symphony.errors import WorkspaceError
from symphony.models import HooksConfig
from symphony.workspace import WorkspaceManager, sanitize_identifier


@pytest.mark.asyncio
async def test_workspace_create_reuse_and_after_create_once(tmp_path: Path) -> None:
    manager = WorkspaceManager(
        tmp_path / "root",
        HooksConfig(after_create="echo created >> marker.txt", timeout_ms=5000),
    )

    first = await manager.create_for_issue("WB/1 unsafe")
    second = await manager.create_for_issue("WB/1 unsafe")

    assert first.workspace_key == "WB_1_unsafe"
    assert first.created_now is True
    assert second.created_now is False
    assert (first.path / "marker.txt").read_text(encoding="utf-8") == "created\n"


@pytest.mark.asyncio
async def test_before_run_failure_is_fatal(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "root", HooksConfig(before_run="exit 7", timeout_ms=5000))
    workspace = await manager.create_for_issue("WB-1")

    with pytest.raises(WorkspaceError) as excinfo:
        await manager.before_run(workspace.path)

    assert excinfo.value.code == "hook_failed"


@pytest.mark.asyncio
async def test_before_remove_runs_then_deletes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    manager = WorkspaceManager(root, HooksConfig(before_remove="echo removed > ../removed.txt"))
    workspace = await manager.create_for_issue("WB-1")

    await manager.remove("WB-1")

    assert not workspace.path.exists()
    assert (root / "removed.txt").read_text(encoding="utf-8") == "removed\n"


def test_sanitize_identifier_replaces_unsafe_characters() -> None:
    assert sanitize_identifier("WB/1: hello") == "WB_1__hello"
