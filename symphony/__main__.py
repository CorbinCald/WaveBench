"""Command-line host for the Python Symphony service."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from symphony.agent import AgentRunner
from symphony.config import resolve_config
from symphony.errors import SymphonyError
from symphony.linear import LinearClient
from symphony.orchestrator import Orchestrator
from symphony.workflow import load_workflow, select_workflow_path
from symphony.workspace import WorkspaceManager

_LOG = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Symphony: poll Linear issues and dispatch Pi RPC agents.",
    )
    parser.add_argument(
        "workflow",
        nargs="?",
        help="Path to WORKFLOW.md (default: ./WORKFLOW.md)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one reconciliation/dispatch tick and exit (useful for smoke tests).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Structured log verbosity.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
    )
    try:
        asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        return 0
    except SymphonyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


async def _async_main(args: argparse.Namespace) -> None:
    workflow_path = select_workflow_path(args.workflow)
    if args.workflow and not Path(args.workflow).exists():
        raise SymphonyError("missing_workflow_file", f"explicit workflow path does not exist: {args.workflow}")
    workflow = load_workflow(workflow_path)
    config = resolve_config(workflow)
    tracker = LinearClient(config.tracker)
    workspace_manager = WorkspaceManager(config.workspace_root, config.hooks, config.git)
    agent_runner = AgentRunner(config, workflow, workspace_manager, tracker)
    orchestrator = Orchestrator(config, workflow, tracker, agent_runner, workspace_manager)
    await orchestrator.startup_terminal_workspace_cleanup()

    if args.once:
        await _reload_if_changed(orchestrator, workflow_path)
        await orchestrator.tick()
        return

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    _LOG.info("symphony_started workflow=%s", workflow_path)
    while not stop_event.is_set():
        await _reload_if_changed(orchestrator, workflow_path)
        await orchestrator.tick()
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=orchestrator.state.poll_interval_ms / 1000
            )
        except asyncio.TimeoutError:
            pass
    _LOG.info("symphony_stopped workflow=%s", workflow_path)


async def _reload_if_changed(orchestrator: Orchestrator, workflow_path: Path) -> None:
    try:
        mtime_ns = workflow_path.stat().st_mtime_ns
    except OSError as exc:
        _LOG.error("workflow_reload_failed error=%s", exc)
        return
    if mtime_ns == orchestrator.config.workflow_mtime_ns:
        return
    try:
        workflow = load_workflow(workflow_path)
        config = resolve_config(workflow)
    except Exception as exc:
        _LOG.error("workflow_reload_failed keep_last_good=true error=%s", exc)
        return
    orchestrator.update_config(config, workflow)
    _LOG.info("workflow_reload_completed workflow=%s", workflow_path)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
