from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from wavebench.harness.commands import Dispatcher
from wavebench.harness.config import Limits
from wavebench.harness.workspace import allocate_project, allocate_run


@pytest.fixture
def project(tmp_path):
    run = allocate_run(tmp_path, "../same prompt", "task")
    workspace, metadata = allocate_project(run, 1, "vendor/model")
    yield workspace, metadata
    workspace.close()


def test_allocations_are_distinct_and_contained(tmp_path):
    with (tmp_path / "historical-artifacts").open("wb") as history:
        history.truncate(256 * 1024 * 1024)
    first = allocate_run(tmp_path, "../..", "same")
    second = allocate_run(tmp_path, "../..", "same")
    assert first != second and first.is_relative_to(tmp_path)
    a, _ = allocate_project(first, 1, "a/b")
    b, _ = allocate_project(first, 2, "a?b")
    try:
        assert a.root != b.root
        a.write("main.py", "secret")
        with pytest.raises(ValueError):
            b.read(str(a.root / "main.py"))
    finally:
        a.close()
        b.close()


def test_nested_file_operations_and_unmatched_edit(project):
    ws, _ = project
    content = 'x = "quoted"\n# $HOME `not a shell`\nprint(x)\n'
    ws.write("src/lib/main.py", content)
    assert ws.read("src/lib/main.py") == content
    assert ws.read("src/lib/main.py", 2, 2) == "# $HOME `not a shell`\n"
    with pytest.raises(ValueError, match="unchanged"):
        ws.edit("src/lib/main.py", "missing", "new")
    assert ws.read("src/lib/main.py") == content
    ws.edit("src/lib/main.py", "print(x)", "print(x.upper())")
    assert "upper()" in ws.read("src/lib/main.py")
    with pytest.raises(ValueError, match="recursive"):
        ws.delete("src")
    ws.delete("src", True)
    assert ws.ls() == []


@pytest.mark.parametrize(
    "path", ["../secret", "/etc/passwd", "src/../../secret", ".wb/deps/file", "a\\b"]
)
def test_all_operations_reject_escape_paths(project, path):
    ws, _ = project
    for operation in (
        lambda: ws.write(path, "x"),
        lambda: ws.read(path),
        lambda: ws.edit(path, "x", "y"),
        lambda: ws.delete(path, True),
        lambda: ws.mkdir(path),
        lambda: ws.ls(path),
    ):
        with pytest.raises((ValueError, OSError)):
            operation()


def test_symlink_hardlink_and_link_replacement_race(project, tmp_path):
    ws, _ = project
    secret = tmp_path / "secret"
    secret.write_text("private")
    (ws.root / "escape").symlink_to(tmp_path, target_is_directory=True)
    (ws.root / "link").symlink_to(secret)
    os.link(secret, ws.root / "hard")
    for path in ("escape/secret", "link", "hard"):
        for operation in (
            lambda path=path: ws.read(path),
            lambda path=path: ws.write(path, "changed"),
        ):
            with pytest.raises((OSError, ValueError)):
                operation()
    assert secret.read_text() == "private"

    stop = threading.Event()
    ws.mkdir("racy")

    def swap():
        while not stop.is_set():
            try:
                (ws.root / "racy").rmdir()
                (ws.root / "racy").symlink_to(tmp_path, target_is_directory=True)
                (ws.root / "racy").unlink()
                (ws.root / "racy").mkdir()
            except OSError:
                pass

    worker = threading.Thread(target=swap)
    worker.start()
    try:
        for _ in range(100):
            try:
                ws.write("racy/secret", "workspace only")
            except (OSError, ValueError):
                pass
    finally:
        stop.set()
        worker.join()
    assert secret.read_text() == "private"


async def test_parallel_overlap_conflicts_and_every_result(project, monkeypatch):
    ws, metadata = project
    times = {}
    original = ws.write

    def write(path, content):
        times[path] = time.monotonic()
        time.sleep(0.08)
        return original(path, content)

    monkeypatch.setattr(ws, "write", write)

    async def lint():
        assert ws.read("a") == "second"
        assert ws.read("b") == "other"
        return {"exit_code": 0}

    dispatcher = Dispatcher(ws, SimpleNamespace(lint=lint), metadata, Limits())
    commands = [
        {"command": "write", "path": "a", "content": "first"},
        {"command": "write", "path": "b", "content": "other"},
        {"command": "read", "path": "missing"},
        {"command": "read", "path": "a"},
        {"command": "write", "path": "a", "content": "second"},
        {"command": "lint"},
    ]
    results = await dispatcher.batch(
        [{"id": str(i), "arguments": command} for i, command in enumerate(commands)]
    )
    assert [result["id"] for result in results] == [str(i) for i in range(6)]
    assert not results[2]["ok"] and results[3]["content"] == "first"
    assert results[5]["ok"] and all(results[i]["ok"] for i in (0, 1, 3, 4))
    # b began before the second, conflicting a write.
    assert times["b"] + 0.05 < times["a"]


async def test_idempotency_done_alone_and_output_limits(project):
    ws, metadata = project
    dispatcher = Dispatcher(ws, None, metadata, Limits(output_chars=100))
    command = {
        "id": "one",
        "arguments": {"command": "write", "path": "main.py", "content": "print('ok')\n"},
    }
    await dispatcher.batch([command])
    ws.write("main.py", "changed")
    assert (await dispatcher.batch([command]))[0]["ok"]
    assert ws.read("main.py") == "changed"
    done = {"id": "done", "arguments": {"command": "done", "runtime": "python", "entry": "main.py"}}
    result = await dispatcher.batch(
        [done, {"id": "two", "arguments": {"command": "delete", "path": "main.py"}}]
    )
    assert all(not r["ok"] for r in result)
    assert dispatcher.submission is None
    assert ws.read("main.py") == "changed"
    ws.write("large", "a" * 200)
    result = (
        await dispatcher.batch([{"id": "read", "arguments": {"command": "read", "path": "large"}}])
    )[0]
    assert result["truncated"]
    assert "a" * 200 in (metadata / result["diagnostics"]).read_text()


def test_real_wb_cli_round_trip(project):
    ws, _ = project

    def cli(*args, data=""):
        return subprocess.run(
            [sys.executable, "-m", "wavebench.harness", "--root", str(ws.root), *args],
            input=data,
            text=True,
            capture_output=True,
            check=False,
        )

    text = "line one ' \"\nline two $(touch /never)\n"
    assert cli("write", "nested/note.txt", data=text).returncode == 0
    result = cli("read", "nested/note.txt")
    assert json.loads(result.stdout)["results"][0]["content"] == text
    assert (
        cli("edit", "nested/note.txt", data=json.dumps({"old": "missing", "new": "x"})).returncode
        == 1
    )
    result = cli("parallel", "read nested/note.txt", "read absent")
    assert len(json.loads(result.stdout)["results"]) == 2 and result.returncode == 1
    assert cli("delete", "nested", "--recursive").returncode == 0
    assert not (ws.root / "nested").exists()


async def test_provider_filled_optional_fields_do_not_change_the_command(project):
    ws, metadata = project
    dispatcher = Dispatcher(ws, None, metadata, Limits())
    arguments = {
        "command": "write",
        "path": "main.py",
        "content": "print(42)\n",
        "start": 1,
        "end": 1,
        "old": "",
        "new": "",
        "recursive": True,
        "runtime": "python",
        "entry": "",
        "args": [],
        "preview": "",
    }
    assert (await dispatcher.batch([{"id": "write", "arguments": arguments}]))[0]["ok"]
    assert ws.read("main.py") == "print(42)\n"
    arguments.update(command="done", entry="main.py")
    assert (await dispatcher.batch([{"id": "done", "arguments": arguments}]))[0]["ok"]
    assert dispatcher.submission["entry"] == "main.py"


async def test_batch_budget_returns_skipped_result_for_every_call(project):
    ws, metadata = project
    dispatcher = Dispatcher(ws, None, metadata, Limits(batch_calls=2))
    calls = [
        {"id": str(i), "arguments": {"command": "write", "path": str(i), "content": "x"}}
        for i in range(5)
    ]
    results = await dispatcher.batch(calls)
    assert [r["id"] for r in results] == [str(i) for i in range(5)]
    assert all("skipped" in r["error"] for r in results[2:])
    assert len(ws.ls()) == 2


async def test_disjoint_file_io_overlaps_after_quota_admission(project, monkeypatch):
    ws, metadata = project
    intervals = []
    original = os.fdopen

    class DelayedWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def write(self, data):
            started = time.monotonic()
            time.sleep(0.1)
            self.stream.write(data)
            intervals.append((started, time.monotonic()))

    def fdopen(fd, mode, *args, **kwargs):
        stream = original(fd, mode, *args, **kwargs)
        return DelayedWriter(stream) if mode == "wb" else stream

    monkeypatch.setattr(os, "fdopen", fdopen)
    dispatcher = Dispatcher(ws, None, metadata, Limits())
    results = await dispatcher.batch(
        [
            {"id": path, "arguments": {"command": "write", "path": path, "content": "data"}}
            for path in ("a", "b")
        ]
    )
    assert all(result["ok"] for result in results)
    assert max(start for start, _ in intervals) < min(end for _, end in intervals)


async def test_equivalent_paths_preserve_write_read_order(project, monkeypatch):
    ws, metadata = project
    original = ws.write

    def delayed(path, content):
        time.sleep(0.05)
        return original(path, content)

    monkeypatch.setattr(ws, "write", delayed)
    dispatcher = Dispatcher(ws, None, metadata, Limits())
    results = await dispatcher.batch(
        [
            {
                "id": "write",
                "arguments": {"command": "write", "path": "src//main.py", "content": "print(42)"},
            },
            {"id": "read", "arguments": {"command": "read", "path": "./src/./main.py"}},
        ]
    )
    assert results[1]["content"] == "print(42)"
