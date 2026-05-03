"""Workspace path management, hook execution, and safety checks."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path

from symphony.errors import WorkspaceError
from symphony.models import HooksConfig, Workspace

_LOG = logging.getLogger(__name__)
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_identifier(identifier: str) -> str:
    """Derive a filesystem-safe workspace key from an issue identifier."""

    sanitized = _SAFE_KEY_RE.sub("_", identifier)
    return sanitized or "_"


class WorkspaceManager:
    """Create, reuse, hook, and remove per-issue workspaces."""

    def __init__(self, root: Path, hooks: HooksConfig | None = None):
        self.root = root.resolve()
        self.hooks = hooks or HooksConfig()

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

    async def create_for_issue(self, identifier: str) -> Workspace:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(identifier)
        created_now = False
        if path.exists() and not path.is_dir():
            raise WorkspaceError(
                "workspace_path_not_directory", f"workspace path exists and is not a directory: {path}"
            )
        if not path.exists():
            path.mkdir(parents=True)
            created_now = True
        workspace = Workspace(path=path, workspace_key=path.name, created_now=created_now)
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
        except TimeoutError as exc:
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


def _truncate(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...<truncated>"
