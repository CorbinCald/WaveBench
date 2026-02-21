import os
import re
import time
import asyncio
import aiohttp
from typing import Dict, Any, Optional

from llm_benchmarks.api import call_model_streaming
from llm_benchmarks.parsers import parse_with_gemini, get_directory_name
from llm_benchmarks.tui.styles import S, _wait, _fail, _work, _ok, _skip, _arrow, format_duration, _rule, _truncate, _dot, _rpad, _tw
from llm_benchmarks.tui.components import ProgressTracker, display_analytics
from llm_benchmarks.storage import load_history, record_run

OUTPUT_DIR       = "benchmarkResults"
MAX_CONCURRENCY  = 12
REQUEST_TIMEOUT  = 600  # seconds

SYSTEM_PROMPT_CODE = (
    "You are an expert programmer. Your goal is to provide a complete, "
    "fully functional, single-file implementation based on the user's request. "
    "Do not include any external modules or dependencies. "
    "Return ONLY the code, with no preamble or explanation."
)

SYSTEM_PROMPT_TEXT = (
    "You are a knowledgeable assistant. Provide a clear, detailed, and "
    "well-structured answer to the user's question. Use Markdown formatting "
    "for readability. Do not include code unless the user explicitly asks for it."
)

def get_unique_filename(directory: str, base_name: str, extension: str) -> str:
    """Return a unique filename, appending _v2, _v3, … if needed."""
    base = re.sub(r'cursor', '', base_name, flags=re.IGNORECASE)
    if not extension.startswith('.'):
        extension = f".{extension}"

    path = os.path.join(directory, f"{base}{extension}")
    if not os.path.exists(path):
        return f"{base}{extension}"

    counter = 2
    while True:
        name = f"{base}_v{counter}{extension}"
        if not os.path.exists(os.path.join(directory, name)):
            return name
        counter += 1

async def process_model(session: aiohttp.ClientSession, api_key: str, model_name: str, model_id: str, prompt: str,
                        default_ext: str, output_dir_task: asyncio.Task, semaphore: asyncio.Semaphore,
                        results: Dict[str, Any], pad: int, tracker: Any) -> None:
    """Generate code from a single model, parse it, and save to disk."""
    start = time.monotonic()
    registered = tracker.is_running

    if registered:
        tracker.register(model_name)
    else:
        print(f"  {_wait} {model_name:<{pad}}  "
              f"{S.DIM}calling {model_id}…{S.RST}")

    content: Optional[str] = None
    usage: dict = {}

    try:
        async with semaphore:
            def _on_progress(chars: int) -> None:
                if registered:
                    tracker.update(model_name, chars)

            content, usage = await call_model_streaming(
                session, api_key, model_id, prompt,
                on_progress=_on_progress)

    except asyncio.CancelledError:
        elapsed = time.monotonic() - start
        print(f"  {_skip} {model_name:<{pad}}  "
              f"{S.DIM}cancelled  [{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "cancelled", "time_s": elapsed, "file": None, "usage": {}}
        return
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        print(f"  {_fail} {model_name:<{pad}}  "
              f"{S.RED}timeout{S.RST}  "
              f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return
    except aiohttp.ClientError as exc:
        elapsed = time.monotonic() - start
        print(f"  {_fail} {model_name:<{pad}}  "
              f"{S.RED}API error: {exc}{S.RST}  "
              f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return
    except Exception as exc:
        elapsed = time.monotonic() - start
        exc_str = str(exc) or exc.__class__.__name__
        print(f"  {_fail} {model_name:<{pad}}  "
              f"{S.RED}{exc_str}{S.RST}  "
              f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return
    finally:
        if registered:
            tracker.unregister(model_name)

    elapsed = time.monotonic() - start

    if not content:
        print(f"  {_fail} {model_name:<{pad}}  "
              f"{S.RED}no response{S.RST}  "
              f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return

    if registered:
        tracker.mark_parsing(model_name)
    else:
        print(f"  {_work} {model_name:<{pad}}  "
              f"{S.DIM}parsing…{S.RST}")

    parsed = await parse_with_gemini(
        session, api_key, model_name, content)

    if registered:
        tracker.finish_parsing(model_name)

    if parsed and parsed.get("code"):
        ext = parsed.get("extension", default_ext)
        output_dir = await output_dir_task
        filename = get_unique_filename(output_dir, model_name, ext)
        filepath = os.path.join(output_dir, filename)

        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write(parsed["code"])

        elapsed = time.monotonic() - start
        print(f"  {_ok} {S.BOLD}{model_name:<{pad}}{S.RST}  "
              f"saved {_arrow} {S.GRN}{filename}{S.RST}  "
              f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "success", "time_s": elapsed, "file": filename, "usage": usage}
    else:
        elapsed = time.monotonic() - start
        print(f"  {_fail} {model_name:<{pad}}  "
              f"{S.RED}parse failed{S.RST}  "
              f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": usage}

async def process_model_text(session: aiohttp.ClientSession, api_key: str, model_name: str, model_id: str, prompt: str,
                             output_dir_task: asyncio.Task, semaphore: asyncio.Semaphore,
                             results: Dict[str, Any], pad: int, tracker: Any) -> None:
    """Query a single model for a text response and save as Markdown."""
    start = time.monotonic()
    registered = tracker.is_running

    if registered:
        tracker.register(model_name)
    else:
        print(f"  {_wait} {model_name:<{pad}}  "
              f"{S.DIM}calling {model_id}…{S.RST}")

    content: Optional[str] = None
    usage: dict = {}

    try:
        async with semaphore:
            def _on_progress(chars: int) -> None:
                if registered:
                    tracker.update(model_name, chars)

            content, usage = await call_model_streaming(
                session, api_key, model_id, prompt,
                on_progress=_on_progress)

    except asyncio.CancelledError:
        elapsed = time.monotonic() - start
        print(f"  {_skip} {model_name:<{pad}}  "
              f"{S.DIM}cancelled  [{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "cancelled", "time_s": elapsed, "file": None, "usage": {}}
        return
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        print(f"  {_fail} {model_name:<{pad}}  "
              f"{S.RED}timeout{S.RST}  "
              f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return
    except aiohttp.ClientError as exc:
        elapsed = time.monotonic() - start
        print(f"  {_fail} {model_name:<{pad}}  "
              f"{S.RED}API error: {exc}{S.RST}  "
              f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return
    except Exception as exc:
        elapsed = time.monotonic() - start
        exc_str = str(exc) or exc.__class__.__name__
        print(f"  {_fail} {model_name:<{pad}}  "
              f"{S.RED}{exc_str}{S.RST}  "
              f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return
    finally:
        if registered:
            tracker.unregister(model_name)

    elapsed = time.monotonic() - start

    if not content:
        print(f"  {_fail} {model_name:<{pad}}  "
              f"{S.RED}no response{S.RST}  "
              f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
        results[model_name] = {
            "status": "failed", "time_s": elapsed, "file": None, "usage": {}}
        return

    output_dir = await output_dir_task
    filename = get_unique_filename(output_dir, model_name, ".md")
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write(content)

    elapsed = time.monotonic() - start
    print(f"  {_ok} {S.BOLD}{model_name:<{pad}}{S.RST}  "
          f"saved {_arrow} {S.GRN}{filename}{S.RST}  "
          f"{S.DIM}[{format_duration(elapsed)}]{S.RST}")
    results[model_name] = {
        "status": "success", "time_s": elapsed, "file": filename, "usage": usage}

async def main_async(args: Any, api_key: str, model_mapping: Optional[Dict[str, str]] = None) -> None:
    from llm_benchmarks.models import MODEL_MAPPING
    mapping = model_mapping if model_mapping is not None else MODEL_MAPPING
    pad = max((len(n) for n in mapping), default=12) + 4

    user_prompt = args.prompt
    text_mode = getattr(args, "text", False)

    if text_mode:
        sys_prompt = SYSTEM_PROMPT_TEXT
        full_prompt = f"{sys_prompt}\n\nQuestion: {user_prompt}"
        default_ext = ".md"
    else:
        sys_prompt = SYSTEM_PROMPT_CODE
        full_prompt = f"{sys_prompt}\n\nTask: {user_prompt}"
        default_ext = (
            ".py" if "python" in user_prompt.lower()
            or ".py" in user_prompt.lower()
            else ".html"
        )

    targets = list(mapping.items())
    if not targets:
        print(f"  {_fail} No models configured in MODEL_MAPPING.")
        return

    mode_label = f"{S.HYEL}TEXT{S.RST}" if text_mode else f"{S.HCYN}CODE{S.RST}"

    # ── Config display ─────────────────────────────────────────────────────
    _rule(heavy=True)
    print()
    print(f"  {S.DIM}{'MODE':>8}{S.RST}  {mode_label}")
    print(f"  {S.DIM}{'PROMPT':>8}{S.RST}  "
          f"{S.BOLD}{_truncate(user_prompt, _tw() - 14)}{S.RST}")
    print(f"  {S.DIM}{'MODELS':>8}{S.RST}  {len(targets)} active")
    print()
    _rule()
    print()

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=0, keepalive_timeout=30)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    results: Dict[str, Any] = {}
    output_dir_final = [None]
    t0 = time.monotonic()

    async with aiohttp.ClientSession(
        timeout=timeout, connector=connector,
    ) as session:
        tracker = ProgressTracker(len(targets), results, pad=pad)
        try:
            async def resolve_output_dir() -> str:
                dir_name = await get_directory_name(
                    session, api_key, user_prompt)
                
                base_out = os.path.join(os.getcwd(), OUTPUT_DIR)
                out = os.path.join(base_out, dir_name)
                os.makedirs(out, exist_ok=True)

                pf = os.path.join(out, "prompt.txt")
                if not os.path.exists(pf):
                    with open(pf, "w", encoding="utf-8") as fh:
                        fh.write(user_prompt)

                output_dir_final[0] = out
                print(f"  {S.DIM}{'OUTPUT':>8}{S.RST}  {out}")
                return out

            output_dir_task = asyncio.create_task(resolve_output_dir())
            await tracker.start()

            if text_mode:
                tasks = [
                    process_model_text(
                        session, api_key, name, mid, full_prompt,
                        output_dir_task, semaphore, results, pad,
                        tracker,
                    )
                    for name, mid in targets
                ]
            else:
                tasks = [
                    process_model(
                        session, api_key, name, mid, full_prompt,
                        default_ext, output_dir_task, semaphore, results, pad,
                        tracker,
                    )
                    for name, mid in targets
                ]
            await asyncio.gather(*tasks, return_exceptions=True)

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
    ok   = sum(1 for v in results.values() if v["status"] == "success")
    fail = sum(1 for v in results.values() if v["status"] == "failed")
    canc = sum(1 for v in results.values() if v["status"] == "cancelled")

    print()
    _rule("Run Results")
    print()

    # Ranked leaderboard — successes first (fastest wins), then failures
    def _rank_key(item: Any) -> Any:
        _, v = item
        order = {"success": 0, "failed": 1, "cancelled": 2}
        return (order.get(v["status"], 3), v["time_s"])

    for i, (name, info) in enumerate(
        sorted(results.items(), key=_rank_key), 1
    ):
        st = info["status"]
        t = format_duration(info["time_s"])

        if st == "success":
            sym = _ok
            usage = info.get("usage", {})
            tokens = usage.get("total_tokens")
            if tokens:
                detail = f"saved {_arrow} {S.GRN}{info['file']}{S.RST}  {S.DIM}{tokens:,} tk{S.RST}"
            else:
                detail = f"saved {_arrow} {S.GRN}{info['file']}{S.RST}"
        elif st == "cancelled":
            sym = _skip
            detail = f"{S.DIM}cancelled{S.RST}"
        else:
            sym = _fail
            detail = f"{S.RED}failed{S.RST}"

        rank = f"{S.DIM}{i:>2}.{S.RST}"
        print(f"  {rank} {sym} {_rpad(name, pad)}"
              f"  {_rpad(detail, 46)}  {S.DIM}{t}{S.RST}")

    print()
    parts = []
    if ok:   parts.append(f"{S.HGRN}{ok} passed{S.RST}")
    if fail: parts.append(f"{S.HRED}{fail} failed{S.RST}")
    if canc: parts.append(f"{S.DIM}{canc} cancelled{S.RST}")
    parts.append(f"{format_duration(total_time)} total")
    print(f"  {f' {_dot} '.join(parts)}")

    # ── Record run & show lifetime analytics ───────────────────────────────
    history = load_history()
    record_run(history, user_prompt, output_dir_final[0],
               total_time, results)
    display_analytics(history, compact=True, pad=pad)

    print()
    _rule(heavy=True)
    print()
