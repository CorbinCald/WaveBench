from __future__ import annotations

import asyncio
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


def call(call_id, name, **arguments):
    return {"id": call_id, "name": name, "arguments": arguments}


def test_listing_while_another_call_rewrites_a_file(project):
    """Parallel tool calls and subagents list files while a write renames its staged copy."""
    workspace, _ = project
    workspace.write("lib/value.py", "VALUE = 0\n")
    stop = threading.Event()

    def rewrite():
        version = 0
        while not stop.is_set():
            version += 1
            workspace.write("lib/value.py", f"VALUE = {version}\n")

    writer = threading.Thread(target=rewrite)
    writer.start()
    try:
        for _ in range(500):
            assert [f["path"] for f in workspace.tree()["files"]] == ["lib/value.py"]
            assert [entry["name"] for entry in workspace.ls("lib")] == ["value.py"]
    finally:
        stop.set()
        writer.join()


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
        return {"exit_code": 0, "diagnostics": "Checked 0 files; 0 error(s)."}

    dispatcher = Dispatcher(ws, SimpleNamespace(lint=lint), metadata, Limits())
    results = await dispatcher.batch(
        [
            call("0", "write_file", path="a", content="first"),
            call("1", "write_file", path="b", content="other"),
            call("2", "read_file", path="missing"),
            call("3", "read_file", path="a"),
            call("4", "write_file", path="a", content="second"),
            call("5", "lint"),
        ]
    )
    assert [result["id"] for result in results] == [str(i) for i in range(6)]
    assert not results[2]["ok"] and "does not exist" in results[2]["text"]
    assert results[3]["text"] == "first"
    assert results[5]["ok"] and results[5]["text"] == "Checked 0 files; 0 error(s)."
    assert all(results[i]["ok"] for i in (0, 1, 3, 4))
    # b began before the second, conflicting a write.
    assert times["b"] + 0.05 < times["a"]


async def test_replay_submission_order_and_output_limits(project):
    ws, metadata = project

    async def lint():
        return {"exit_code": 1, "diagnostics": "x" * 500}

    dispatcher = Dispatcher(
        ws, SimpleNamespace(lint=lint), metadata, Limits(output_chars=100, read_chars=40)
    )
    write = call("one", "write_file", path="main.py", content="print('ok')\n")
    await dispatcher.batch([write])
    ws.write("main.py", "changed")
    # Replaying a call ID returns its saved result without running it again.
    assert (await dispatcher.batch([write]))[0]["ok"]
    assert ws.read("main.py") == "changed"

    # submit runs after the other calls in its response, and only if they succeeded.
    refused = await dispatcher.batch(
        [
            call("bad", "read_file", path="absent.py"),
            call("s1", "submit", runtime="python", entry="main.py"),
        ]
    )
    assert [r["ok"] for r in refused] == [False, False]
    assert "earlier call in this response failed" in refused[1]["text"]
    assert dispatcher.submission is None
    accepted = await dispatcher.batch(
        [
            call("s2", "submit", runtime="python", entry="main.py"),
            call("w2", "write_file", path="late.py", content="x"),
        ]
    )
    assert accepted[0]["ok"] and dispatcher.submission["entry"] == "main.py"
    assert "already submitted" in accepted[1]["text"] and not (ws.root / "late.py").exists()
    dispatcher.reopen()

    # Long tool output is cut at a line boundary with a visible marker; the record keeps it all.
    lint_result = (await dispatcher.batch([call("lint", "lint")]))[0]
    assert "[Output truncated" in lint_result["text"] and len(lint_result["text"]) < 200
    saved = [json.loads(p.read_text()) for p in sorted(metadata.glob("tool-*.json"))]
    assert saved[-1]["diagnostics"] == "x" * 500 and saved[-1]["tool"] == "lint"

    # Reads return whole lines up to the read allowance and say how to continue.
    ws.write("long.txt", "".join(f"line {n}\n" for n in range(1, 21)))
    first = (await dispatcher.batch([call("r1", "read_file", path="long.txt")]))[0]
    assert first["text"].startswith("[long.txt: lines 1-5 of 20]\nline 1\n")
    assert first["text"].endswith("[Continue with start_line=6.]")
    rest = (await dispatcher.batch([call("r2", "read_file", path="long.txt", start_line=19)]))[0]
    assert rest["text"] == "[long.txt: lines 19-20 of 20]\nline 19\nline 20\n"
    assert dispatcher.tool_usage == {"calls": 8, "failures": 4}


async def test_tool_metrics_update_before_parallel_batch_finishes(project):
    ws, metadata = project
    lint_started, finish_lint = asyncio.Event(), asyncio.Event()
    updates = []

    async def lint():
        lint_started.set()
        await finish_lint.wait()
        return {"exit_code": 1, "diagnostics": "main.py: syntax error"}

    dispatcher = Dispatcher(
        ws, SimpleNamespace(lint=lint), metadata, Limits(), on_tool_result=updates.append
    )
    task = asyncio.create_task(
        dispatcher.batch(
            [
                call("write", "write_file", path="main.py", content="bad"),
                call("read", "read_file", path="missing"),
                call("lint", "lint"),
            ]
        )
    )
    try:
        await asyncio.wait_for(lint_started.wait(), 2)
        assert not task.done()
        assert updates[-1] == {"calls": 2, "failures": 1}
    finally:
        finish_lint.set()
        results = await task
    assert [result["ok"] for result in results] == [True, False, False]
    assert results[2]["text"] == "Lint found problems:\nmain.py: syntax error"
    assert updates[-1] == dispatcher.tool_usage == {"calls": 3, "failures": 2}
    assert [update["calls"] for update in updates] == [1, 2, 3]
    await dispatcher.batch([call("write", "list_files")])
    assert updates[-1] == {"calls": 4, "failures": 3}


async def test_tool_metrics_include_cancelled_and_skipped_calls_once(project):
    ws, metadata = project
    started = asyncio.Event()
    updates = []

    async def lint():
        started.set()
        await asyncio.Event().wait()

    dispatcher = Dispatcher(
        ws, SimpleNamespace(lint=lint), metadata, Limits(), on_tool_result=updates.append
    )
    calls = [call("lint", "lint"), call("queued", "list_files")]
    task = asyncio.create_task(dispatcher.batch(calls))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert updates == [{"calls": 1, "failures": 1}, {"calls": 2, "failures": 2}]
    assert all(not result["ok"] for result in await dispatcher.batch(calls))
    assert len(updates) == 2


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
    assert json.loads(result.stdout)["results"][0]["text"] == text
    edit = json.dumps({"old_text": "missing", "new_text": "x"})
    assert cli("edit", "nested/note.txt", data=edit).returncode == 1
    result = cli("parallel", "read nested/note.txt", "read absent")
    assert len(json.loads(result.stdout)["results"]) == 2 and result.returncode == 1
    listing = json.loads(cli("ls").stdout)["results"][0]["text"]
    assert listing == "nested/note.txt  2 lines"
    batch = [
        {"command": "write", "path": "a.txt", "content": "x\n"},
        {"command": "read", "path": "a.txt"},
    ]
    assert [
        r["text"] for r in json.loads(cli("--json", data=json.dumps(batch)).stdout)["results"]
    ] == [
        "Wrote a.txt (1 line).",
        "x\n",
    ]
    assert cli("delete", "nested").returncode == 0
    assert not (ws.root / "nested").exists()


async def test_provider_filled_optional_fields_mean_unset(project):
    """Strict function calling sends every optional field as "", 0, false, or []."""
    ws, metadata = project
    dispatcher = Dispatcher(ws, None, metadata, Limits())
    results = await dispatcher.batch(
        [
            call("w", "write_file", path="main.py", content="print(42)\n", append=False),
            call("r", "read_file", path="main.py", start_line=0, end_line=0),
            call("l", "list_files", path=""),
            call("e", "edit_file", path="main.py", old_text="42", new_text="43", replace_all=False),
            call("s", "submit", runtime="python", entry="main.py", args=[], preview=""),
        ]
    )
    assert [r["ok"] for r in results] == [True] * 5
    assert results[1]["text"] == "print(42)\n"
    assert results[2]["text"] == "main.py  1 line"
    assert ws.read("main.py") == "print(43)\n"
    assert dispatcher.submission == {
        "runtime": "python",
        "entry": "main.py",
        "args": [],
        "preview": "/",
    }


async def test_readable_errors_for_misused_tools(project):
    ws, metadata = project
    ws.write("app.js", "const a = 1;\nconst b = 2;\nconst a2 = 1;\n")
    dispatcher = Dispatcher(ws, None, metadata, Limits())
    results = await dispatcher.batch(
        [
            call("1", "run_shell", command="ls"),
            call("2", "read_file", path="app.js", command="read"),
            call("3", "edit_file", path="app.js", old_text="const  b = 2;", new_text="x"),
            call("4", "edit_file", path="app.js", old_text="= 1;", new_text="= 3;"),
            call("5", "edit_file", path="app.js", old_text="const b = 2;\\nconst a2", new_text="x"),
            call("6", "write_file", content="no path"),
        ]
    )
    texts = [r["text"] for r in results]
    assert texts[0].startswith("Error: unknown tool 'run_shell'; available tools: read_file")
    assert texts[1] == "Error: read_file does not accept command"
    assert "matches if whitespace is ignored" in texts[2]
    assert "occurs 2 times in app.js (lines 1, 3)" in texts[3]
    assert "escaped \\n sequences" in texts[4]
    assert texts[5] == "Error: write_file requires path"
    assert ws.read("app.js") == "const a = 1;\nconst b = 2;\nconst a2 = 1;\n"
    replaced = await dispatcher.batch(
        [call("7", "edit_file", path="app.js", old_text="= 1;", new_text="= 3;", replace_all=True)]
    )
    assert replaced[0]["text"] == "Edited app.js (2 replacements)."


async def test_append_builds_a_large_file_in_parts(project):
    ws, metadata = project
    dispatcher = Dispatcher(ws, None, metadata, Limits())
    results = await dispatcher.batch(
        [
            call("1", "write_file", path="big.js", content="// part 1\n", append=True),
            call("2", "write_file", path="big.js", content="// part 2\n", append=True),
        ]
    )
    assert [r["text"] for r in results] == [
        "Appended to big.js (now 1 line).",
        "Appended to big.js (now 2 lines).",
    ]
    assert ws.read("big.js") == "// part 1\n// part 2\n"


async def test_batch_limit_returns_skipped_result_for_every_call(project):
    ws, metadata = project
    dispatcher = Dispatcher(ws, None, metadata, Limits(batch_calls=2))
    calls = [call(str(i), "write_file", path=str(i), content="x") for i in range(5)]
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
        [call(path, "write_file", path=path, content="data") for path in ("a", "b")]
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
            call("write", "write_file", path="src//main.py", content="print(42)"),
            call("read", "read_file", path="./src/./main.py"),
        ]
    )
    assert results[1]["text"] == "print(42)"
