"""Present managed previews through the optional Herdr laptop companion."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
from pathlib import Path

DESTINATIONS = {"automatic": "Automatic", "laptop": "Connected laptop", "host": "Wavebench host"}


async def stop_helper(process) -> None:
    if process.stdin:
        process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


async def client_status(helper: str, session: str) -> dict:
    process = await asyncio.create_subprocess_exec(
        helper,
        "status",
        "--session",
        session,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 5)
        if process.returncode:
            raise RuntimeError("Could not read laptop connection status")
        data = json.loads(output)
        if not isinstance(data, dict):
            raise ValueError("Invalid laptop connection status")
        client = data.get("client", {})
        if not isinstance(client, dict):
            raise ValueError("Invalid laptop connection status")
        return client
    finally:
        if process.returncode is None:
            await stop_helper(process)


async def destination(choice: str) -> str:
    if choice not in DESTINATIONS:
        raise ValueError("Preview destination must be automatic, laptop, or host")
    if choice != "automatic":
        return choice
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"):
        return "laptop"
    helper = shutil.which("herdr-review")
    if helper:
        try:
            status = await client_status(helper, os.environ.get("HERDR_SESSION", "work"))
        except (OSError, ValueError, RuntimeError, asyncio.TimeoutError):
            status = {}
        # Herdr's persistent server may have no SSH environment. A previous
        # companion acknowledgement identifies that session even after detach.
        if status.get("fresh") or (os.environ.get("HERDR_ENV") and status.get("received")):
            return "laptop"
    if sys.platform == "linux" and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return "laptop"
    return "host"


class RemotePreview:
    """A registration owned by a pipe; closing it withdraws only this preview."""

    def __init__(self):
        self.helper = shutil.which("herdr-review")
        self.session = os.environ.get("HERDR_SESSION", "work")
        self.process = None
        self.record: dict = {}

    async def open(self, url: str, log_path: Path) -> None:
        if not self.helper:
            raise RuntimeError(
                "Laptop handoff unavailable: install herdr-review on the Wavebench host"
            )
        try:
            with log_path.open("ab", buffering=0) as log:
                self.process = await asyncio.create_subprocess_exec(
                    self.helper,
                    "offer",
                    "--session",
                    self.session,
                    "--url",
                    url,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=log,
                )
            line = await asyncio.wait_for(self.process.stdout.readline(), 5)
            self.record = json.loads(line)
            if self.record.get("urls") != [url] or not self.record.get("token"):
                raise ValueError("Invalid preview registration")
        except BaseException as exc:
            await self.close()
            if not isinstance(exc, Exception):
                raise
            raise RuntimeError(
                "Laptop handoff unavailable: install or update herdr-review on the Wavebench host. "
                f"Details: {log_path}"
            ) from exc

    def message(self, status: dict) -> str:
        if not status.get("fresh"):
            return "Waiting for laptop connection"
        for record in status.get("reviews", []):
            if record.get("token") != self.record.get("token"):
                continue
            if record.get("errors"):
                return "Laptop preview unavailable: " + "; ".join(record["errors"])
            if set(record.get("ports", [])) == set(self.record.get("ports", [])):
                return "Opened on connected laptop"
        return "Waiting for laptop to open preview"

    async def close(self) -> None:
        if self.process:
            await stop_helper(self.process)
