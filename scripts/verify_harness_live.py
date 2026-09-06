"""Explicit, paid acceptance checks through the real WaveBench CLI.

Run from the repository using its Python environment. Outputs and credentials
stay outside version control; only the redacted evidence summary is publishable.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from wavebench.api import load_api_key

PYTHON_TASK = (
    "Build a small multi-file Python project: main.py imports a function from helpers.py "
    "that computes the sum of squares of [1,2,3], prints 14, and exits. Include README.md. "
    "This is an intentional harness repair test: in your FIRST submission, main.py must "
    "instead raise RuntimeError('intentional acceptance failure'). Lint and submit it. "
    "Only after WaveBench reports that failure, repair main.py to import and call the helper, "
    "lint again, and submit. Do not introduce dependencies or a server."
)
WEB_TASK = (
    "Build a small static web project with index.html, styles.css, and app.js. "
    "Show a title 'Harness counter', a visible numeric counter starting at 0, and an "
    "Increment button that increases the counter on click. Use local assets only, "
    "no dependencies. Lint and submit using the static runtime with entry index.html."
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Required acknowledgement: uses paid OpenRouter calls"
    )
    parser.add_argument(
        "--model", action="append", required=True, help="Repeat for at least two families"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--case",
        action="append",
        choices=[
            f"{kind}-{policy}"
            for kind in ("python-repair", "web")
            for policy in ("off", "incremental", "after_all")
        ],
        help="Optional case filter",
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; default tests never make paid calls")
    if len(args.model) < 2 or len({mid.split("/")[0] for mid in args.model}) < 2:
        parser.error("select at least two model families")
    key = load_api_key()
    if not key:
        parser.error("OPENROUTER_API_KEY is missing")
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[1]
    env = {**os.environ, "OPENROUTER_API_KEY": key, "PYTHONPATH": str(repo)}
    # Use the actual managed preview URL with a headless HTTP viewer. No relaunch,
    # and no twelve-tab browser burst during the matrix. Browser UX is checked separately.
    viewer = output / "headless_viewer.py"
    viewer.write_text(
        "import sys, urllib.request\nwith urllib.request.urlopen(sys.argv[-1], timeout=5) as response:\n    print('PREVIEW_HTTP', response.status, len(response.read()))\n"
    )
    import shlex

    env["BROWSER"] = shlex.join([sys.executable, str(viewer)]) + " %s"
    evidence = []
    for kind, prompt in (("python-repair", PYTHON_TASK), ("web", WEB_TASK)):
        for policy in ("off", "incremental", "after_all"):
            if args.case and f"{kind}-{policy}" not in args.case:
                continue
            case = output / f"{kind}-{policy}"
            case.mkdir()
            (case / ".benchmark_models.json").write_text(
                json.dumps({f"model-{i + 1}": mid for i, mid in enumerate(args.model)})
            )
            (case / ".benchmark_config.json").write_text(
                json.dumps(
                    {
                        "reasoning_effort": "low",
                        "directory_naming": "slug",
                        "auto_install": "off",
                        "auto_open": policy,
                        "harness": {
                            "build_turns": 16,
                            "repair_turns": 8,
                            "total_tokens": 128000,
                            "turn_tokens": 8192,
                            "build_seconds": 300,
                            "repair_seconds": 180,
                            "review_seconds": 2,
                        },
                    }
                )
            )
            print(f"Running {kind} / {policy}: {', '.join(args.model)}", flush=True)
            started = time.time()
            with (case / "cli.log").open("w") as log:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "wavebench",
                        "--mode",
                        "harness",
                        "--auto-open",
                        policy,
                        "--prompt",
                        prompt,
                    ],
                    cwd=case,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=1000,
                    check=False,
                )
            history_path = case / ".benchmark_history.json"
            run = (
                json.loads(history_path.read_text())["runs"][-1]
                if history_path.exists()
                else {"models": {}}
            )
            row = {
                "case": case.name,
                "started_at": started,
                "finished_at": time.time(),
                "cli_exit": completed.returncode,
                "models": run["models"],
            }
            expected = 2 if kind == "python-repair" else 1
            row["passed"] = (
                completed.returncode == 0
                and len(run["models"]) == len(args.model)
                and all(
                    result["status"] == "success"
                    and len(result.get("harness", {}).get("attempts", [])) == expected
                    and result["usage"].get("total_tokens") is not None
                    for result in run["models"].values()
                )
            )
            if kind == "python-repair":
                row["passed"] = row["passed"] and all(
                    "RuntimeError: intentional acceptance failure"
                    in result["harness"]["attempts"][0].get("diagnostics", "")
                    for result in run["models"].values()
                    if result.get("harness", {}).get("attempts")
                )
            if kind == "web" and policy != "off":
                row["preview_loads"] = (case / "cli.log").read_text().count("PREVIEW_HTTP 200")
                row["passed"] = row["passed"] and row["preview_loads"] == len(args.model)
            evidence.append(row)
            (output / "evidence.json").write_text(json.dumps(evidence, indent=2))
            print(
                f"  {'PASS' if row['passed'] else 'FAIL'}: "
                + ", ".join(
                    f"{name} {result['status']} / {len(result.get('harness', {}).get('attempts', []))} attempts"
                    for name, result in run["models"].items()
                ),
                flush=True,
            )
            if not row["passed"]:
                print(
                    "Stopping at the first failing case; inspect the saved diagnostics before spending on more calls.",
                    flush=True,
                )
                return 1
    print(f"Evidence: {output / 'evidence.json'}", flush=True)
    return 0 if all(row["passed"] for row in evidence) else 1


if __name__ == "__main__":
    raise SystemExit(main())
