"""Linux filesystem/network isolation, trusted checks, and supervised execution."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import stat
import sys
import time
import uuid
from pathlib import Path

import aiohttp

from .config import Limits
from .workspace import Workspace


class SetupError(RuntimeError):
    """Failure before an execution attempt is admitted."""


class ManagedProcess:
    def __init__(self, process, drains, log: Path, state: str):
        self.process = process
        self.drains = drains
        self.log = log
        self.state = state
        self.proxy = None
        self.connections: set[asyncio.Task] = set()
        self.url: str | None = None
        self._stopped = False
        self.resource_error: str | None = None
        self.monitor = None
        self.socket_fd = -1
        self._stop_task = None

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        try:
            await asyncio.shield(self._stop_task)
        except asyncio.CancelledError:
            await self._stop_task
            raise

    async def _stop(self) -> None:
        self._stopped = True
        if self.proxy:
            self.proxy.close()
            await self.proxy.wait_closed()
        for task in list(self.connections):
            task.cancel()
        if self.connections:
            await asyncio.gather(*self.connections, return_exceptions=True)
        # Kill the bwrap group even if its immediate child has already exited.
        # bwrap's PID namespace reaper and die-with-parent also cover setsid children.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(self.process.wait(), 2)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            await self.process.wait()
        await asyncio.gather(*self.drains, return_exceptions=True)
        if self.monitor:
            await self.monitor
        if self.socket_fd >= 0:
            os.close(self.socket_fd)
            self.socket_fd = -1


class Runtime:
    def __init__(
        self, workspace: Workspace, metadata: Path, limits: Limits, auto_install: str = "off"
    ):
        self.workspace = workspace
        self.metadata = metadata
        self.limits = limits
        self.auto_install = auto_install
        self.processes: list[ManagedProcess] = []
        self.setup_results: list[dict] = []
        self._manifest: str | None = None
        self.process_slots = asyncio.Semaphore(limits.process_concurrency)
        self._deps = ".wb/deps-empty"
        self.workspace.mkdir(self._deps, internal=True)

    def _sandbox(
        self, state: str, *, readonly: bool = False, network: bool = False, deps_write: bool = False
    ) -> list[str]:
        if (
            sys.platform != "linux"
            or not shutil.which("bwrap")
            or not Path("/usr/bin/python3").exists()
        ):
            raise SetupError(
                "Harness requires Linux, Bubblewrap (bwrap), and /usr/bin/python3; no unconfined fallback"
            )
        root = f"/proc/self/fd/{self.workspace.fd}"
        argv = [
            shutil.which("bwrap"),
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--clearenv",
        ]
        if network:
            argv += ["--share-net"]  # Only trusted pip with validated wheel requirements.
        for path in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(path).exists():
                argv += ["--ro-bind", path, path]
        argv += [
            "--proc",
            "/proc",
            "--remount-ro",
            "/proc",
            "--dev",
            "/dev",
            "--ro-bind" if readonly else "--bind",
            root,
            "/workspace",
            "--bind",
            f"{root}/{state}",
            "/state",
            "--bind",
            f"{root}/{state}/tmp",
            "/tmp",
            "--bind",
            f"{root}/{state}/tmp",
            "/dev/shm",
            "--bind" if deps_write else "--ro-bind",
            f"{root}/{self._deps}",
            "/deps",
            "--tmpfs",
            "/workspace/.wb",
            "--remount-ro",
            "/workspace/.wb",
            "--ro-bind",
            str(Path(__file__).with_name("trusted.py")),
            "/trusted/trusted.py",
        ]
        if network:
            for file in ("/etc/resolv.conf", "/etc/ssl/certs", "/etc/hosts"):
                if Path(file).exists():
                    argv += ["--ro-bind", file, file]
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/state/home",
            "TMPDIR": "/tmp",
            "XDG_CACHE_HOME": "/state/cache",
            "LANG": "C.UTF-8",
            "PORT": "8000",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "CI": "1",
            "FLASK_DEBUG": "0",
            "NODE_ENV": "production",
        }
        for key, value in env.items():
            argv += ["--setenv", key, value]
        return [*argv, "--chdir", "/workspace", "--remount-ro", "/", "--"]

    async def spawn(self, command: list[str], label: str, **options) -> ManagedProcess:
        state = f".wb/{label}-{uuid.uuid4().hex}"
        for directory in ("tmp", "home", "cache"):
            self.workspace.mkdir(f"{state}/{directory}", internal=True)
        log = self.metadata / f"{label}-{uuid.uuid4().hex[:8]}.log"
        # No inherited API credentials, descriptors, terminal, or stdin.
        process = await asyncio.create_subprocess_exec(
            *self._sandbox(state, **options),
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            pass_fds=(self.workspace.fd,),
            env={"PATH": "/usr/bin:/bin"},
        )
        total = 0
        truncated = False
        stream = log.open("wb")

        async def drain(reader):
            nonlocal total, truncated
            try:
                while data := await reader.read(65536):
                    remaining = self.limits.diagnostic_bytes - total
                    if remaining > 0:
                        stream.write(data[:remaining])
                        stream.flush()
                        total += min(len(data), remaining)
                    if len(data) > remaining and not truncated:
                        stream.write(b"\n[diagnostic byte budget exceeded; output truncated]\n")
                        stream.flush()
                        truncated = True
            finally:
                stream.flush()

        tasks = [
            asyncio.create_task(drain(process.stdout)),
            asyncio.create_task(drain(process.stderr)),
        ]

        async def close_log():
            await asyncio.gather(*tasks)
            stream.close()

        managed = ManagedProcess(process, [asyncio.create_task(close_log())], log, state)
        self.processes.append(managed)

        async def monitor():
            while process.returncode is None:
                try:
                    size = await asyncio.to_thread(
                        self.workspace._size,
                        self.workspace.fd,
                        include_runtime=True,
                        limit=self.limits.workspace_bytes,
                    )
                    if size > self.limits.workspace_bytes:
                        managed.resource_error = "workspace storage budget exceeded"
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        return
                except OSError:
                    pass  # A concurrent rename never permits following a link.
                except ValueError as exc:
                    managed.resource_error = str(exc)
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    return
                await asyncio.sleep(0.1)

        managed.monitor = asyncio.create_task(monitor())
        return managed

    async def check(self, command: list[str], label: str, seconds: float, **options) -> dict:
        async with self.process_slots:
            return await self._check(command, label, seconds, **options)

    async def _check(self, command: list[str], label: str, seconds: float, **options) -> dict:
        started = time.monotonic()
        managed = await self.spawn(command, label, **options)
        error = None
        try:
            await asyncio.wait_for(managed.process.wait(), seconds)
        except asyncio.TimeoutError:
            error = f"{label} exceeded {seconds:g}s deadline"
        finally:
            await managed.stop()
        error = error or managed.resource_error
        return {
            "exit_code": managed.process.returncode if not error else -1,
            "error": error,
            "diagnostics": self.diagnostics(managed),
            "log": str(managed.log),
            "time_s": time.monotonic() - started,
        }

    def diagnostics(self, managed: ManagedProcess) -> str:
        data = managed.log.read_text(errors="replace")
        if len(data) > self.limits.output_chars:
            return "[truncated; full log saved]\n" + data[-self.limits.output_chars :]
        return data

    async def preflight(self) -> None:
        if not Path("/usr/bin/node").exists():
            raise SetupError("missing /usr/bin/node; install Node for the shared harness toolchain")
        source = "import sys, subprocess; print('Python', sys.version.split()[0]); subprocess.run(['/usr/bin/node', '--version'], check=True); print('sandbox ready')"
        if self.auto_install == "on":
            source += "; import pip; print('pip', pip.__version__)"
        result = await self.check(
            ["/usr/bin/python3", "-I", "-c", source],
            "preflight",
            10,
            readonly=True,
        )
        self.setup_results.append(result)
        if result["exit_code"] != 0:
            raise SetupError(f"unsupported validation environment: {result['diagnostics']}")

    async def lint(self) -> dict:
        return await self.check(
            ["/usr/bin/python3", "-I", "-S", "/trusted/trusted.py", "lint"],
            "lint",
            self.limits.lint_seconds,
            readonly=True,
        )

    async def setup(self, descriptor: dict) -> None:
        if descriptor["runtime"].startswith("node") and not Path("/usr/bin/node").exists():
            raise SetupError("missing /usr/bin/node for Node projects")
        try:
            package = json.loads(self.workspace.read("package.json"))
        except FileNotFoundError:
            package = {}
        if not isinstance(package, dict):
            raise SetupError("package.json must be an object")
        if package.get("dependencies") or package.get("devDependencies"):
            raise SetupError(
                "Node package installation is unsupported; use built-in Node modules or static web assets"
            )
        try:
            manifest = self.workspace.read("requirements.txt")
        except FileNotFoundError:
            manifest = ""
        specs = [
            line.strip()
            for line in manifest.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        # No URLs, local paths, includes, options, editable installs, or build hooks.
        pattern = r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:(?:==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!-]+(?:,(?:==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!-]+)*)?"
        if any(not re.fullmatch(pattern, spec) for spec in specs) or len(specs) > 256:
            raise SetupError(
                "requirements.txt accepts up to 256 PyPI name/version specifications only"
            )
        if specs and self.auto_install != "on":
            raise SetupError("Auto-install deps is off; requirements.txt must be empty")
        if manifest == self._manifest:
            return
        self._deps = f".wb/deps-{uuid.uuid4().hex}"
        self.workspace.mkdir(self._deps, internal=True)
        if specs:
            command = [
                "/usr/bin/python3",
                "-I",
                "-m",
                "pip",
                "--isolated",
                "--disable-pip-version-check",
                "install",
                "--index-url",
                "https://pypi.org/simple",
                "--no-cache-dir",
                "--only-binary=:all:",
                "--no-compile",
                "--ignore-installed",
                "--no-warn-conflicts",
                "--target",
                "/deps",
                *specs,
            ]
            result = await self.check(
                command,
                "dependencies",
                self.limits.setup_seconds,
                network=True,
                readonly=True,
                deps_write=True,
            )
            self.setup_results.append(result)
            if result["exit_code"] != 0:
                raise SetupError(f"dependency setup failed: {result['diagnostics']}")
        self._manifest = manifest

    async def execute(self, descriptor: dict, attempt: dict) -> ManagedProcess | None:
        """Caller admits/counts the attempt before spawn, including startup errors."""
        managed = None
        started = time.monotonic()
        try:
            managed = await self.spawn(
                [
                    "/usr/bin/python3",
                    "-I",
                    "-S",
                    "/trusted/trusted.py",
                    descriptor["runtime"],
                    f"/workspace/{descriptor['entry']}",
                    *descriptor["args"],
                ],
                f"run-{attempt['number']}",
            )
            attempt["log"] = str(managed.log)
            if descriptor["runtime"] in {"python", "node"}:
                await asyncio.wait_for(managed.process.wait(), self.limits.process_seconds)
                await managed.stop()
                attempt.update(
                    outcome="success"
                    if managed.process.returncode == 0 and not managed.resource_error
                    else "failed",
                    exit_code=managed.process.returncode,
                    rule="exit code 0",
                )
                if managed.resource_error:
                    attempt["error"] = managed.resource_error
            else:
                await self._ready(managed, descriptor["preview"])
                attempt.update(
                    outcome="success",
                    exit_code=None,
                    rule="HTTP readiness (2xx/3xx) before startup deadline",
                )
            attempt["diagnostics"] = self.diagnostics(managed)
        except asyncio.CancelledError:
            attempt.update(outcome="cancelled", error="cancelled")
            if managed:
                await managed.stop()
            raise
        except Exception as exc:
            if managed:
                await managed.stop()
            reason = (
                "runtime/startup deadline exceeded"
                if isinstance(exc, asyncio.TimeoutError)
                else str(exc)
            )
            attempt.update(
                outcome="failed",
                error=reason,
                exit_code=managed.process.returncode if managed else None,
                diagnostics=self.diagnostics(managed) if managed else reason,
            )
        finally:
            attempt["time_s"] = time.monotonic() - started
            attempt["finished_at"] = time.time()
        return (
            managed
            if attempt.get("outcome") == "success"
            and descriptor["runtime"] not in {"python", "node"}
            else None
        )

    def _socket_path(self, managed: ManagedProcess) -> str:
        # Pin the actual socket inode, not just its parent. A generated server can
        # replace /state/preview.sock; following that path on the host would let a
        # symlink redirect the controller into an unrelated host Unix service.
        if managed.socket_fd < 0:
            with self.workspace.directory(self.workspace.parts(managed.state, internal=True)) as fd:
                socket_fd = os.open(
                    "preview.sock", os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
                )
                if not stat.S_ISSOCK(os.fstat(socket_fd).st_mode):
                    os.close(socket_fd)
                    raise RuntimeError(
                        "preview socket must be a contained Unix socket; links are denied"
                    )
                managed.socket_fd = socket_fd
        # Also stays below AF_UNIX's pathname limit for deeply nested workspaces.
        return f"/proc/self/fd/{managed.socket_fd}"

    async def _ready(self, managed: ManagedProcess, preview: str) -> None:
        deadline = time.monotonic() + self.limits.startup_seconds
        while time.monotonic() < deadline:
            try:
                socket_path = self._socket_path(managed)
                break
            except FileNotFoundError:
                if managed.process.returncode is not None:
                    raise RuntimeError(
                        f"server exited before readiness ({managed.process.returncode})"
                    )
                await asyncio.sleep(0.05)
        else:
            raise RuntimeError("startup readiness timed out: no preview socket")
        async with aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=socket_path),
            timeout=aiohttp.ClientTimeout(total=1),
        ) as client:
            last = "no HTTP response"
            while time.monotonic() < deadline:
                if managed.process.returncode is not None:
                    raise RuntimeError(
                        f"server exited before readiness ({managed.process.returncode})"
                    )
                try:
                    async with client.get(
                        f"http://localhost{preview}", allow_redirects=False
                    ) as response:
                        await response.content.read(1024 * 1024)
                        if 200 <= response.status < 400:
                            return
                        last = f"HTTP {response.status}"
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                    last = str(exc)
                await asyncio.sleep(0.1)
        raise RuntimeError(f"startup readiness timed out: {last}")

    async def present(self, managed: ManagedProcess, preview: str) -> str:
        """Attach a loopback view to the existing process; never relaunch."""
        if managed.url:
            return managed.url

        async def connection(reader, writer):
            task = asyncio.current_task()
            if len(managed.connections) >= 16:
                writer.close()
                return
            managed.connections.add(task)
            upstream_writer = None

            async def copy(source, target):
                while data := await asyncio.wait_for(source.read(65536), 30):
                    target.write(data)
                    await target.drain()
                with contextlib.suppress(OSError):
                    target.write_eof()

            try:
                upstream_reader, upstream_writer = await asyncio.open_unix_connection(
                    self._socket_path(managed)
                )
                await asyncio.gather(copy(reader, upstream_writer), copy(upstream_reader, writer))
            except (OSError, asyncio.TimeoutError):
                pass
            finally:
                writer.close()
                if upstream_writer:
                    upstream_writer.close()
                managed.connections.discard(task)

        managed.proxy = await asyncio.start_server(connection, "127.0.0.1", 0)
        port = managed.proxy.sockets[0].getsockname()[1]
        managed.url = f"http://127.0.0.1:{port}{preview}"
        return managed.url

    async def close(self) -> None:
        await asyncio.gather(*(managed.stop() for managed in self.processes))
