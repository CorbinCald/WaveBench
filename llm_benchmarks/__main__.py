import argparse
import sys
import os
import asyncio
from concurrent.futures import ThreadPoolExecutor, Future

try:
    import readline
except ImportError:
    readline = None  # type: ignore[assignment]

from llm_benchmarks.api import load_api_key, fetch_top_models
from llm_benchmarks.models import MODEL_MAPPING
from llm_benchmarks.storage import load_models, save_models, load_config, save_config, load_history, _history_path
from llm_benchmarks.tui.styles import _rule, S, _ok, _fail
from llm_benchmarks.tui.components import display_analytics
from llm_benchmarks.tui.interactive import run_config_menu, _read_key, _read_line, _TabEscape
from llm_benchmarks.core import main_async

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
    args = parser.parse_args()

    # ── Stats-only mode ────────────────────────────────────────────────────
    if args.stats:
        print()
        _rule("LLM BENCHMARK", heavy=True)
        history = load_history()
        cfg = load_config()
        display_analytics(history, compact=False,
                          sort_by=cfg.get("analytics_sort", "runs"))
        print()
        _rule(heavy=True)
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

    def _resolve_models_future() -> tuple:
        """Block on the background fetch and return (available, pricing)."""
        try:
            return models_future.result(timeout=30)
        except Exception:
            return [], {}

    if sys.stdout.isatty():
        sys.stdout.write('\033[2J\033[H')
        sys.stdout.flush()

    if args.config:
        print()
        _rule("LLM BENCHMARK", heavy=True)
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

    # ── Interactive prompt ─────────────────────────────────────────────────
    if not args.prompt:
        if not args.config:
            print()
            _rule("LLM BENCHMARK", heavy=True)
            print()

        text_from_cli = args.text

        def _print_mode_menu() -> None:
            active = (selected_models
                      if selected_models is not None
                      else MODEL_MAPPING)
            print(f"  {S.DIM}Select mode:{S.RST}  "
                  f"{S.HCYN}[1]{S.RST} Code  "
                  f"{S.HYEL}[2]{S.RST} Text")
            print(f"  {S.DIM}{len(active)} models active{S.RST}  "
                  f"{S.BLU}[c]{S.RST} config")

        def _refresh_header() -> None:
            sys.stdout.write('\033[2J\033[H')
            sys.stdout.flush()
            print()
            _rule("LLM BENCHMARK", heavy=True)
            print()

        while True:
            text_mode = text_from_cli

            # ── Mode selection (skip if --text was passed on CLI) ─────
            if not text_from_cli:
                _print_mode_menu()
                mode_prompt = f"  {S.DIM}mode{S.RST} {S.HCYN}›{S.RST} "
                sys.stdout.write(mode_prompt)
                sys.stdout.flush()

                mode_done = False
                while not mode_done:
                    key = _read_key()
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
                        _refresh_header()
                        _print_mode_menu()
                        sys.stdout.write(mode_prompt)
                        sys.stdout.flush()
                        continue
                    if key == '2':
                        sys.stdout.write('2\n')
                        text_mode = True
                        mode_done = True
                    elif key == '1':
                        sys.stdout.write('1\n')
                        mode_done = True
                print()

            # Show active models summary
            active = (selected_models
                      if selected_models is not None else MODEL_MAPPING)
            names = list(active.keys())
            summary = ", ".join(names[:6])
            if len(names) > 6:
                summary += f", … (+{len(names) - 6})"
            print(f"  {S.DIM}{len(active)} models:{S.RST} {summary}")
            print()

            # ── Prompt input ──────────────────────────────────────────
            try:
                _load_query_history()
                history_entries: list[str] = []
                if readline:
                    for i in range(readline.get_current_history_length()):
                        entry = readline.get_history_item(i + 1)
                        if entry:
                            history_entries.append(entry)

                rl_prompt = f"  {S.HCYN}›{S.RST} "
                user_prompt = _read_line(rl_prompt, history=history_entries)

                if not user_prompt.strip():
                    _refresh_header()
                    continue
                _save_query_history(user_prompt)
            except _TabEscape:
                if text_from_cli:
                    return
                _refresh_header()
                continue
            except (KeyboardInterrupt, EOFError):
                print(f"  {S.DIM}Interrupted.{S.RST}\n")
                return
            print()

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
