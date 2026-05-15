"""Workspace path management, native git branching, hook execution, and safety checks."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from symphony.errors import WorkspaceError
from symphony.models import GitConfig, HooksConfig, Issue, PullRequestResult, Workspace

_LOG = logging.getLogger(__name__)
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]")
_BRANCH_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._/-]+")
_META_EXCLUDE = ".symphony-meta.json"
_FETCH_RETRY_DELAYS_SECONDS = (1.0, 2.0)


@dataclass(slots=True)
class _CommandResult:
    returncode: int
    stdout: str
    stderr: str


def sanitize_identifier(identifier: str) -> str:
    """Derive a filesystem-safe workspace key from an issue identifier."""

    sanitized = _SAFE_KEY_RE.sub("_", identifier)
    return sanitized or "_"


def issue_branch_name(issue: Issue, git: GitConfig) -> str:
    """Return the git branch Symphony should use for *issue*."""

    fallback = _sanitize_branch_name(
        f"{git.branch_prefix}/{_slug(issue.identifier)}-{_slug(issue.title)}",
        f"{git.branch_prefix}/{_slug(issue.identifier)}",
    )
    if issue.branch_name:
        branch = _sanitize_branch_name(issue.branch_name, fallback)
        if branch and branch != git.base_branch and branch != f"{git.remote}/{git.base_branch}":
            return branch
    return fallback


class WorkspaceManager:
    """Create, reuse, hook, branch, and remove per-issue workspaces."""

    def __init__(
        self,
        root: Path,
        hooks: HooksConfig | None = None,
        git: GitConfig | None = None,
    ):
        self.root = root.resolve()
        self.hooks = hooks or HooksConfig()
        self.git = git or GitConfig()

    def path_for(self, identifier: str) -> Path:
        path = (self.root / sanitize_identifier(identifier)).resolve()
        self.assert_inside_root(path)
        return path

    def assert_inside_root(self, path: Path) -> None:
        try:
            path.resolve().relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(
                "invalid_workspace_path", f"workspace path escapes root: {path}"
            ) from exc

    async def create_for_issue(self, issue_or_identifier: Issue | str) -> Workspace:
        issue = _coerce_issue(issue_or_identifier)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(issue.identifier)
        created_now = False
        if path.exists() and not path.is_dir():
            raise WorkspaceError(
                "workspace_path_not_directory", f"workspace path exists and is not a directory: {path}"
            )
        if not path.exists():
            path.mkdir(parents=True)
            created_now = True
        workspace = Workspace(path=path, workspace_key=path.name, created_now=created_now)
        if self.git.enabled:
            await self._prepare_git_workspace(path, issue, created_now=created_now)
        if created_now and self.hooks.after_create:
            await self._run_hook("after_create", self.hooks.after_create, path, fatal=True)
        return workspace

    async def before_run(self, path: Path) -> None:
        self.assert_inside_root(path)
        if self.hooks.before_run:
            await self._run_hook("before_run", self.hooks.before_run, path, fatal=True)

    async def after_run(self, path: Path) -> None:
        self.assert_inside_root(path)
        if self.hooks.after_run:
            await self._run_hook("after_run", self.hooks.after_run, path, fatal=False)

    async def remove(self, identifier: str) -> None:
        path = self.path_for(identifier)
        if not path.exists():
            return
        if self.hooks.before_remove:
            await self._run_hook("before_remove", self.hooks.before_remove, path, fatal=False)
        shutil.rmtree(path, ignore_errors=False)

    async def create_pull_request_for_issue(self, issue: Issue) -> PullRequestResult:
        """Commit, rebase, push, and create/find a PR for an issue in Merging."""

        if not self.git.enabled:
            raise WorkspaceError("git_disabled", "git must be enabled to create pull requests")
        if not self.git.repo:
            raise WorkspaceError("missing_git_repo", "git.repo is required")
        path = self.path_for(issue.identifier)
        if not path.exists():
            raise WorkspaceError(
                "workspace_missing", f"workspace does not exist for {issue.identifier}: {path}"
            )
        await self._prepare_git_workspace(path, issue, created_now=False)
        branch = issue_branch_name(issue, self.git)
        await self._commit_dirty_worktree(path, issue)
        await self._fetch(path)
        if self.git.rebase_policy == "clean-only":
            await self._rebase_onto_base(path, branch)
        behind, ahead = await self._ahead_behind(path)
        if ahead <= 0:
            raise WorkspaceError(
                "git_no_changes_to_pr",
                f"{issue.identifier} branch {branch} has no commits ahead of {self._base_ref()}",
            )
        pushed = False
        if self.git.push_on_merging or self.git.pr_on_merging:
            await self._git(path, "push", "-u", self.git.remote, branch)
            pushed = True
        pr_url: str | None = None
        created = False
        if self.git.pr_on_merging:
            pr_url, created = await self._create_or_find_pr(path, issue, branch)
        return PullRequestResult(
            branch=branch,
            base_branch=self.git.base_branch,
            pr_url=pr_url,
            pushed=pushed,
            created=created,
            ahead=ahead,
            behind=behind,
        )

    async def _prepare_git_workspace(self, path: Path, issue: Issue, created_now: bool) -> None:
        if not self.git.repo:
            raise WorkspaceError("missing_git_repo", "git.repo is required when git.enabled is true")
        if not await self._is_git_repo(path):
            if not _path_empty(path):
                raise WorkspaceError(
                    "workspace_not_git_repo",
                    f"workspace is not empty and is not a git repository: {path}",
                )
            await self._run_command(
                ["git", "clone", "--origin", self.git.remote, self.git.repo, "."],
                path,
                "git_clone_failed",
            )
        await self._ensure_git_excludes(path)
        await self._fetch(path)
        branch = issue_branch_name(issue, self.git)
        await self._ensure_issue_branch(path, branch)
        if self.git.rebase_policy == "clean-only":
            if await self._worktree_dirty(path):
                _LOG.info(
                    "git_rebase_skipped_dirty issue_identifier=%s branch=%s cwd=%s",
                    issue.identifier,
                    branch,
                    path,
                )
            else:
                await self._rebase_onto_base(path, branch)
        if created_now:
            _LOG.info(
                "git_workspace_created issue_identifier=%s branch=%s base=%s cwd=%s",
                issue.identifier,
                branch,
                self._base_ref(),
                path,
            )

    async def _ensure_issue_branch(self, path: Path, branch: str) -> None:
        current = await self._current_branch(path)
        if current == branch:
            return
        local_exists = await self._branch_exists(path, branch)
        dirty = await self._worktree_dirty(path)
        if local_exists:
            if dirty:
                raise WorkspaceError(
                    "git_dirty_wrong_branch",
                    f"workspace has uncommitted changes on {current}; cannot switch to {branch}",
                )
            await self._git(path, "switch", branch)
            return
        remote_exists = await self._remote_branch_exists(path, branch)
        if remote_exists:
            if dirty:
                raise WorkspaceError(
                    "git_dirty_wrong_branch",
                    f"workspace has uncommitted changes on {current}; cannot switch to {branch}",
                )
            await self._git(path, "switch", "--track", "-c", branch, f"{self.git.remote}/{branch}")
            return
        if dirty:
            # Important migration path for existing Symphony workspaces: if work was
            # accidentally produced on main, create the issue branch at the current
            # HEAD and keep the dirty worktree attached to that new branch.
            await self._git(path, "switch", "-c", branch)
            return
        await self._git(path, "switch", "-c", branch, self._base_ref())

    async def _is_git_repo(self, path: Path) -> bool:
        if not (path / ".git").exists():
            return False
        result = await self._git(path, "rev-parse", "--is-inside-work-tree", check=False)
        return result.returncode == 0 and result.stdout.strip() == "true"

    async def _ensure_git_excludes(self, path: Path) -> None:
        exclude = path / ".git" / "info" / "exclude"
        try:
            text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if _META_EXCLUDE not in text.splitlines():
                with exclude.open("a", encoding="utf-8") as fh:
                    if text and not text.endswith("\n"):
                        fh.write("\n")
                    fh.write(f"{_META_EXCLUDE}\n")
        except OSError as exc:
            raise WorkspaceError("git_exclude_failed", f"could not update {exclude}: {exc}") from exc

    async def _fetch(self, path: Path) -> None:
        last_result: _CommandResult | None = None
        attempts = len(_FETCH_RETRY_DELAYS_SECONDS) + 1
        for attempt in range(1, attempts + 1):
            result = await self._git(path, "fetch", self.git.remote, check=False)
            if result.returncode == 0:
                return
            last_result = result
            if attempt < attempts:
                delay_seconds = _FETCH_RETRY_DELAYS_SECONDS[attempt - 1]
                _LOG.warning(
                    "git_fetch_retrying cwd=%s remote=%s attempt=%s attempts=%s delay_seconds=%s error=%s",
                    path,
                    self.git.remote,
                    attempt,
                    attempts,
                    delay_seconds,
                    (result.stderr or result.stdout).strip(),
                )
                await asyncio.sleep(delay_seconds)
        assert last_result is not None
        raise WorkspaceError(
            "git_fetch_failed",
            f"command exited {last_result.returncode}: git fetch {self.git.remote}: "
            f"{(last_result.stderr or last_result.stdout).strip()}",
        )

    async def _current_branch(self, path: Path) -> str:
        result = await self._git(path, "branch", "--show-current")
        return result.stdout.strip() or "HEAD"

    async def _branch_exists(self, path: Path, branch: str) -> bool:
        result = await self._git(
            path,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        )
        return result.returncode == 0

    async def _remote_branch_exists(self, path: Path, branch: str) -> bool:
        result = await self._git(
            path,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/remotes/{self.git.remote}/{branch}",
            check=False,
        )
        return result.returncode == 0

    async def has_reviewable_changes_for_issue(self, issue: Issue) -> bool:
        """Return whether an issue workspace has changes worth sending to review.

        For git-backed workspaces, a reviewable change is either a dirty worktree
        or commits ahead of the configured base branch. Non-git workspaces keep
        the historical behavior because Symphony has no reliable diff source.
        """

        if not self.git.enabled:
            return True
        path = self.path_for(issue.identifier)
        if not path.exists():
            return False
        if not await self._is_git_repo(path):
            return False
        if await self._worktree_dirty(path):
            return True
        _behind, ahead = await self._ahead_behind(path)
        return ahead > 0

    async def _worktree_dirty(self, path: Path) -> bool:
        result = await self._git(path, "status", "--porcelain")
        return bool(result.stdout.strip())

    async def _commit_dirty_worktree(self, path: Path, issue: Issue) -> None:
        if not await self._worktree_dirty(path):
            return
        await self._git(path, "add", "-A")
        staged = await self._git(path, "diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            return
        if staged.returncode != 1:
            raise WorkspaceError(
                "git_diff_failed", (staged.stderr or staged.stdout or "git diff failed").strip()
            )
        identity_args: list[str] = []
        if not await self._git_config_value(path, "user.name"):
            identity_args.extend(["-c", "user.name=Symphony"])
        if not await self._git_config_value(path, "user.email"):
            identity_args.extend(["-c", "user.email=symphony@localhost"])
        message = f"{issue.identifier}: {issue.title}".strip()
        await self._git(path, *identity_args, "commit", "-m", message)

    async def _git_config_value(self, path: Path, key: str) -> str | None:
        result = await self._git(path, "config", "--get", key, check=False)
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value or None

    async def _rebase_onto_base(self, path: Path, branch: str) -> None:
        result = await self._git(path, "rebase", self._base_ref(), check=False)
        if result.returncode == 0:
            return
        await self._git(path, "rebase", "--abort", check=False)
        raise WorkspaceError(
            "git_rebase_failed",
            f"could not rebase {branch} onto {self._base_ref()}: "
            f"{(result.stderr or result.stdout).strip()}",
        )

    async def _ahead_behind(self, path: Path) -> tuple[int, int]:
        result = await self._git(path, "rev-list", "--left-right", "--count", f"{self._base_ref()}...HEAD")
        parts = result.stdout.strip().split()
        if len(parts) != 2:
            raise WorkspaceError("git_rev_list_failed", f"unexpected rev-list output: {result.stdout!r}")
        return int(parts[0]), int(parts[1])

    async def _create_or_find_pr(self, path: Path, issue: Issue, branch: str) -> tuple[str, bool]:
        existing = await self._gh(
            path,
            "pr",
            "view",
            "--head",
            branch,
            "--json",
            "url",
            "--jq",
            ".url",
            check=False,
        )
        if existing.returncode == 0 and existing.stdout.strip():
            return existing.stdout.strip().splitlines()[-1], False
        title = f"{issue.identifier}: {issue.title}".strip()
        body_lines = [
            f"Linear issue: {issue.url or issue.identifier}",
            "",
            f"Branch: `{branch}`",
            f"Base: `{self.git.base_branch}`",
            "",
            "Created by Symphony after the Linear issue was moved to Merging.",
        ]
        created = await self._gh(
            path,
            "pr",
            "create",
            "--base",
            self.git.base_branch,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            "\n".join(body_lines),
        )
        url = _extract_url(created.stdout.strip())
        if not url:
            raise WorkspaceError("gh_pr_create_failed", f"could not parse PR URL: {created.stdout!r}")
        return url, True

    async def _git(self, path: Path, *args: str, check: bool = True) -> _CommandResult:
        return await self._run_command(["git", *args], path, f"git_{args[0]}_failed", check=check)

    async def _gh(self, path: Path, *args: str, check: bool = True) -> _CommandResult:
        return await self._run_command(["gh", *args], path, f"gh_{args[0]}_failed", check=check)

    async def _run_command(
        self,
        command: list[str],
        cwd: Path,
        error_code: str,
        check: bool = True,
    ) -> _CommandResult:
        self.assert_inside_root(cwd)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.git.timeout_ms / 1000
            )
        except FileNotFoundError as exc:
            raise WorkspaceError(error_code, f"command not found: {command[0]}") from exc
        except asyncio.TimeoutError as exc:
            raise WorkspaceError(
                error_code,
                f"command timed out after {self.git.timeout_ms}ms: {' '.join(command)}",
            ) from exc
        result = _CommandResult(
            process.returncode,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )
        if check and result.returncode != 0:
            raise WorkspaceError(
                error_code,
                f"command exited {result.returncode}: {' '.join(command)}: "
                f"{(result.stderr or result.stdout).strip()}",
            )
        return result

    def _base_ref(self) -> str:
        return f"{self.git.remote}/{self.git.base_branch}"

    async def _run_hook(self, name: str, script: str, cwd: Path, fatal: bool) -> None:
        _LOG.info("hook_start name=%s cwd=%s", name, cwd)
        try:
            process = await asyncio.create_subprocess_exec(
                "sh",
                "-lc",
                script,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.hooks.timeout_ms / 1000
            )
        except asyncio.TimeoutError as exc:
            message = f"hook {name} timed out after {self.hooks.timeout_ms}ms"
            _LOG.warning("hook_timeout name=%s cwd=%s", name, cwd)
            if fatal:
                raise WorkspaceError("hook_timeout", message) from exc
            return
        except OSError as exc:
            message = f"hook {name} failed to start: {exc}"
            _LOG.warning("hook_start_failed name=%s cwd=%s error=%s", name, cwd, exc)
            if fatal:
                raise WorkspaceError("hook_start_failed", message) from exc
            return

        out = _truncate(stdout.decode(errors="replace"))
        err = _truncate(stderr.decode(errors="replace"))
        if process.returncode != 0:
            message = f"hook {name} exited {process.returncode}: {err or out}"
            _LOG.warning(
                "hook_failed name=%s cwd=%s returncode=%s stdout=%r stderr=%r",
                name,
                cwd,
                process.returncode,
                out,
                err,
            )
            if fatal:
                raise WorkspaceError("hook_failed", message)
            return
        _LOG.info("hook_completed name=%s cwd=%s stdout=%r stderr=%r", name, cwd, out, err)


def _coerce_issue(issue_or_identifier: Issue | str) -> Issue:
    if isinstance(issue_or_identifier, Issue):
        return issue_or_identifier
    identifier = str(issue_or_identifier)
    return Issue(id="", identifier=identifier, title=identifier, state="")


def _path_empty(path: Path) -> bool:
    return not any(path.iterdir())


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:60].strip("-") or "issue"


def _sanitize_branch_name(raw: str, fallback: str) -> str:
    branch = _BRANCH_UNSAFE_RE.sub("-", raw.strip().replace("\\", "/"))
    branch = re.sub(r"/+", "/", branch)
    branch = re.sub(r"\.\.+", ".", branch)
    parts = [part.strip(".-") for part in branch.split("/") if part.strip(".-")]
    branch = "/".join(parts)
    if branch.endswith(".lock"):
        branch = branch[: -len(".lock")].rstrip(".-/")
    if not branch or branch == "@":
        return _sanitize_branch_name(fallback, "symphony/issue") if raw != fallback else "symphony/issue"
    return branch


def _extract_url(text: str) -> str | None:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("http://") or stripped.startswith("https://"):
            return stripped
    return None


def _truncate(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...<truncated>"
