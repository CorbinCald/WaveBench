"""Free, disposable acceptance check for Harness preview routing and its menu.

Uses two deterministic generated-project fixtures. The actual configuration
menu, sandbox, runtime, browser routing and cleanup run unchanged; no model API
calls or personal settings are used.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import tempfile
import time
from pathlib import Path

from wavebench.harness.commands import launch_descriptor
from wavebench.harness.config import Limits
from wavebench.harness.handoff import DESTINATIONS
from wavebench.harness.session import HarnessBatch, HarnessSession
from wavebench.harness.workspace import allocate_run
from wavebench.storage import save_config
from wavebench.tui.menus.config_menu import interactive_config_menu

PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Generated app</title>
<style>body{{font:20px system-ui;max-width:620px;margin:10vh auto;padding:24px;background:#f5f7fb;color:#172033}}button,input{{font:inherit;padding:12px;border:1px solid #adb6c5;border-radius:8px}}button{{background:#2346b8;color:white;cursor:pointer}}output{{display:block;font-size:48px;margin:20px 0}}</style>
<h1>Generated app</h1><p>This generated Harness fixture reached your browser.</p>
<p>Click Increment, then type a short note below.</p>
<output id="count">0</output><button id="increment">Increment</button>
<p><label>Review note <input id="note" placeholder="Type here"></label></p>
<p id="echo" aria-live="polite"></p>
<p><a href="next.html">Next page</a></p>
<script>let value=0;document.querySelector('#increment').onclick=()=>document.querySelector('#count').textContent=++value;document.querySelector('#note').oninput=e=>document.querySelector('#echo').textContent=e.target.value;</script>
</html>'''


async def review(root, config):
    run = allocate_run(root, "preview-destination", "Disposable laptop handoff verification")
    sessions = []
    try:
        for slot, name in enumerate(("First preview", "Second preview"), 1):
            session = HarnessSession(
                run, slot, name, "fixture/preview", "Offline presentation check", None,
                "offline", Limits(review_seconds=config["harness"]["review_seconds"]),
                asyncio.Semaphore(1), asyncio.Semaphore(1),
                auto_open=config["auto_open"], preview_destination=config["preview_destination"],
            )
            sessions.append(session)
            session.workspace.write("index.html", PAGE.format(name=name))
            session.workspace.write("next.html", '<title>Another app title</title><h1>Next page</h1><a href="index.html">Back to app</a>')
            session.descriptor = launch_descriptor({"runtime": "static", "entry": "index.html"}, session.workspace)
            session.generation = "submitted"
            session.submitted_at = time.monotonic()
        # Identical apps deliberately finish in reverse selection order: only
        # WaveBench's identity should be needed to tell the results apart.
        for session in reversed(sessions):
            await session.execute()
            if session.status != "success":
                raise RuntimeError(session.error)
        await HarnessBatch(sessions, config["auto_open"], {}).review()
    finally:
        await asyncio.gather(*(session.close() for session in sessions))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", choices=list(DESTINATIONS), default="automatic")
    parser.add_argument("--menu", action="store_true", help="Choose the destination in the real configuration menu first")
    parser.add_argument("--review-seconds", type=int, default=600)
    args = parser.parse_args()
    if args.review_seconds <= 0:
        parser.error("--review-seconds must be positive")

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    with tempfile.TemporaryDirectory(prefix="wavebench-preview-check-") as temporary:
        original = Path.cwd()
        os.chdir(temporary)
        try:
            config = {"auto_open": "incremental", "preview_destination": args.destination,
                      "harness": {"review_seconds": args.review_seconds}}
            if args.menu:
                _, config = interactive_config_menu([], {"Fixture": "fixture/preview"}, config)
                if config is None:
                    return
            save_config(config)
            print(f"Preview destination: {DESTINATIONS[config['preview_destination']]}", flush=True)
            asyncio.run(review(Path(temporary), config))
        finally:
            os.chdir(original)
    print("Verification previews and temporary projects cleaned up.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Verification stopped; cleanup completed.")
