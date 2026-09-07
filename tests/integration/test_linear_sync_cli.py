"""Exercise the optional sync command with realistic missing and rejected lookups."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/sync_linear_issue_project.py"


@pytest.mark.parametrize(
    "allow_missing, graphql_error, expected", [(True, False, 0), (False, False, 1), (True, True, 1)]
)
def test_sync_cli_missing_issue_and_private_error(allow_missing, graphql_error, expected):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            data = (
                {"errors": [{"message": "private-workspace-url-and-project-id"}]}
                if graphql_error
                else {"data": {"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        args = [
            sys.executable,
            str(SCRIPT),
            "--issue-url",
            "https://github.com/example/repo/issues/1",
            "--team-key",
            "TEST",
            "--project-id",
            "test-project",
            "--retries",
            "1",
            "--linear-api-url",
            f"http://127.0.0.1:{server.server_port}",
        ]
        if allow_missing:
            args.append("--allow-missing")
        result = subprocess.run(
            args,
            env={**os.environ, "LINEAR_API_KEY": "test-key"},
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result.returncode == expected
    assert "private-workspace-url-and-project-id" not in result.stdout + result.stderr
    if allow_missing and not graphql_error:
        assert "skipping" in result.stdout
