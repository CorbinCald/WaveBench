"""Trusted sandbox entry point. Standard library only; mounted read-only.

Do not import this module from generated projects. Lint never imports project
code, configuration plugins, package scripts, or installed dependencies.
"""

from __future__ import annotations

import ast
import http.server
import json
import os
import re
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


SCRIPT_TAG = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
SCRIPT_TYPE = re.compile(r"""\btype\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE)
CLASSIC_TYPES = {"", "text/javascript", "application/javascript", "text/ecmascript"}
JSON_TYPES = {"importmap", "application/json", "application/ld+json"}


def node_check(source: str, kind: str) -> str | None:
    """Syntax-check source through stdin; node --check skips ES-module .js files."""
    result = subprocess.run(
        ["/usr/bin/node", "--check", f"--input-type={kind}", "-"],
        input=source,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return None if result.returncode == 0 else result.stderr or "syntax check failed"


def script_error(source: str, module: bool | None) -> str | None:
    """None when the script parses; module=None accepts either an ES module or a script."""
    if module is not None:
        return node_check(source, "module" if module else "commonjs")
    as_module = node_check(source, "module")
    if as_module is None:
        return None
    as_script = node_check(source, "commonjs")
    if as_script is None:
        return None
    return as_module if re.search(r"^\s*(?:import|export)\b", source, re.MULTILINE) else as_script


def syntax_report(path: str, stderr: str, first_line: int = 1) -> str:
    """path:line: error, plus Node's source and caret lines, without its stack trace."""
    lines = stderr.splitlines()
    error = next(
        (line for line in lines if re.match(r"^\w*Error\b", line)), lines[-1] if lines else ""
    )
    location = re.match(r"^\[stdin\]:(\d+)", lines[0]) if lines else None
    if not location:
        return f"{path}: {error}"
    report = f"{path}:{int(location.group(1)) + first_line - 1}: {error}"
    snippet = [line for line in lines[1:3] if line.strip()]
    return "\n".join([report, *("    " + line for line in snippet)])


def lint() -> int:
    errors = 0
    checked = 0
    scripts = 0

    def problem(message: str) -> None:
        nonlocal errors
        print(message, flush=True)
        errors += 1

    for directory, dirs, files in os.walk("/workspace", followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in {".wb", ".git", "__pycache__", "node_modules"})
        for name in [*dirs, *files]:
            path = Path(directory, name)
            if path.is_symlink():
                problem(f"{path.relative_to('/workspace')}: symlinks are unsupported")
        for name in sorted(files):
            path = Path(directory, name)
            relative = str(path.relative_to("/workspace"))
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
                    compile(source, relative, "exec", ast.PyCF_ONLY_AST)
                    compile(source, relative, "exec")
                elif path.suffix in {".js", ".mjs", ".cjs"}:
                    module = {".mjs": True, ".cjs": False}.get(path.suffix)
                    if error := script_error(source, module):
                        problem(syntax_report(relative, error))
                elif path.suffix == ".json":
                    json.loads(source)
                else:
                    HTMLParser().feed(source)
                    for match in SCRIPT_TAG.finditer(source):
                        attributes, body = match.group(1), match.group(2)
                        if re.search(r"\bsrc\s*=", attributes, re.IGNORECASE) or not body.strip():
                            continue
                        kind = SCRIPT_TYPE.search(attributes)
                        kind = kind.group(1).lower() if kind else ""
                        first_line = source.count("\n", 0, match.start(2)) + 1
                        if kind in JSON_TYPES:
                            scripts += 1
                            try:
                                json.loads(body)
                            except ValueError as exc:
                                problem(f"{relative}:{first_line}: inline {kind} JSON: {exc}")
                        elif kind == "module" or kind in CLASSIC_TYPES:
                            scripts += 1
                            if error := script_error(body, kind == "module"):
                                problem(syntax_report(relative, error, first_line))
            except Exception as exc:
                problem(f"{relative}: {exc}")
    inline = f" and {scripts} inline script(s)" if scripts else ""
    print(f"Checked {checked} files{inline}; {errors} error(s).", flush=True)
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
