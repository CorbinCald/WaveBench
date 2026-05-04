from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from symphony.errors import WorkspaceError
from symphony.models import GitConfig, HooksConfig, Issue
from symphony.workspace import WorkspaceManager, issue_branch_name, sanitize_identifier


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


def test_issue_branch_name_prefers_linear_branch_name() -> None:
    git = GitConfig(enabled=True, repo="https://example.invalid/repo.git")
    issue = Issue("1", "WB-1", "Add thing", "Todo", branch_name="corbin/wb-1-add-thing")

    assert issue_branch_name(issue, git) == "corbin/wb-1-add-thing"


def test_issue_branch_name_generates_safe_fallback() -> None:
    git = GitConfig(enabled=True, repo="https://example.invalid/repo.git", branch_prefix="symphony")
    issue = Issue("1", "WB/1", "Add TTS mode!", "Todo")

    assert issue_branch_name(issue, git) == "symphony/wb-1-add-tts-mode"


@pytest.mark.asyncio
async def test_git_workspace_clones_and_switches_to_issue_branch(tmp_path: Path) -> None:
    remote = _create_remote_repo(tmp_path)
    manager = WorkspaceManager(
        tmp_path / "root",
        git=GitConfig(enabled=True, repo=str(remote), timeout_ms=10_000),
    )
    issue = Issue("1", "WB-1", "Add TTS mode", "Todo")

    workspace = await manager.create_for_issue(issue)

    assert _git(workspace.path, "branch", "--show-current").stdout.strip() == "symphony/wb-1-add-tts-mode"
    assert (workspace.path / "README.md").read_text(encoding="utf-8") == "hello\n"


@pytest.mark.asyncio
async def test_git_workspace_migrates_dirty_main_to_issue_branch(tmp_path: Path) -> None:
    remote = _create_remote_repo(tmp_path)
    manager = WorkspaceManager(
        tmp_path / "root",
        git=GitConfig(enabled=True, repo=str(remote), timeout_ms=10_000),
    )
    path = manager.path_for("WB-2")
    path.mkdir(parents=True)
    _git(path, "clone", str(remote), ".")
    (path / "dirty.txt").write_text("work in progress\n", encoding="utf-8")

    await manager.create_for_issue(Issue("2", "WB-2", "Dirty branch", "Todo"))

    assert _git(path, "branch", "--show-current").stdout.strip() == "symphony/wb-2-dirty-branch"
    assert (path / "dirty.txt").read_text(encoding="utf-8") == "work in progress\n"


@pytest.mark.asyncio
async def test_create_pull_request_commits_pushes_and_uses_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _create_remote_repo(tmp_path)
    gh_log = tmp_path / "gh.log"
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        "echo \"$@\" >> \"$GH_LOG\"\n"
        "if [ \"$1 $2\" = \"pr view\" ]; then exit 1; fi\n"
        "if [ \"$1 $2\" = \"pr create\" ]; then echo https://github.com/example/repo/pull/7; exit 0; fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GH_LOG", str(gh_log))
    manager = WorkspaceManager(
        tmp_path / "root",
        git=GitConfig(
            enabled=True,
            repo=str(remote),
            timeout_ms=10_000,
            push_on_merging=True,
            pr_on_merging=True,
        ),
    )
    issue = Issue("1", "WB-1", "Add TTS mode", "Merging", url="https://linear.app/WB-1")
    workspace = await manager.create_for_issue(issue)
    (workspace.path / "feature.txt").write_text("feature\n", encoding="utf-8")

    result = await manager.create_pull_request_for_issue(issue)

    assert result.pr_url == "https://github.com/example/repo/pull/7"
    assert result.pushed is True
    assert result.created is True
    assert result.ahead == 1
    remote_branches = _git(tmp_path, "--git-dir", str(remote), "branch", "--list").stdout
    assert "symphony/wb-1-add-tts-mode" in remote_branches
    assert "pr create" in gh_log.read_text(encoding="utf-8")


def _create_remote_repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--bare", str(remote))
    seed.mkdir()
    _git(seed, "init")
    _git(seed, "config", "user.name", "Tester")
    _git(seed, "config", "user.email", "tester@example.com")
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "Initial commit")
    _git(seed, "branch", "-M", "main")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    return remote


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
