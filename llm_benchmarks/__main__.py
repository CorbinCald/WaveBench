import argparse
import sys
import os
import asyncio

try:
    import readline
except ImportError:
    readline = None  # type: ignore[assignment]

from llm_benchmarks.api import load_api_key
from llm_benchmarks.models import MODEL_MAPPING
from llm_benchmarks.storage import load_models, save_models, load_config, save_config, load_history, _history_path
from llm_benchmarks.tui.styles import _rule, S, _ok, _fail
from llm_benchmarks.tui.components import display_analytics
from llm_benchmarks.tui.interactive import run_config_menu
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
        display_analytics(history, compact=False)
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

    # ── Load persisted state ─────────────────────────────────────────────
    selected_models = load_models()
    config = load_config()

    if args.config:
        print()
        _rule("LLM BENCHMARK", heavy=True)
        print()
        new_models, new_config = run_config_menu(
            api_key, current_mapping=selected_models,
            current_config=config)
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

        # ── Mode selection (skip if --text was passed on CLI) ─────────
        if not args.text:
            def _print_mode_menu() -> None:
                print(f"  {S.DIM}Select mode:{S.RST}  "
                      f"{S.HCYN}[1]{S.RST} Code  "
                      f"{S.HYEL}[2]{S.RST} Text")
                active = (selected_models
                          if selected_models is not None
                          else MODEL_MAPPING)
                print(f"  {S.DIM}{len(active)} models active{S.RST}  "
                      f"{S.BLU}[c]{S.RST} config")
            _print_mode_menu()

            while True:
                try:
                    mode_input = input(
                        f"  \001{S.DIM}\002mode\001{S.RST}\002 "
                        f"\001{S.HCYN}\002›\001{S.RST}\002 ").strip()
                except (KeyboardInterrupt, EOFError):
                    print(f"\n  {S.DIM}Interrupted.{S.RST}\n")
                    return

                if mode_input.lower() == "c":
                    print()
                    new_m, new_c = run_config_menu(
                        api_key, current_mapping=selected_models,
                        current_config=config)
                    if new_m is not None:
                        selected_models = new_m
                        config = new_c
                        save_models(selected_models)
                        save_config(config)
                    else:
                        print(f"  {S.DIM}Cancelled — "
                              f"keeping current config.{S.RST}")
                    print()
                    _print_mode_menu()
                    continue

                if mode_input == "2":
                    args.text = True
                break
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

        try:
            # \001 / \002 tell readline that enclosed chars are non-printing
            # so cursor-position math stays correct with ANSI colours.
            _load_query_history()
            _rl_prompt = (f"  \001{S.HCYN}\002›\001{S.RST}\002 ")
            args.prompt = input(_rl_prompt).strip()
            if not args.prompt:
                print(f"  {S.DIM}No prompt provided.{S.RST}\n")
                return
            _save_query_history(args.prompt)
        except (KeyboardInterrupt, EOFError):
            print(f"\n  {S.DIM}Interrupted.{S.RST}\n")
            return
        print()
    else:
        print()

    try:
        asyncio.run(main_async(args, api_key, model_mapping=selected_models))
    except KeyboardInterrupt:
        print(f"\n\n  {S.DIM}Interrupted.{S.RST}\n")

if __name__ == "__main__":
    main()
