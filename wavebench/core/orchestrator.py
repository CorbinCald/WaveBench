"""Parallel-run orchestrator and module-level benchmark constants.

``main_async`` is the one public entry point of ``wavebench.core`` — it
owns the whole benchmark run: prompt framing, output directory setup,
per-model task fan-out, progress-tracker lifecycle, post-run
leaderboard rendering, history recording, and auto-open cleanup.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import aiohttp

import wavebench.tui.styles as _styles
from wavebench.api import _map_effort, _supported_efforts
from wavebench.modes import MODES, Mode
from wavebench.modes.code import CodeMode
from wavebench.parsers import get_directory_name
from wavebench.storage import load_history, record_run
from wavebench.tui.analytics import compute_cost, display_analytics
from wavebench.tui.progress import ProgressTracker
from wavebench.tui.styles import (
    S,
    _arrow,
    _box_bot,
    _box_divider,
    _box_row,
    _box_sep,
    _box_top,
    _dot,
    _fail,
    _ok,
    _rpad,
    _skip,
    _truncate,
    _tw,
    _vlen,
    format_cost,
    format_duration,
)

from .auto_install import _venv_python_path
from .auto_open import (
    _INTERPRETER_MAP,
    _open_files_as_tabs,
    _open_with_viewer,
    _reset_incremental_tabs,
    _resolve_interpreter,
)
from .runner import run_model

OUTPUT_DIR = "benchmarkResults"
MAX_CONCURRENCY = 12
REQUEST_TIMEOUT = 1800  # seconds


def _resolve_mode(args: Any, auto_install: str) -> Mode:
    """Pick the :class:`Mode` for this run based on CLI args and config.

    Precedence: explicit ``--mode <name>`` wins; falls back to the
    legacy ``--text`` flag (→ text mode) or defaults to code mode. When
    code mode is selected and ``auto_install == "on"``, a fresh
    ``CodeMode(allow_deps=True)`` is constructed so the system prompt
    permits third-party packages.
    """
    explicit = getattr(args, "mode", None)
    if explicit:
        mode = MODES.get(explicit)
        if mode is not None:
            if mode.name == "code" and auto_install == "on":
                return CodeMode(allow_deps=True)
            return mode

    if getattr(args, "text", False):
        return MODES["text"]

    return CodeMode(allow_deps=(auto_install == "on"))


async def main_async(
    args: Any,
    api_key: str,
    model_mapping: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
    pricing_lookup: dict[str, Any] | None = None,
) -> None:
    from wavebench.models import MODEL_MAPPING

    mapping = model_mapping if model_mapping is not None else MODEL_MAPPING
    pad = max((len(n) for n in mapping), default=12) + 1

    if config is None:
        from wavebench.storage import load_config

        config = load_config()

    raw_effort = config.get("reasoning_effort", "high")
    reasoning_effort: str | None = None if raw_effort == "off" else raw_effort

    auto_open = config.get("auto_open", "off")
    if getattr(args, "auto_open", None):
        auto_open = args.auto_open

    auto_install = config.get("auto_install", "off")
    if getattr(args, "auto_install", None):
        auto_install = "on"

    _reset_incremental_tabs()

    user_prompt = args.prompt
    mode = _resolve_mode(args, auto_install)
    text_mode = mode.name == "text"

    if text_mode:
        default_ext = ".md"
    else:
        default_ext = (
            ".py" if "python" in user_prompt.lower() or ".py" in user_prompt.lower() else ".html"
        )

    targets = list(mapping.items())
    if not targets:
        print(f"  {_fail} No models configured in MODEL_MAPPING.")
        return

    mode_label = (
        f"{S.HYEL}{mode.display_name.upper()}{S.RST}"
        if text_mode
        else f"{_styles.ACCENT_HI}{mode.display_name.upper()}{S.RST}"
    )

    w = _tw() - 4
    if reasoning_effort:
        reasoning_label = f"{S.HGRN}{reasoning_effort}{S.RST}"
    else:
        reasoning_label = f"{S.HRED}off{S.RST}"
    print()
    print(_box_top("", w, heavy=True))
    print(_box_row(f"{S.DIM}{'MODE':>8}{S.RST}  {mode_label}", w, heavy=True))
    print(
        _box_row(
            f"{S.DIM}{'PROMPT':>8}{S.RST}  {S.BOLD}{_truncate(user_prompt, w - 16)}{S.RST}",
            w,
            heavy=True,
        )
    )
    print(_box_divider(w, heavy=True))
    print(_box_row(f"{S.DIM}{'MODELS':>8}{S.RST}  {len(targets)} active", w, heavy=True))
    print(_box_row(f"{S.DIM}{'REASON':>8}{S.RST}  {reasoning_label}", w, heavy=True))
    print(_box_bot(w, heavy=True))
    print()

    # Build per-model effort-adjustment notices; these get scrolled as a
    # news-ticker on the summary line once the tracker starts rendering.
    effort_ticker_msgs: list = []
    if reasoning_effort:
        for _name, model_id in targets:
            supported = _supported_efforts(model_id)
            short_id = model_id.split("/", 1)[-1]
            if supported is None:
                effort_ticker_msgs.append(
                    f"{short_id}: effort {reasoning_effort} n/a → reasoning on"
                )
            else:
                mapped = _map_effort(reasoning_effort, supported)
                if mapped != reasoning_effort:
                    effort_ticker_msgs.append(f"{short_id}: effort {reasoning_effort} → {mapped}")

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=0, keepalive_timeout=30)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    results: dict[str, Any] = {}
    output_dir_final = [None]
    t0 = time.monotonic()

    history = load_history()
    avg_tokens: dict[str, float] = {}
    for run in history.get("runs", []):
        for name, res in run.get("models", {}).items():
            if res.get("status") == "success":
                tkns = (res.get("usage") or {}).get("total_tokens")
                if tkns:
                    avg_tokens.setdefault(name, []).append(tkns)  # type: ignore[arg-type]
    avg_tokens = {k: sum(v) / len(v) for k, v in avg_tokens.items()}  # type: ignore[arg-type]

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
    ) as session:
        model_names = [name for name, _ in targets]
        model_id_map = {name: mid for name, mid in targets}
        tracker = ProgressTracker(
            len(targets),
            results,
            pad=pad,
            model_names=model_names,
            avg_tokens=avg_tokens,
            pricing_lookup=pricing_lookup or {},
            model_id_map=model_id_map,
            alt_screen=True,
        )
        if effort_ticker_msgs:
            tracker.set_ticker(effort_ticker_msgs)
        try:

            async def resolve_output_dir() -> str:
                dir_name = await get_directory_name(session, api_key, user_prompt)

                base_out = os.path.join(os.getcwd(), OUTPUT_DIR)
                out = os.path.join(base_out, dir_name)
                os.makedirs(out, exist_ok=True)

                pf = os.path.join(out, "prompt.txt")
                if not os.path.exists(pf):
                    with open(pf, "w", encoding="utf-8") as fh:
                        fh.write(user_prompt)

                output_dir_final[0] = out
                tracker.set_output_dir(out)
                return out

            output_dir_task = asyncio.create_task(resolve_output_dir())
            await tracker.start()

            tasks = [
                run_model(
                    mode,
                    session,
                    api_key,
                    name,
                    mid,
                    user_prompt,
                    default_ext,
                    output_dir_task,
                    semaphore,
                    results,
                    pad,
                    tracker,
                    reasoning_effort=reasoning_effort,
                    auto_open=auto_open,
                    auto_install=auto_install,
                )
                for name, mid in targets
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

            if auto_open == "after_all" and output_dir_task.done():
                out = output_dir_task.result()
                code_tabs: list[tuple] = []
                for name, info in results.items():
                    if info.get("status") != "success" or not info.get("file"):
                        continue
                    fp = os.path.join(out, info["file"])
                    ext = os.path.splitext(fp)[1].lower()
                    if ext in _INTERPRETER_MAP:
                        vp = info.get("venv_python")
                        if not vp and ext == ".py":
                            candidate = _venv_python_path(out)
                            if os.path.isfile(candidate):
                                vp = candidate
                        interp = vp or _resolve_interpreter(fp, ext)
                        if interp:
                            code_tabs.append((fp, interp))
                        else:
                            _open_with_viewer(fp)
                    else:
                        _open_with_viewer(fp)
                if code_tabs:
                    _open_files_as_tabs(code_tabs)

            if not output_dir_task.done():
                output_dir_task.cancel()

        except asyncio.CancelledError:
            print(f"\n  {S.DIM}Cancelled.{S.RST}")
        except Exception as exc:
            exc_str = str(exc) or exc.__class__.__name__
            print(f"\n  {_fail} {S.RED}{exc_str}{S.RST}")
        finally:
            await tracker.stop()

    # ── Run results ────────────────────────────────────────────────────────
    total_time = time.monotonic() - t0
    _pricing = pricing_lookup or {}
    _id_map = {name: mid for name, mid in targets}

    def _result_cost(name: str, info: dict[str, Any]) -> float | None:
        mid = _id_map.get(name, "")
        return compute_cost(info.get("usage", {}), _pricing.get(mid, {}))

    if not tracker.rendered_final:
        ok = sum(1 for v in results.values() if v["status"] == "success")
        fail = sum(1 for v in results.values() if v["status"] == "failed")
        canc = sum(1 for v in results.values() if v["status"] == "cancelled")
        inner_w = w - 4

        print()
        print(_box_top("Run Results", w))
        if output_dir_final[0]:
            out_path = output_dir_final[0]
            max_path = inner_w - 10
            if len(out_path) > max_path:
                out_path = "…" + out_path[-(max_path - 1) :]
            print(_box_row(f"{S.DIM}{'OUTPUT':>8}  {out_path}{S.RST}", w))
            print(_box_sep("", w))
        else:
            print(_box_row("", w))

        def _rank_key(item: Any) -> Any:
            _, v = item
            order = {"success": 0, "failed": 1, "cancelled": 2}
            return (order.get(v["status"], 3), v["time_s"])

        _total_run_cost = 0.0
        _has_any_cost = False

        for i, (name, info) in enumerate(sorted(results.items(), key=_rank_key), 1):
            st = info["status"]
            t = format_duration(info["time_s"])
            model_cost = _result_cost(name, info)
            if model_cost is not None:
                _total_run_cost += model_cost
                _has_any_cost = True
            cost_s = f"  {S.HYEL}{format_cost(model_cost)}{S.RST}" if model_cost else ""
            if st == "success":
                sym = _ok
                usage_d = info.get("usage", {})
                tokens = usage_d.get("total_tokens")
                fname = info["file"]
                tk_part = f"  {S.DIM}{tokens:,} tk{S.RST}" if tokens else ""
                detail = f"saved {_arrow} {S.GRN}{fname}{S.RST}{tk_part}{cost_s}"
            elif st == "cancelled":
                sym = _skip
                detail = f"{S.DIM}cancelled{S.RST}"
            else:
                sym = _fail
                detail = f"{S.RED}failed{S.RST}{cost_s}"
            rank = f"{S.DIM}{i:>2}.{S.RST}"
            content = f"{rank} {sym} {_rpad(name, pad)}  {detail}"
            if st == "success" and _vlen(content) + 2 + len(t) > inner_w:
                overflow = _vlen(content) + 2 + len(t) - inner_w
                max_fname = max(8, len(fname) - overflow)
                fname = _truncate(fname, max_fname)
                detail = f"saved {_arrow} {S.GRN}{fname}{S.RST}{tk_part}{cost_s}"
                content = f"{rank} {sym} {_rpad(name, pad)}  {detail}"
            gap = max(inner_w - _vlen(content) - len(t), 2)
            print(_box_row(f"{content}{' ' * gap}{S.DIM}{t}{S.RST}", w))

        print(_box_sep("", w))
        parts: list[str] = []
        if ok:
            parts.append(f"{S.HGRN}{ok} passed{S.RST}")
        if fail:
            parts.append(f"{S.HRED}{fail} failed{S.RST}")
        if canc:
            parts.append(f"{S.DIM}{canc} cancelled{S.RST}")
        parts.append(f"{format_duration(total_time)} total")
        if _has_any_cost:
            parts.append(f"{S.HYEL}{format_cost(_total_run_cost)}{S.RST}")
        sep = f" {_dot} "
        print(_box_row(sep.join(parts), w))
        print(_box_bot(w))

    # ── Record run & show lifetime analytics ───────────────────────────────
    run_costs = {name: _result_cost(name, info) for name, info in results.items()}
    record_run(
        history,
        user_prompt,
        output_dir_final[0],
        total_time,
        results,
        costs=run_costs,
        reasoning_effort=raw_effort,
    )
    display_analytics(history, compact=True, pad=pad, sort_by=config.get("analytics_sort", "runs"))
    print()
