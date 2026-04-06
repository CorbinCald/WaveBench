import argparse
import sys
import os
import shutil
import asyncio
from concurrent.futures import ThreadPoolExecutor, Future

try:
    import readline
except ImportError:
    readline = None  # type: ignore[assignment]

from wavebench.api import load_api_key, fetch_top_models
from wavebench.models import MODEL_MAPPING
from wavebench.storage import load_models, save_models, load_config, save_config, load_history, _history_path
from wavebench.tui.styles import (
    _banner, S, _ok, _fail, _dot, _tw, _work,
    _box_top, _box_row, _box_bot,
    apply_theme,
)
import wavebench.tui.styles as _styles
from wavebench.tui.components import display_analytics, render_idle_wave
from wavebench.tui.interactive import run_config_menu, _read_key, _read_line, _TabEscape, _read_key_timeout
from wavebench.core import main_async

QUERY_HISTORY_FILE = ".benchmark_query_history"

def _query_history_path() -> str:
    return os.path.join(os.getcwd(), QUERY_HISTORY_FILE)

def _load_query_history() -> None:
    if readline is None:
        return
    path = _query_history_path()
    try:
        readline.clear_history()
    except Exception:
        pass
    if os.path.exists(path):
        try:
            readline.read_history_file(path)
        except Exception:
            pass
    try:
        readline.set_history_length(500)
    except Exception:
        pass

def _save_query_history(query: str) -> None:
    if readline is None or not query:
        return
    try:
        readline.add_history(query)
        readline.write_history_file(_query_history_path())
    except Exception:
        pass

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark LLMs via OpenRouter and track analytics.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--prompt", type=str, help="Prompt to send to all models")
    parser.add_argument(
        "--text", action="store_true",
        help="Text mode: get prose answers instead of code")
    parser.add_argument(
        "--stats", action="store_true",
        help="Show lifetime analytics and exit")
    parser.add_argument(
        "--clear-history", action="store_true",
        help="Reset analytics history")
    parser.add_argument(
        "--config", "--models", action="store_true", dest="config",
        help="Open the configuration menu (models & settings)")
    parser.add_argument(
        "--open", "--auto-open", dest="auto_open",
        choices=["incremental", "after_all"],
        default=None,
        help="Auto-open generated files (incremental or after_all)")
    parser.add_argument(
        "--auto-install", action="store_true", default=None,
        help="Auto-install detected dependencies in a venv")
    args = parser.parse_args()

    # ── Stats-only mode ────────────────────────────────────────────────────
    if args.stats:
        cfg = load_config()
        apply_theme(cfg.get("theme", "default"))
        print()
        print(_banner("WAVEBENCH"))
        history = load_history()
        display_analytics(history, compact=False,
                          sort_by=cfg.get("analytics_sort", "runs"))
        print()
        return

    # ── Clear history ──────────────────────────────────────────────────────
    if args.clear_history:
        path = _history_path()
        if os.path.exists(path):
            os.remove(path)
            print(f"\n  {_ok} History cleared.\n")
        else:
            print(f"\n  {S.DIM}No history to clear.{S.RST}\n")
        return

    # ── API key ────────────────────────────────────────────────────────────
    api_key = load_api_key()
    if not api_key:
        print(f"\n  {_fail} {S.BOLD}OPENROUTER_API_KEY{S.RST} not set.")
        print(f"     Set via environment variable or .env file.\n")
        sys.exit(1)

    # ── Pre-fetch models in background ────────────────────────────────────
    _executor = ThreadPoolExecutor(max_workers=1)
    models_future: Future = _executor.submit(fetch_top_models, api_key, 100)

    # ── Load persisted state ─────────────────────────────────────────────
    selected_models = load_models()
    config = load_config()
    apply_theme(config.get("theme", "default"))

    def _resolve_models_future() -> tuple:
        """Block on the background fetch and return (available, pricing)."""
        if not models_future.done():
            print(f"  {_work} {S.DIM}Fetching models from OpenRouter…{S.RST}")
            sys.stdout.flush()
        try:
            return models_future.result(timeout=30)
        except Exception:
            return [], {}

    if sys.stdout.isatty():
        sys.stdout.write('\033[2J\033[H')
        sys.stdout.flush()

    if args.config:
        print()
        print(_banner("WAVEBENCH"))
        print()
        new_models, new_config = run_config_menu(
            api_key, current_mapping=selected_models,
            current_config=config,
            prefetched=_resolve_models_future())
        if new_models is None:
            print(f"  {S.DIM}Cancelled.{S.RST}\n")
            return
        selected_models = new_models
        config = new_config
        save_models(selected_models)
        save_config(config)
        apply_theme(config.get("theme", "default"))

    # ── Interactive prompt ─────────────────────────────────────────────────
    if not args.prompt:
        text_from_cli = args.text

        def _print_mode_menu() -> None:
            active = (selected_models
                      if selected_models is not None
                      else MODEL_MAPPING)
            w = _tw() - 4
            row = (f"{_styles.ACCENT_HI}[1]{S.RST} Code  "
                   f"{_styles.ACCENT}[2]{S.RST} Text"
                   f"  {_dot}  "
                   f"{S.DIM}{len(active)} models{S.RST}  "
                   f"{_styles.ACCENT}[c]{S.RST} config")
            print(_box_top("Select Mode", w))
            print(_box_row(row, w))
            print(_box_bot(w))

        def _refresh_header() -> None:
            sys.stdout.write('\033[2J\033[H')
            sys.stdout.flush()
            print()
            print(_banner("WAVEBENCH"))
            print()

        _PROMPT_ROW = 9
        _wave_tick = 0

        def _wave_idle() -> None:
            nonlocal _wave_tick
            term = shutil.get_terminal_size((80, 24))
            _wt = _PROMPT_ROW + 1
            _wh = term.lines - _wt
            _ww = term.columns - 2
            if _wh >= 3 and _ww >= 10:
                _wf = render_idle_wave(_wave_tick, _ww, _wh)
                _buf = ['\x1b7']
                for _i, _rs in enumerate(_wf):
                    _buf.append(f'\x1b[{_wt + _i};2H{_rs}')
                _buf.append('\x1b8')
                sys.stdout.write(''.join(_buf))
                sys.stdout.flush()
            _wave_tick += 1

        _refresh_header()

        while True:
            text_mode = text_from_cli

            # ── Mode selection (skip if --text was passed on CLI) ─────
            if not text_from_cli:
                _print_mode_menu()
                mode_prompt = f"  {S.DIM}mode{S.RST} {_styles.ACCENT_HI}›{S.RST} "
                sys.stdout.write(mode_prompt)
                sys.stdout.write('\x1b7')
                sys.stdout.flush()

                mode_done = False
                while not mode_done:
                    key = _read_key_timeout(0.07)

                    if key is None:
                        _wave_idle()
                        continue

                    sys.stdout.write(
                        f'\x1b[{_PROMPT_ROW + 1};1H\x1b[J\x1b8')
                    sys.stdout.flush()

                    if key in ('tab', 'escape'):
                        sys.stdout.write('\n')
                        return
                    if key == 'ctrl-c':
                        print(f"\n  {S.DIM}Interrupted.{S.RST}\n")
                        return
                    if key == 'c':
                        sys.stdout.write('c\n')
                        new_m, new_c = run_config_menu(
                            api_key, current_mapping=selected_models,
                            current_config=config,
                            prefetched=_resolve_models_future())
                        if new_m is not None:
                            selected_models = new_m
                            config = new_c
                            save_models(selected_models)
                            save_config(config)
                            apply_theme(config.get("theme", "default"))
                        _refresh_header()
                        _print_mode_menu()
                        mode_prompt = f"  {S.DIM}mode{S.RST} {_styles.ACCENT_HI}›{S.RST} "
                        sys.stdout.write(mode_prompt)
                        sys.stdout.write('\x1b7')
                        sys.stdout.flush()
                        continue
                    if key == '2':
                        sys.stdout.write('2\n')
                        text_mode = True
                        mode_done = True
                    elif key == '1':
                        sys.stdout.write('1\n')
                        mode_done = True
                sys.stdout.write('\033[4A\r\033[J')
                sys.stdout.flush()

            active = (selected_models
                      if selected_models is not None else MODEL_MAPPING)
            names = list(active.keys())
            summary = ", ".join(names[:6])
            if len(names) > 6:
                summary += f", … (+{len(names) - 6})"
            w = _tw() - 4
            print(_box_top(f"{len(active)} Models", w))
            print(_box_row(summary, w))
            print(_box_bot(w))

            # ── Prompt input ──────────────────────────────────────────
            try:
                _load_query_history()
                history_entries: list[str] = []
                if readline:
                    for i in range(readline.get_current_history_length()):
                        entry = readline.get_history_item(i + 1)
                        if entry:
                            history_entries.append(entry)

                rl_prompt = f"  {_styles.ACCENT_HI}›{S.RST} "
                user_prompt = _read_line(rl_prompt, history=history_entries,
                                         on_idle=_wave_idle)
                sys.stdout.write(f'\x1b[{_PROMPT_ROW + 1};1H\x1b[J')
                sys.stdout.flush()

                if not user_prompt.strip():
                    _refresh_header()
                    continue
                _save_query_history(user_prompt)
            except _TabEscape:
                sys.stdout.write(f'\x1b[{_PROMPT_ROW + 1};1H\x1b[J')
                sys.stdout.flush()
                if text_from_cli:
                    return
                _refresh_header()
                continue
            except (KeyboardInterrupt, EOFError):
                sys.stdout.write(f'\x1b[{_PROMPT_ROW + 1};1H\x1b[J')
                sys.stdout.flush()
                print(f"  {S.DIM}Interrupted.{S.RST}\n")
                return
            args.prompt = user_prompt.strip()
            args.text = text_mode
            break
    else:
        print()

    _, run_pricing = _resolve_models_future()
    try:
        asyncio.run(main_async(args, api_key, model_mapping=selected_models,
                               config=config, pricing_lookup=run_pricing))
    except KeyboardInterrupt:
        print(f"\n\n  {S.DIM}Interrupted.{S.RST}\n")

if __name__ == "__main__":
    main()
