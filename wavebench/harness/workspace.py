"""Descriptor-relative file operations: never follow a model-controlled link."""

from __future__ import annotations

import contextlib
import os
import re
import stat
import sys
import threading
import uuid
from pathlib import Path

DIRECTORY = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PROJECT_BYTES = 128 * 1024 * 1024


def safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")[:60] or "project"


class Workspace:
    def __init__(self, root: Path | str):
        if sys.platform != "linux":
            raise OSError(
                "Harness file tools require Linux; text, TTS and image modes remain available"
            )
        self.root = Path(root).absolute()
        self.fd = os.open(self.root, DIRECTORY)
        self._quota_lock = threading.Lock()
        self._reserved_bytes = 0

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    @staticmethod
    def parts(path: str, *, internal: bool = False) -> list[str]:
        if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
            raise ValueError("use a relative workspace path")
        if len(path.encode("utf-8")) > 4096:
            raise ValueError("path exceeds 4096 bytes")
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("path must stay inside the workspace")
        parts = [p for p in path.split("/") if p and p != "."]
        if len(parts) > 128:
            raise ValueError("path exceeds 128 directory components")
        if parts and parts[0] == ".wb" and not internal:
            raise ValueError(".wb is reserved for isolated runtime data")
        return parts

    @contextlib.contextmanager
    def directory(self, parts: list[str], *, create: bool = False):
        fd = os.dup(self.fd)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd
        finally:
            os.close(fd)

    def mkdir(self, path: str, *, internal: bool = False) -> None:
        with self.directory(self.parts(path, internal=internal), create=True):
            pass

    @contextlib.contextmanager
    def parent(self, path: str, *, create: bool = False):
        parts = self.parts(path)
        if not parts:
            raise ValueError("operation requires a file or subtree, not the workspace root")
        with self.directory(parts[:-1], create=create) as fd:
            yield fd, parts[-1]

    def read(self, path: str, start: int = 1, end: int | None = None) -> str:
        if (
            type(start) is not int
            or start < 1
            or (end is not None and (type(end) is not int or end < start))
        ):
            raise ValueError("range uses positive start/end line numbers (inclusive)")
        with self.parent(path) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("only regular, unlinked project files are readable")
                data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise ValueError("file exceeds the 8 MiB read limit; split the file")
        text = data.decode("utf-8")
        if start == 1 and end is None:
            return text
        return "".join(text.splitlines(keepends=True)[start - 1 : end])

    def ls(self, path: str = ".") -> list[dict]:
        with self.directory(self.parts(path)) as fd:
            entries = []
            for name in sorted(os.listdir(fd)):
                if name == ".wb":
                    continue
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
                if stat.S_ISLNK(info.st_mode):
                    kind = "blocked symlink"
                entries.append({"name": name, "type": kind, "bytes": info.st_size})
            return entries

    def _size(
        self, fd: int, *, include_runtime: bool = False, limit: int | None = None, depth: int = 0
    ) -> int:
        if depth > 128:
            raise ValueError("workspace directory depth budget exceeded")
        total = 0
        for name in os.listdir(fd):
            if name == ".wb" and not include_runtime:
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    total += info.st_size
                    child = os.open(name, DIRECTORY, dir_fd=fd)
                    try:
                        total += self._size(
                            child,
                            include_runtime=include_runtime,
                            limit=None if limit is None else limit - total,
                            depth=depth + 1,
                        )
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    # Include allocation/metadata cost so empty-file floods are bounded too.
                    total += max(512, info.st_size, info.st_blocks * 512)
                else:
                    total += max(512, info.st_size)
            except FileNotFoundError:
                continue  # A running project may replace a temporary file.
            if limit is not None and total > limit:
                break
        return total

    def write(self, path: str, content: str) -> dict:
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("file exceeds 8 MiB")
        with self.parent(path, create=True) as (parent, name):
            previous = 0
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("cannot replace a link, directory, or special file")
                previous = info.st_size
            except FileNotFoundError:
                pass
            with self._quota_lock:
                if (
                    self._size(self.fd) + self._reserved_bytes - previous + len(data)
                    > MAX_PROJECT_BYTES
                ):
                    raise ValueError("project exceeds 128 MiB")
                self._reserved_bytes += len(data)
            temp = f".write-{uuid.uuid4().hex}"
            try:
                fd = os.open(
                    temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                # Atomic replacement never follows a link swapped in after validation.
                os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                with self._quota_lock:
                    self._reserved_bytes -= len(data)
                    try:
                        os.unlink(temp, dir_fd=parent)
                    except FileNotFoundError:
                        pass
        return {"path": path, "bytes": len(data)}

    def edit(self, path: str, old: str, new: str) -> dict:
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError("edit requires nonempty old and string new")
        content = self.read(path)
        if content.count(old) != 1:
            raise ValueError("edit must match exactly once; file unchanged")
        return self.write(path, content.replace(old, new, 1))

    def delete(self, path: str, recursive: bool = False) -> dict:
        def remove(parent: int, name: str) -> None:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("symlink operations are denied")
            if stat.S_ISDIR(info.st_mode):
                if not recursive:
                    raise ValueError("subtree deletion requires recursive=true")
                child = os.open(name, DIRECTORY, dir_fd=parent)
                try:
                    for item in os.listdir(child):
                        remove(child, item)
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=parent)
            elif stat.S_ISREG(info.st_mode):
                os.unlink(name, dir_fd=parent)
            else:
                raise ValueError("special file operations are denied")

        with self.parent(path) as (parent, name):
            remove(parent, name)
        return {"deleted": path}


def allocate_run(base: Path, prompt_name: str, prompt: str) -> Path:
    """Fresh invocation directory, including when names collide or runs overlap."""
    base.mkdir(exist_ok=True)
    workspace = Workspace(base)
    try:
        parent = f"harness/{safe_name(prompt_name)}"
        workspace.mkdir(parent)
        with workspace.directory(workspace.parts(parent)) as fd:
            while True:
                invocation = uuid.uuid4().hex
                try:
                    os.mkdir(invocation, 0o700, dir_fd=fd)
                    break
                except FileExistsError:
                    continue
        relative = f"{parent}/{invocation}"
        # Controller records are not charged to a model's source quota, nor
        # should previous invocations prevent allocation of a fresh project.
        with workspace.directory(workspace.parts(relative)) as fd:
            prompt_fd = os.open(
                "prompt.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
            )
            with os.fdopen(prompt_fd, "w", encoding="utf-8") as stream:
                stream.write(prompt)
            os.mkdir("metadata", 0o700, dir_fd=fd)
    finally:
        workspace.close()
    return base / relative


def allocate_project(run: Path, slot: int, model_name: str) -> tuple[Workspace, Path]:
    owner = Workspace(run)
    try:
        while True:
            model_slot = f"{slot:03d}-{safe_name(model_name)}-{uuid.uuid4().hex[:12]}"
            try:
                os.mkdir(model_slot, 0o700, dir_fd=owner.fd)
                break
            except FileExistsError:
                continue
        relative = f"{model_slot}/project"
        owner.mkdir(relative)
        owner.mkdir(f"metadata/{model_slot}")
    finally:
        owner.close()
    metadata = run / "metadata" / model_slot
    return Workspace(run / relative), metadata
