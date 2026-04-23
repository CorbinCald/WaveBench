"""Full-screen model selection menu and its OpenRouter-fetching wrapper.

``interactive_model_menu`` renders a paginated, searchable, toggleable
catalog of models with a cursor-based UI. ``run_model_selection`` is the
thin outer wrapper that fetches the catalog from OpenRouter before
entering the menu.

Requires a real TTY; degrades to a printed warning in non-interactive
environments.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
from typing import Any

from wavebench.api import fetch_top_models
from wavebench.models import MODEL_MAPPING
from wavebench.tui import styles as _styles
from wavebench.tui.input import _read_key_or_resize
from wavebench.tui.menus._shared import (
    MODEL_MENU_LIMIT,
    _filter_model_indices,
    _fit,
    _format_price,
    _is_printable_search_char,
    _unique_short_name,
)
from wavebench.tui.styles import (
    S,
    _dot,
    _rule,
    _work,
)

try:
    import termios  # noqa: F401
    import tty  # noqa: F401

    _HAS_TTY = True
except ImportError:
    _HAS_TTY = False

_HAS_SIGWINCH = hasattr(signal, "SIGWINCH")


def interactive_model_menu(
    available_models: list[dict[str, Any]],
    current_mapping: dict[str, str],
    pricing_lookup: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """Full-screen interactive model selector."""
    if not sys.stdin.isatty() or not _HAS_TTY:
        print(f"  {S.DIM}Interactive selection requires a terminal.{S.RST}")
        return None

    pricing_lookup = pricing_lookup or {}

    items = []
    seen_ids = set()
    existing_names = set()

    for short_name, model_id in current_mapping.items():
        items.append(
            {
                "short": short_name,
                "id": model_id,
                "selected": True,
                "pricing": _format_price(pricing_lookup.get(model_id, {})),
            }
        )
        seen_ids.add(model_id)
        existing_names.add(short_name)

    for m in available_models:
        if len(items) >= MODEL_MENU_LIMIT:
            break
        mid = m.get("id", "")

        if mid in seen_ids:
            continue
        seen_ids.add(mid)

        short = _unique_short_name(mid, existing_names)
        existing_names.add(short)

        items.append(
            {
                "short": short,
                "id": mid,
                "selected": False,
                "pricing": _format_price(pricing_lookup.get(mid, {})),
            }
        )

    if not items:
        print(f"  {S.DIM}No models available.{S.RST}")
        return None

    cursor_pos = 0
    _CHROME_LINES = 5
    page_size = max(1, min(14, shutil.get_terminal_size((80, 24)).lines - _CHROME_LINES))
    page_index = 0
    search_query = ""
    filtered_indices = list(range(len(items)))
    _nat_short_w = max(len(it["short"]) for it in items) + 2
    _nat_id_w = max(len(it["id"]) for it in items) + 2
    short_w = _nat_short_w
    id_w = _nat_id_w

    _max_price_w = max((len(it["pricing"]) for it in items if it["pricing"]), default=0)
    _overhead = 9 + (2 + _max_price_w if _max_price_w else 0)

    def _page_count() -> int:
        return max(1, (len(filtered_indices) + page_size - 1) // page_size)

    def _sync_page_from_cursor() -> None:
        nonlocal cursor_pos, page_index
        if not filtered_indices:
            page_index = 0
            return
        if cursor_pos not in filtered_indices:
            cursor_pos = filtered_indices[0]
        page_index = filtered_indices.index(cursor_pos) // page_size

    def _page_bounds() -> tuple[int, int]:
        start = page_index * page_size
        end = min(start + page_size, len(filtered_indices))
        return start, end

    def _refresh_filter(preserve_current: bool = True) -> None:
        nonlocal cursor_pos, page_index, filtered_indices
        current = cursor_pos
        filtered_indices = _filter_model_indices(items, search_query)
        if not filtered_indices:
            page_index = 0
            return
        if preserve_current and current in filtered_indices:
            cursor_pos = current
        else:
            cursor_pos = filtered_indices[0]
        _sync_page_from_cursor()

    def render() -> None:
        nonlocal page_size, short_w, id_w, page_index
        term = shutil.get_terminal_size((80, 24))
        new_ps = max(1, min(14, term.lines - _CHROME_LINES))
        if new_ps != page_size:
            page_size = new_ps
            if filtered_indices and cursor_pos in filtered_indices:
                page_index = filtered_indices.index(cursor_pos) // page_size
        cols = min(120, term.columns)
        _avail = max(10, cols - _overhead)
        short_w, id_w = _nat_short_w, _nat_id_w
        if short_w + id_w > _avail:
            ratio = _avail / (short_w + id_w)
            short_w = max(6, int(_nat_short_w * ratio))
            id_w = max(6, _avail - short_w)

        sys.stdout.write("\033[H")
        buf: list[str] = []

        pcount = _page_count()
        hl = (
            f"  {S.DIM}↑↓{S.RST} navigate  {_dot}  "
            f"{S.DIM}Space{S.RST} toggle  {_dot}  "
            f"{S.DIM}Enter/Tab{S.RST} confirm  {_dot}  "
            f"{S.DIM}^A{S.RST} all  {_dot}  "
            f"{S.DIM}^N{S.RST} none  {_dot}  "
            f"{S.DIM}[ ]{S.RST} page  {_dot}  "
            f"{S.DIM}Esc{S.RST} cancel"
        )
        buf.append(f"\033[K{hl}\n")

        search_label = f"{_styles.ACCENT_HI}search{S.RST}" if search_query else "search"
        query_display = search_query or f"{S.DIM}type to filter{S.RST}"
        buf.append(f"\033[K  {search_label}: {query_display}\n")

        buf.append(f"\033[K  {S.DIM}page {page_index + 1}/{pcount}{S.RST}\n")

        start, end = _page_bounds()
        visible_indices = filtered_indices[start:end]
        for i in visible_indices:
            item = items[i]
            is_cur = i == cursor_pos
            chk = f"{S.HGRN}✓{S.RST}" if item["selected"] else " "

            sn = _fit(item["short"], short_w)
            mi = _fit(item["id"], id_w)
            if is_cur:
                mk = f"{_styles.ACCENT_HI}▸{S.RST}"
                ns = f"{S.BOLD}{sn:<{short_w}}{S.RST}"
                ids = f"{_styles.ACCENT}{mi:<{id_w}}{S.RST}"
            else:
                mk = " "
                ns = f"{sn:<{short_w}}"
                ids = f"{S.DIM}{mi:<{id_w}}{S.RST}"

            ps = f"  {S.DIM}{item['pricing']}{S.RST}" if item["pricing"] else ""
            buf.append(f"\033[K  {mk} [{chk}] {ns} {ids}{ps}\n")
        if not visible_indices:
            buf.append(f"\033[K  {S.DIM}No models match the current search.{S.RST}\n")
            blank_rows = page_size - 1
        else:
            blank_rows = page_size - len(visible_indices)
        for _ in range(blank_rows):
            buf.append("\033[K\n")

        buf.append("\033[K\n")
        sel = sum(1 for it in items if it["selected"])
        buf.append(
            f"\033[K  {S.BOLD}{sel}{S.RST} of {len(items)} selected  "
            f"{S.DIM}{len(filtered_indices)} shown{S.RST}\n"
        )
        buf.append("\033[J")

        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    if _HAS_SIGWINCH:
        _wr, _ww = os.pipe()
        os.set_blocking(_wr, False)
        os.set_blocking(_ww, False)

        def _on_winch(sig, frame):
            try:
                os.write(_ww, b"\x00")
            except OSError:
                pass

        _old_sigwinch = signal.signal(signal.SIGWINCH, _on_winch)
    else:
        _wr = -1

    sys.stdout.write("\033[?1049h\033[?25l")
    try:
        render()
        while True:
            key = _read_key_or_resize(_wr)
            if key == "resize":
                render()
                continue
            if key in ("escape", "ctrl-c"):
                sys.stdout.write("\033[?25h\033[?1049l")
                print()
                return None
            elif key == "up" and filtered_indices:
                idx = filtered_indices.index(cursor_pos)
                cursor_pos = filtered_indices[(idx - 1) % len(filtered_indices)]
                _sync_page_from_cursor()
            elif key == "down" and filtered_indices:
                idx = filtered_indices.index(cursor_pos)
                cursor_pos = filtered_indices[(idx + 1) % len(filtered_indices)]
                _sync_page_from_cursor()
            elif key == "space" and filtered_indices:
                items[cursor_pos]["selected"] = not items[cursor_pos]["selected"]
            elif key in ("[", "]") and filtered_indices:
                pcount = _page_count()
                if key == "[":
                    page_index = (page_index - 1) % pcount
                else:
                    page_index = (page_index + 1) % pcount
                start, end = _page_bounds()
                cursor_pos = filtered_indices[start if start < end else 0]
            elif key in ("enter", "tab"):
                break
            elif key == "ctrl-a":
                for it in items:
                    it["selected"] = True
            elif key == "ctrl-n":
                for it in items:
                    it["selected"] = False
            elif key == "backspace":
                if search_query:
                    search_query = search_query[:-1]
                    _refresh_filter(preserve_current=False)
            elif _is_printable_search_char(key):
                search_query += key
                _refresh_filter(preserve_current=False)
            render()
    finally:
        sys.stdout.write("\033[?25h\033[?1049l")
        if _HAS_SIGWINCH:
            signal.signal(signal.SIGWINCH, _old_sigwinch)
            os.close(_wr)
            os.close(_ww)

    selected = {it["short"]: it["id"] for it in items if it["selected"]}
    if not selected:
        print(f"\n  {S.HYEL}No models selected — keeping current config.{S.RST}")
        return None

    print()
    return selected


def run_model_selection(
    api_key: str, current_mapping: dict[str, str] | None = None
) -> dict[str, str] | None:
    """Fetch available models from OpenRouter and open the selector."""
    print(f"  {_work} {S.DIM}Fetching models from OpenRouter…{S.RST}")
    available, pricing_lookup = fetch_top_models(api_key, count=100)
    if not available:
        print(f"  {S.DIM}Could not fetch remote models — showing local config only.{S.RST}")
    print()
    _rule("Model Selection")
    print()
    if current_mapping is None:
        current_mapping = MODEL_MAPPING
    return interactive_model_menu(available, current_mapping, pricing_lookup=pricing_lookup)
