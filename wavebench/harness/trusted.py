"""Trusted sandbox entry point. Standard library only; mounted read-only.

Do not import this module from generated projects. Lint never imports project
code, configuration plugins, package scripts, or installed dependencies.
"""

from __future__ import annotations

import ast
import http.server
import json
import os
import resource
import runpy
import socket
import socketserver
import subprocess
import sys
import threading
from html.parser import HTMLParser
from pathlib import Path


def limits() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024 * 1024, 128 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    # Node reserves a large virtual address space; bound physical use via its heap.
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))


def lint() -> int:
    errors = 0
    checked = 0
    for directory, dirs, files in os.walk("/workspace", followlinks=False):
        dirs[:] = [d for d in dirs if d not in {".wb", ".git", "__pycache__"}]
        for name in [*dirs, *files]:
            path = Path(directory, name)
            if path.is_symlink():
                print(f"{path.relative_to('/workspace')}: symlinks are unsupported", flush=True)
                errors += 1
        for name in files:
            path = Path(directory, name)
            if path.is_symlink() or path.suffix not in {
                ".py",
                ".js",
                ".mjs",
                ".cjs",
                ".json",
                ".html",
                ".htm",
            }:
                continue
            checked += 1
            try:
                if path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError("file exceeds 8 MiB static-check limit")
                source = path.read_text(encoding="utf-8")
                if path.suffix == ".py":
                    # compile catches syntax errors (including return outside function).
                    compile(source, str(path.relative_to("/workspace")), "exec", ast.PyCF_ONLY_AST)
                    compile(source, str(path.relative_to("/workspace")), "exec")
                elif path.suffix in {".js", ".mjs", ".cjs"}:
                    result = subprocess.run(
                        ["/usr/bin/node", "--check", str(path)], timeout=10, check=False
                    )
                    errors += result.returncode != 0
                elif path.suffix == ".json":
                    json.loads(source)
                else:
                    HTMLParser().feed(source)
            except Exception as exc:
                print(f"{path.relative_to('/workspace')}: {exc}", flush=True)
                errors += 1
    print(f"Checked {checked} files; {errors} error(s).", flush=True)
    return 1 if errors else 0


def python_entry(entry: str, args: list[str]) -> None:
    resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
    sys.path[:0] = [str(Path(entry).parent), "/workspace", "/deps"]
    # Deliberately do not process .pth files or sitecustomize from installed wheels.
    sys.argv = [entry, *args]
    runpy.run_path(entry, run_name="__main__")


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class StaticHandler(http.server.SimpleHTTPRequestHandler):
    def address_string(self):
        return "local preview"

    def log_message(self, format, *args):
        print(format % args, file=sys.stderr, flush=True)


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            upstream = socket.create_connection(("127.0.0.1", 8000), timeout=3)
        except OSError:
            return
        self.request.settimeout(30)
        upstream.settimeout(30)

        def copy(source, target):
            try:
                while data := source.recv(65536):
                    target.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    target.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        with upstream:
            worker = threading.Thread(target=copy, args=(self.request, upstream), daemon=True)
            worker.start()
            copy(upstream, self.request)
            worker.join(timeout=1)


def main() -> int:
    limits()
    action, *args = sys.argv[1:]
    if action == "lint":
        return lint()
    if action == "python":
        python_entry(args[0], args[1:])
        return 0
    if action == "static":
        server = UnixServer("/state/preview.sock", StaticHandler)
        server.serve_forever()
    if action in {"python-server", "node-server"}:
        server = UnixServer("/state/preview.sock", Relay)
        command = (
            ["/usr/bin/python3", "-I", "-S", "/trusted/trusted.py", "python"]
            if action == "python-server"
            else ["/usr/bin/node", "--no-global-search-paths", "--max-old-space-size=512"]
        )
        child = subprocess.Popen([*command, *args], stdin=subprocess.DEVNULL)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return child.wait()
    if action == "node":
        os.execv(
            "/usr/bin/node", ["node", "--no-global-search-paths", "--max-old-space-size=512", *args]
        )
    raise ValueError(f"unsupported trusted action: {action}")


if __name__ == "__main__":
    sys.exit(main())
