from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
import pytest

from wavebench.harness.commands import launch_descriptor
from wavebench.harness.config import Limits
from wavebench.harness.preview import PreviewIdentity
from wavebench.harness.runtime import Runtime, SetupError
from wavebench.harness.workspace import allocate_project, allocate_run


@pytest.fixture
async def runtime(tmp_path):
    if sys.platform != "linux" or not shutil.which("bwrap"):
        if os.getenv("WAVEBENCH_REQUIRE_SANDBOX_TESTS"):
            pytest.fail("sandbox checks are required but bwrap is missing")
        pytest.skip("Linux and bwrap required for real sandbox subprocess tests")
    run = allocate_run(tmp_path, "runtime", "offline")
    ws, metadata = allocate_project(run, 1, "model")
    rt = Runtime(ws, metadata, Limits(process_seconds=1, startup_seconds=1, lint_seconds=1))
    await rt.preflight()
    yield rt
    await rt.close()
    ws.close()


async def test_lint_fix_then_real_multifile_run(runtime):
    ws = runtime.workspace
    ws.write("src/helper.py", "def answer(:\n  return 42")
    ws.write("src/main.py", "from helper import answer\nprint(answer())")
    bad = await runtime.lint()
    assert bad["exit_code"] != 0 and "helper.py" in bad["diagnostics"]
    ws.edit("src/helper.py", "def answer(:", "def answer():")
    assert (await runtime.lint())["exit_code"] == 0
    descriptor = launch_descriptor({"runtime": "python", "entry": "src/main.py"}, ws)
    await runtime.setup(descriptor)
    attempt = {"number": 1}
    await runtime.execute(descriptor, attempt)
    assert attempt["outcome"] == "success" and attempt["diagnostics"].strip() == "42"


async def test_runtime_cannot_read_host_secrets_environment_or_siblings(
    runtime, tmp_path, monkeypatch
):
    ws = runtime.workspace
    secret = tmp_path / "host-secret"
    secret.write_text("secret contents")
    monkeypatch.setenv("OPENROUTER_API_KEY", "never-inherit-this")
    source = f"""import os, pathlib, socket
assert 'OPENROUTER_API_KEY' not in os.environ
for path in [{str(secret)!r}, {str(ws.root.parent.parent)!r}, '/etc/passwd']:
    try:
        pathlib.Path(path).read_text()
    except (OSError, PermissionError):
        pass
    else:
        raise AssertionError('escaped: ' + path)
try:
    pathlib.Path('/usr/escaped').write_text('bad')
except OSError:
    pass
else:
    raise AssertionError('runtime must be read-only')
pathlib.Path('/workspace/observed.txt').write_text('contained')
print('contained')
"""
    ws.write("main.py", source)
    attempt = {"number": 1}
    await runtime.execute(launch_descriptor({"runtime": "python", "entry": "main.py"}, ws), attempt)
    assert attempt["outcome"] == "success", attempt
    assert secret.read_text() == "secret contents"
    assert ws.read("observed.txt") == "contained"


async def test_lint_does_not_run_imports_scripts_or_plugins(runtime):
    ws = runtime.workspace
    ws.write("main.py", "open('/workspace/executed', 'w').write('bad')")
    ws.write("sitecustomize.py", "raise RuntimeError('imported project during lint')")
    ws.write("package.json", '{"scripts":{"lint":"touch executed"}}')
    ws.write("eslint.config.js", "throw new Error('executed plugin');")
    assert (await runtime.lint())["exit_code"] == 0
    assert not (ws.root / "executed").exists()


@pytest.mark.parametrize("kind", ["static", "python-server", "node-server"])
async def test_server_ready_preview_reuses_process_and_releases_port(runtime, kind):
    ws = runtime.workspace
    if kind == "node-server" and not shutil.which("node"):
        pytest.skip("node required")
    if kind == "static":
        entry = "index.html"
        source = '<!doctype html><link rel="stylesheet" href="style.css"><h1>Ready</h1>'
        ws.write("style.css", "h1 { color: green }")
    elif kind == "python-server":
        entry = "main.py"
        source = "from http.server import HTTPServer, SimpleHTTPRequestHandler\nHTTPServer(('127.0.0.1',8000),SimpleHTTPRequestHandler).serve_forever()"
    else:
        entry = "main.js"
        source = "require('http').createServer((q,r)=>r.end('Ready')).listen(process.env.PORT);"
    ws.write(entry, source)
    descriptor = launch_descriptor({"runtime": kind, "entry": entry}, ws)
    attempt = {"number": 1}
    managed = await runtime.execute(descriptor, attempt)
    assert attempt["outcome"] == "success", attempt
    pid = managed.process.pid
    identity = PreviewIdentity("Model A", "vendor/model", 2, "12345678abcdef", "Build a game")
    url = await runtime.present(managed, descriptor["preview"], identity, 2)
    assert url == await runtime.present(managed, descriptor["preview"])
    async with aiohttp.ClientSession() as client:
        async with client.get(url) as response:
            assert response.status == 200
            page = await response.text()
            assert identity.label in page
            assert "Attempt 2" in page
            assert f'src="{descriptor["preview"]}"' in page
            assert response.headers["Cache-Control"] == "no-store"
        async with client.head(url) as response:
            assert response.status == 200
            assert await response.read() == b""
        async with client.post(url) as response:
            assert response.status == 405
        async with client.get(urljoin(url, descriptor["preview"])) as response:
            assert response.status == 200
            app = await response.text()
            assert "Benchmark identity" not in app
            if kind == "static":
                assert app == source
        if kind == "static":
            async with client.get(urljoin(url, "/style.css")) as response:
                assert await response.text() == "h1 { color: green }"
    assert managed.process.pid == pid
    await managed.stop()
    async with aiohttp.ClientSession() as client:
        with pytest.raises(aiohttp.ClientError):
            await client.get(url)


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_stop_children(runtime, cancel):
    runtime.workspace.write(
        "main.py",
        "import subprocess, time\nsubprocess.Popen(['/usr/bin/python3','-c','import time; time.sleep(60)'])\ntime.sleep(60)",
    )
    descriptor = launch_descriptor({"runtime": "python", "entry": "main.py"}, runtime.workspace)
    attempt = {"number": 1}
    task = asyncio.create_task(runtime.execute(descriptor, attempt))
    if cancel:
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert attempt["outcome"] == "cancelled"
    else:
        await task
        assert attempt["outcome"] == "failed" and "deadline" in attempt["error"]
    assert all(process.process.returncode is not None for process in runtime.processes)


async def test_missing_setup_and_server_failure_are_explicit(runtime):
    runtime.workspace.write("main.py", "raise RuntimeError('startup failed')")
    descriptor = launch_descriptor(
        {"runtime": "python-server", "entry": "main.py"}, runtime.workspace
    )
    attempt = {"number": 1}
    assert await runtime.execute(descriptor, attempt) is None
    assert attempt["outcome"] == "failed" and "startup failed" in attempt["diagnostics"]
    runtime.workspace.write("requirements.txt", "requests==2.32.0")
    with pytest.raises(SetupError, match="off"):
        await runtime.setup(descriptor)
    runtime.auto_install = "on"
    runtime.workspace.write("requirements.txt", "-r /etc/passwd")
    with pytest.raises(SetupError, match="specifications"):
        await runtime.setup(descriptor)
    with pytest.raises(ValueError, match="unsupported"):
        launch_descriptor({"runtime": "gui", "entry": "main.py"}, runtime.workspace)


async def test_preview_socket_replacement_cannot_reach_host_services(runtime):
    reached = []
    with tempfile.TemporaryDirectory(prefix="wb-host-") as directory:
        host_path = str(Path(directory) / "host.sock")

        async def host(reader, writer):
            reached.append(True)
            writer.write(b"HTTP/1.0 200 OK\r\n\r\nHOST SECRET")
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(host, host_path)
        try:
            runtime.workspace.write(
                "main.py",
                f"import os, time\nos.unlink('/state/preview.sock')\nos.symlink({host_path!r}, '/state/preview.sock')\ntime.sleep(10)",
            )
            attempt = {"number": 1}
            await runtime.execute(
                launch_descriptor(
                    {"runtime": "python-server", "entry": "main.py"}, runtime.workspace
                ),
                attempt,
            )
            assert attempt["outcome"] == "failed"
            assert not reached and "HOST SECRET" not in attempt["diagnostics"]
        finally:
            server.close()
            await server.wait_closed()


async def test_cancelled_lint_settles_cleanup_for_concurrent_stoppers(runtime):
    task = asyncio.create_task(
        runtime.check(
            ["/usr/bin/python3", "-I", "-c", "import time; time.sleep(60)"],
            "lint",
            60,
            readonly=True,
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(runtime.close(), runtime.close())
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(p.process.returncode is not None for p in runtime.processes)


async def test_labeled_preview_preserves_post_redirect_cookies_and_websocket(runtime):
    runtime.workspace.write(
        "main.js",
        """const http = require('http');
const crypto = require('crypto');
const server = http.createServer((req, res) => {
  if (req.url === '/redirect') {
    res.writeHead(302, {Location: '/app?mode=1', 'Set-Cookie': 'review=active; Path=/'});
    return res.end();
  }
  let body = '';
  req.on('data', chunk => body += chunk);
  req.on('end', () => res.end(JSON.stringify({
    url: req.url, method: req.method, body, cookie: req.headers.cookie
  })));
});
server.on('upgrade', (req, socket) => {
  const accept = crypto.createHash('sha1').update(req.headers['sec-websocket-key'] +
    '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  socket.write('HTTP/1.1 101 Switching Protocols\\r\\nUpgrade: websocket\\r\\n' +
    'Connection: Upgrade\\r\\nSec-WebSocket-Accept: ' + accept + '\\r\\n\\r\\n');
  socket.once('data', chunk => {
    const length = chunk[1] & 127;
    const body = Buffer.from(chunk.subarray(6, 6 + length));
    for (let i = 0; i < length; i++) body[i] ^= chunk[2 + i % 4];
    socket.end(Buffer.concat([Buffer.from([129, length]), body]));
  });
});
server.listen(process.env.PORT);
""",
    )
    descriptor = launch_descriptor(
        {"runtime": "node-server", "entry": "main.js"}, runtime.workspace
    )
    attempt = {"number": 1}
    managed = await runtime.execute(descriptor, attempt)
    assert attempt["outcome"] == "success", attempt
    identity = PreviewIdentity("Model", "vendor/model", 1, "12345678", "Interactive app")
    url = await runtime.present(managed, "/app?mode=1", identity)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
        async with client.get(urljoin(url, "/redirect")) as response:
            assert (await response.json(content_type=None))["cookie"] == "review=active"
        async with client.post(urljoin(url, "/save?mode=1"), data="note=hello&count=2") as response:
            assert await response.json(content_type=None) == {
                "url": "/save?mode=1",
                "method": "POST",
                "body": "note=hello&count=2",
                "cookie": "review=active",
            }
        async with client.ws_connect(urljoin(url, "/socket")) as socket:
            await socket.send_str("still interactive")
            assert await socket.receive_str(timeout=2) == "still interactive"
        # A wrapper reload after app requests must not reach the generated server.
        async with client.get(url) as response:
            assert identity.label in await response.text()


async def test_runtime_storage_budget_is_enforced(runtime):
    runtime.limits = replace(runtime.limits, workspace_bytes=1024 * 1024)
    runtime.workspace.write(
        "main.py",
        "import time\nopen('/workspace/large', 'wb').write(b'x' * (2 * 1024 * 1024))\ntime.sleep(10)",
    )
    attempt = {"number": 1}
    await runtime.execute(
        launch_descriptor({"runtime": "python", "entry": "main.py"}, runtime.workspace), attempt
    )
    assert attempt["outcome"] == "failed"
    assert "storage budget" in attempt["error"]
