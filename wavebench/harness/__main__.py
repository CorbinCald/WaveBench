"""Developer CLI: wb --root PROJECT write src/main.py < main.py."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from .commands import Dispatcher, parse_command
from .config import Limits
from .runtime import Runtime
from .workspace import Workspace


async def dispatch_cli(args) -> int:
    workspace = Workspace(args.root)
    metadata = Path(tempfile.mkdtemp(prefix="wavebench-wb-"))
    runtime = Runtime(workspace, metadata, Limits())
    dispatcher = Dispatcher(workspace, runtime, metadata, Limits())
    try:
        if args.json:
            data = json.load(sys.stdin)
            commands = data if isinstance(data, list) else [data]
        elif args.command and args.command[0] == "parallel":
            commands = []
            for text in args.command[1:]:
                try:
                    commands.append(parse_command(text))
                except ValueError as exc:
                    commands.append({"invalid": str(exc)})
        else:
            import shlex

            command = parse_command(shlex.join(args.command))
            if command["command"] == "write":
                command["content"] = sys.stdin.read(8 * 1024 * 1024 + 1)
            elif command["command"] in {"edit", "done"}:
                data = json.load(sys.stdin)
                if not isinstance(data, dict):
                    raise ValueError("stdin must be a JSON object")
                command.update(data)
            commands = [command]
        calls = [
            {"id": f"cli-{index}", "arguments": command}
            for index, command in enumerate(commands, 1)
        ]
        results = await dispatcher.batch(calls)
        print(
            json.dumps(
                {"results": results, "diagnostics": str(metadata)}, ensure_ascii=False, indent=2
            )
        )
        return 0 if results and all(result["ok"] for result in results) else 1
    finally:
        await runtime.close()
        workspace.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="wb",
        description="Bounded project tools. No shell or project execution. Use --json for structured commands/batches on stdin.",
    )
    parser.add_argument(
        "--root", required=True, type=Path, help="Existing project root, bound by the developer"
    )
    parser.add_argument(
        "--json", action="store_true", help="Read one command object or a command array from stdin"
    )
    parser.add_argument(
        "command",
        nargs="*",
        help="ls, read PATH [START:END], write PATH (stdin), edit PATH (JSON old/new stdin), delete PATH [--recursive], lint, parallel COMMAND..., done (JSON launch stdin)",
    )
    args, extra = parser.parse_known_args()
    args.command.extend(extra)
    try:
        code = asyncio.run(dispatch_cli(args))
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        code = 1
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    main()
