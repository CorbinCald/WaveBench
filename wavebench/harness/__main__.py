"""Developer CLI: wb --root PROJECT write src/main.py < main.py."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from .commands import VERBS, Dispatcher, parse_command
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
            calls = []
            for item in data if isinstance(data, list) else [data]:
                if not isinstance(item, dict):
                    calls.append(
                        {"name": None, "arguments": {}, "error": "each command must be an object"}
                    )
                    continue
                item = dict(item)
                verb = item.pop("command", None) or item.pop("tool", None)
                calls.append({"name": VERBS.get(verb, verb), "arguments": item})
        elif args.command and args.command[0] == "parallel":
            calls = []
            for text in args.command[1:]:
                try:
                    calls.append(parse_command(text))
                except ValueError as exc:
                    calls.append({"name": None, "arguments": {}, "error": str(exc)})
        else:
            import shlex

            call = parse_command(shlex.join(args.command))
            if call["name"] == "write_file":
                call["arguments"]["content"] = sys.stdin.read(8 * 1024 * 1024 + 1)
            elif call["name"] in {"edit_file", "submit"}:
                data = json.load(sys.stdin)
                if not isinstance(data, dict):
                    raise ValueError("stdin must be a JSON object")
                call["arguments"].update(data)
            calls = [call]
        calls = [{"id": f"cli-{index}", **call} for index, call in enumerate(calls, 1)]
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
        help="ls [PATH], read PATH [START:END], write PATH (stdin), edit PATH (JSON old_text/new_text on stdin), delete PATH, lint, parallel COMMAND..., submit (JSON runtime/entry on stdin)",
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
