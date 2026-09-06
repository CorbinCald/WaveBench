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
from wavebench.api import _is_effort_naming_bridge, _map_effort, _supported_efforts
from wavebench.modes import MODES, Mode
from wavebench.modes.code import CodeMode
from wavebench.modes.image import ImageMode, write_image_gallery
from wavebench.modes.tts import TTSMode
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
REQUEST_TIMEOUT = 3_600  # 1 hour, in seconds


def _resolve_mode(args: Any, auto_install: str, config: dict[str, Any] | None = None) -> Mode:
    """Pick the :class:`Mode` for this run based on CLI args and config.

    Precedence: explicit ``--mode <name>`` wins; falls back to the
    legacy ``--text`` flag (→ text mode) or defaults to Harness. ``code``
    remains a registry alias. Auto-install selects an explicit manifest
    policy for isolated PyPI wheels. TTS mode uses configured voice/format/speed.
    """
    config = config or {}

    def _configured_tts_mode() -> TTSMode:
        voice = getattr(args, "tts_voice", None) or config.get("tts_voice", "alloy")
        response_format = getattr(args, "tts_format", None) or config.get("tts_format", "mp3")
        speed_raw = getattr(args, "tts_speed", None)
        if speed_raw is None:
            speed_raw = config.get("tts_speed", 1.0)
        try:
            speed = float(speed_raw)
        except (TypeError, ValueError):
            speed = 1.0
        return TTSMode(
            voice=str(voice or "alloy"),
            response_format=str(response_format or "mp3"),
            speed=speed,
        )

    def _configured_image_mode() -> ImageMode:
        cli_aspect = getattr(args, "image_aspect_ratio", None)
        cli_size = getattr(args, "image_size", None)
        config_custom = config.get("image_settings") == "custom"
        custom = bool(cli_aspect or cli_size) or config_custom
        aspect_ratio = cli_aspect or (config.get("image_aspect_ratio") if config_custom else None)
        image_size = cli_size or (config.get("image_size") if config_custom else None)
        return ImageMode(
            aspect_ratio=str(aspect_ratio or "1:1"),
            image_size=str(image_size or "1K"),
            custom_settings=custom,
        )

    explicit = getattr(args, "mode", None)
    if explicit:
        mode = MODES.get(explicit)
        if mode is not None:
            if mode.name == "harness" and auto_install == "on":
                return CodeMode(allow_deps=True)
            if mode.name == "tts":
                return _configured_tts_mode()
            if mode.name == "image":
                return _configured_image_mode()
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
    from wavebench.models import (
        IMAGE_MODEL_MAPPING,
        MODEL_MAPPING,
        TTS_MODEL_MAPPING,
        image_modalities_for_model,
        is_image_model,
        is_tts_model,
    )

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

    directory_naming = config.get("directory_naming", "llm")

    _reset_incremental_tabs()

    user_prompt = args.prompt
    mode = _resolve_mode(args, auto_install, config)
    text_mode = mode.name == "text"
    tts_mode = mode.name == "tts"
    image_mode = mode.name == "image"
    harness_mode = mode.name == "harness"
    if tts_mode:
        reasoning_effort = None
        # Avoid launching every generated audio file when a user has auto-open
        # enabled for code/text runs; TTS has its own post-run browser/player.
        auto_open = "off"
    if image_mode:
        reasoning_effort = None
        if auto_open == "incremental":
            print(f"  {_skip} {S.DIM}incremental auto-open is ignored for image mode.{S.RST}")
            auto_open = "off"

    explicit_image_ids = set(config.get("image_model_ids") or [])
    if tts_mode:
        if model_mapping is None:
            mapping = TTS_MODEL_MAPPING
        else:
            mapping = {name: mid for name, mid in model_mapping.items() if is_tts_model(mid)}
            if not mapping:
                mapping = TTS_MODEL_MAPPING
    elif image_mode:
        if model_mapping is None:
            mapping = IMAGE_MODEL_MAPPING
        else:
            _pricing = pricing_lookup or {}
            mapping = {
                name: mid
                for name, mid in model_mapping.items()
                if mid in explicit_image_ids or is_image_model(mid, _pricing.get(mid, {}))
            }
            if not mapping:
                mapping = IMAGE_MODEL_MAPPING
    else:
        if model_mapping is None:
            mapping = MODEL_MAPPING
        else:
            mapping = {
                name: mid
                for name, mid in model_mapping.items()
                if not is_tts_model(mid)
                and mid not in explicit_image_ids
                and not is_image_model(mid, (pricing_lookup or {}).get(mid, {}))
            }
            if not mapping and not harness_mode:
                mapping = MODEL_MAPPING
    pad = max((len(n) for n in mapping), default=12) + 1

    if tts_mode:
        default_ext = f".{getattr(mode, 'response_format', 'mp3')}"
    elif image_mode:
        default_ext = ".png"
    elif text_mode:
        default_ext = ".md"
    else:
        default_ext = (
            ".py" if "python" in user_prompt.lower() or ".py" in user_prompt.lower() else ".html"
        )

    targets = list(mapping.items())
    if not targets:
        print(f"  {_fail} No models configured.")
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
    if tts_mode:
        print(
            _box_row(
                f"{S.DIM}{'VOICE':>8}{S.RST}  {S.HGRN}{getattr(mode, 'voice', 'alloy')}{S.RST}",
                w,
                heavy=True,
            )
        )
        print(
            _box_row(
                f"{S.DIM}{'FORMAT':>8}{S.RST}  {getattr(mode, 'response_format', 'mp3')} (provider-adjusted)",
                w,
                heavy=True,
            )
        )
    elif image_mode:
        settings = "custom" if getattr(mode, "custom_settings", False) else "provider defaults"
        details = (
            f"{settings} "
            f"({getattr(mode, 'aspect_ratio', '1:1')}, {getattr(mode, 'image_size', '1K')})"
        )
        print(_box_row(f"{S.DIM}{'IMAGE':>8}{S.RST}  {details}", w, heavy=True))
    else:
        print(_box_row(f"{S.DIM}{'REASON':>8}{S.RST}  {reasoning_label}", w, heavy=True))
    print(_box_row(f"{S.DIM}{'NAMING':>8}{S.RST}  {directory_naming}", w, heavy=True))
    if harness_mode:
        print(
            _box_row(
                f"{S.DIM}{'EXECUTE':>8}{S.RST}  {auto_open} · one run + one repair on failure",
                w,
                heavy=True,
            )
        )
        print(
            _box_row(
                f"{S.DIM}{'DEPS':>8}{S.RST}  {auto_install} · isolated PyPI wheels", w, heavy=True
            )
        )
    print(_box_bot(w, heavy=True))
    print()

    # Build per-model effort-adjustment notices; these get scrolled as a
    # news-ticker on the summary line once the tracker starts rendering.
    effort_ticker_msgs: list = []
    if reasoning_effort and not tts_mode and not image_mode:
        for _name, model_id in targets:
            supported = _supported_efforts(model_id)
            short_id = model_id.split("/", 1)[-1]
            if supported is None:
                effort_ticker_msgs.append(
                    f"{short_id}: effort {reasoning_effort} n/a → reasoning on"
                )
            else:
                mapped = _map_effort(reasoning_effort, supported)
                if mapped != reasoning_effort and not _is_effort_naming_bridge(
                    model_id, reasoning_effort, mapped
                ):
                    effort_ticker_msgs.append(f"{short_id}: effort {reasoning_effort} → {mapped}")

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=0, keepalive_timeout=30)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    results: dict[str, Any] = {}
    output_dir_final = [None]
    t0 = time.monotonic()
    harness_batch = None
    run_failure = ("failed", "benchmark did not finish")

    # Read-only snapshot for token-average pacing hints; record_run re-reads
    # fresh under a lock at write time, so this stale copy is never written back.
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
            label="Synthesizing" if tts_mode else "Generating",
            progress_unit="bytes" if tts_mode else ("images" if image_mode else "tokens"),
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
                dir_name = await get_directory_name(
                    session,
                    api_key,
                    user_prompt,
                    naming_mode=directory_naming,
                )

                base_out = os.path.join(os.getcwd(), OUTPUT_DIR)
                if harness_mode:
                    from pathlib import Path

                    from wavebench.harness.workspace import allocate_run

                    out = str(allocate_run(Path(base_out), dir_name, user_prompt))
                    output_dir_final[0] = out
                    tracker.set_output_dir(out)
                    return out
                from wavebench.harness.workspace import safe_name

                dir_name = safe_name(dir_name)
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

            def _run_model_task(name: str, mid: str) -> asyncio.Task:
                kwargs: dict[str, Any] = {
                    "reasoning_effort": reasoning_effort,
                    "auto_open": auto_open,
                    "auto_install": auto_install,
                }
                if image_mode:
                    kwargs["image_modalities"] = image_modalities_for_model(
                        mid, (pricing_lookup or {}).get(mid, {})
                    )
                return asyncio.create_task(
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
                        **kwargs,
                    )
                )

            if harness_mode:
                from pathlib import Path

                from wavebench.harness.config import Limits
                from wavebench.harness.session import HarnessBatch, HarnessSession

                limits = Limits.from_config(config)
                out = Path(await output_dir_task)
                process_slots = asyncio.Semaphore(limits.process_concurrency)
                sessions = []
                harness_batch = HarnessBatch(sessions, auto_open, results)
                for slot, (name, mid) in enumerate(targets, 1):
                    sessions.append(
                        HarnessSession(
                            out,
                            slot,
                            name,
                            mid,
                            user_prompt,
                            session,
                            api_key,
                            limits,
                            semaphore,
                            process_slots,
                            auto_install=auto_install,
                            auto_open=auto_open,
                            reasoning_effort=reasoning_effort,
                            tracker=tracker,
                        )
                    )
                await harness_batch.run()
                task_errors = []
            else:
                tasks = [_run_model_task(name, mid) for name, mid in targets]
                task_errors = await asyncio.gather(*tasks, return_exceptions=True)
            # run_model records its own failures, so a leaked exception here
            # means a task died before writing its result.  Backstop it —
            # a model must never be silently absent from the results box.
            for (name, _mid), err in zip(
                targets if not harness_mode else [], task_errors, strict=True
            ):
                if not isinstance(err, BaseException) or name in results:
                    continue
                if isinstance(err, asyncio.CancelledError):
                    results[name] = {
                        "status": "cancelled",
                        "time_s": 0.0,
                        "file": None,
                        "usage": {},
                        "retries": [],
                    }
                else:
                    results[name] = {
                        "status": "failed",
                        "time_s": 0.0,
                        "file": None,
                        "usage": {},
                        "retries": [],
                        "error": str(err) or err.__class__.__name__,
                    }

            if image_mode and output_dir_task.done():
                out = output_dir_task.result()
                gallery_path = write_image_gallery(
                    out,
                    user_prompt,
                    results,
                    theme_name=str(config.get("theme", "default")),
                )
                if auto_open == "after_all":
                    _open_with_viewer(gallery_path)
            elif not harness_mode and auto_open == "after_all" and output_dir_task.done():
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
            run_failure = ("cancelled", "cancelled before generation completed")
            print(f"\n  {S.DIM}Cancelled.{S.RST}")
        except Exception as exc:
            exc_str = str(exc) or exc.__class__.__name__
            run_failure = ("failed", exc_str)
            print(f"\n  {_fail} {S.RED}{exc_str}{S.RST}")
        finally:
            if harness_batch:
                for harness in harness_batch.sessions:
                    if harness.name not in results:
                        harness.status, harness.error = run_failure
                        harness.generation = run_failure[0]
                        harness.phase("finished")
                        results[harness.name] = harness.result()
            for name, mid in targets:
                if name not in results:
                    results[name] = {
                        "status": run_failure[0],
                        "time_s": 0.0,
                        "file": None,
                        "error": run_failure[1],
                        "usage": {},
                        "retries": [],
                    }
                    if harness_mode:
                        results[name]["harness"] = {
                            "version": 1,
                            "model_id": mid,
                            "generation": run_failure[0],
                            "attempts": [],
                        }
            if "output_dir_task" in locals() and not output_dir_task.done():
                output_dir_task.cancel()
                await asyncio.gather(output_dir_task, return_exceptions=True)
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
            cost_s = (
                f"  {S.HYEL}{format_cost(model_cost)}{S.RST}"
                if model_cost is not None
                else (f"  {S.DIM}cost unknown{S.RST}" if info.get("harness") else "")
            )
            # A length-truncated response still saves and still counts as a
            # success — the marker is the only thing separating it from a
            # complete answer in this box.
            trunc_s = f"  {S.YEL}⚠ truncated{S.RST}" if info.get("truncated") else ""
            if st == "success":
                sym = _ok
                usage_d = info.get("usage", {})
                tokens = usage_d.get("total_tokens")
                audio_bytes = usage_d.get("audio_bytes")
                image_count = usage_d.get("image_count")
                fname = info["file"]
                if audio_bytes:
                    usage_part = f"  {S.DIM}{ProgressTracker._format_bytes(audio_bytes)}{S.RST}"
                elif image_count:
                    label = "image" if image_count == 1 else "images"
                    usage_part = f"  {S.DIM}{image_count} {label}{S.RST}"
                elif tokens:
                    usage_part = f"  {S.DIM}{tokens:,} tk{S.RST}"
                else:
                    usage_part = f"  {S.DIM}usage unknown{S.RST}" if info.get("harness") else ""
                outcome = (
                    f"runtime passed ({len(info['harness']['attempts'])} run(s))"
                    if info.get("harness")
                    else "saved"
                )
                detail = f"{outcome} {_arrow} {S.GRN}{fname}{S.RST}{usage_part}{cost_s}{trunc_s}"
            elif st == "cancelled":
                sym = _skip
                detail = f"{S.DIM}cancelled{S.RST}"
            else:
                sym = _fail
                err = str(info.get("error") or "")
                err_s = (
                    f"  {S.DIM}{_truncate(err, max(12, inner_w - pad - 30))}{S.RST}" if err else ""
                )
                detail = f"{S.RED}failed{S.RST}{err_s}{cost_s}"
            rank = f"{S.DIM}{i:>2}.{S.RST}"
            content = f"{rank} {sym} {_rpad(name, pad)}  {detail}"
            if st == "success" and _vlen(content) + 2 + len(t) > inner_w:
                overflow = _vlen(content) + 2 + len(t) - inner_w
                max_fname = max(8, len(fname) - overflow)
                fname = _truncate(fname, max_fname)
                detail = f"{outcome} {_arrow} {S.GRN}{fname}{S.RST}{usage_part}{cost_s}{trunc_s}"
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
    # Image runs included: "Every run is recorded" (README) — skipping them
    # made image benchmarks invisible to history and lifetime analytics.
    run_costs = {name: _result_cost(name, info) for name, info in results.items()}
    history = record_run(
        user_prompt,
        output_dir_final[0],
        total_time,
        results,
        costs=run_costs,
        reasoning_effort=raw_effort if not (tts_mode or image_mode) else None,
    )
    display_analytics(history, compact=True, pad=pad, sort_by=config.get("analytics_sort", "runs"))
    if harness_batch:
        await harness_batch.review()
    if tts_mode and output_dir_final[0]:
        from wavebench.tui.tts_player import browse_tts_outputs

        browse_tts_outputs(output_dir_final[0], results)
    print()
