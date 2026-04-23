import os
import sys
import re
import shutil
import select
import signal
from typing import Callable, Dict, Any, Optional, List, Tuple

try:
    import tty
    import termios
    _HAS_TTY = True
except ImportError:
    _HAS_TTY = False

_HAS_SIGWINCH = hasattr(signal, 'SIGWINCH')

from wavebench.tui.styles import (
    S, _dot, _rule, _work, _vlen, _box_top, _box_row, _box_bot, THEMES,
)
import wavebench.tui.styles as _styles
from wavebench.api import fetch_top_models
from wavebench.models import MODEL_MAPPING

MODEL_MENU_LIMIT = 100

def _format_price(pricing_dict: Dict[str, Any]) -> str:
    """Format OpenRouter pricing as '$in/$out /M' (per million tokens)."""
    try:
        pp = float(pricing_dict.get("prompt") or 0) * 1_000_000
        cp = float(pricing_dict.get("completion") or 0) * 1_000_000
    except (TypeError, ValueError):
        return ""
    if pp == 0 and cp == 0:
        return ""
    return f"${pp:,.2f}/${cp:,.2f} /M"

def _generate_short_name(model_id: str) -> str:
    """Generate a camelCase short name from 'provider/model-name-v1'."""
    name = model_id.split('/')[-1] if '/' in model_id else model_id
    parts = re.split(r'[-_]+', name)
    if not parts:
        return name
    result = parts[0].lower()
    for p in parts[1:]:
        if p:
            result += (p if p[0].isdigit() else p[0].upper() + p[1:])
    return result

def _unique_short_name(model_id: str, existing_names: set) -> str:
    """Generate a unique short name that doesn't collide with *existing_names*."""
    base = _generate_short_name(model_id)
    if base not in existing_names:
        return base
    counter = 2
    while f"{base}_{counter}" in existing_names:
        counter += 1
    return f"{base}_{counter}"

def _fit(text: str, width: int) -> str:
    """Truncate *text* to *width*, adding ellipsis if needed."""
    return text if len(text) <= width else text[:width - 1] + "…"


def _filter_model_indices(items: List[Dict[str, Any]], query: str) -> List[int]:
    """Return item indices matching the current search query."""
    needle = query.strip().lower()
    if not needle:
        return list(range(len(items)))
    return [
        i for i, item in enumerate(items)
        if needle in item['short'].lower() or needle in item['id'].lower()
    ]


def _is_printable_search_char(key: str) -> bool:
    """Allow plain printable characters in search mode."""
    return len(key) == 1 and key.isprintable() and key not in ("\t", "\r", "\n")


def _read_key() -> str:
    """Read a single keypress from the terminal, handling escape sequences."""
    if not _HAS_TTY:
        ch = input()[:1]
        return ch or 'enter'
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == '\x1b':
            ch2 = sys.stdin.read(1)
            if ch2 == '[':
                ch3 = sys.stdin.read(1)
                return {'A': 'up', 'B': 'down', 'C': 'right', 'D': 'left'}.get(ch3, '')
            return 'escape'
        if ch in ('\r', '\n'):
            return 'enter'
        if ch == '\t':
            return 'tab'
        if ch == ' ':
            return 'space'
        if ch == '\x03':
            return 'ctrl-c'
        if ch == '\x01':
            return 'ctrl-a'
        if ch == '\x0e':
            return 'ctrl-n'
        if ch in ('\x7f', '\b'):
            return 'backspace'
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_key_or_resize(winch_r: int = -1) -> str:
    """Read a single keypress, returning ``'resize'`` if SIGWINCH fires."""
    if not _HAS_TTY:
        ch = input()[:1]
        return ch or 'enter'
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        watch: list = [fd]
        if winch_r >= 0:
            watch.append(winch_r)
        try:
            ready, _, _ = select.select(watch, [], [])
        except (OSError, ValueError):
            return 'resize'
        if winch_r >= 0 and winch_r in ready:
            try:
                os.read(winch_r, 1024)
            except OSError:
                pass
            return 'resize'
        ch = os.read(fd, 1)
        if not ch:
            return ''
        ch = ch.decode('utf-8', errors='replace')
        if ch == '\x1b':
            esc_ready, _, _ = select.select([fd], [], [], 0.05)
            if not esc_ready:
                return 'escape'
            ch2 = os.read(fd, 1).decode('utf-8', errors='replace')
            if ch2 == '[':
                ch3 = os.read(fd, 1).decode('utf-8', errors='replace')
                return {'A': 'up', 'B': 'down', 'C': 'right',
                        'D': 'left'}.get(ch3, '')
            return 'escape'
        if ch in ('\r', '\n'):
            return 'enter'
        if ch == '\t':
            return 'tab'
        if ch == ' ':
            return 'space'
        if ch == '\x03':
            return 'ctrl-c'
        if ch == '\x01':
            return 'ctrl-a'
        if ch == '\x0e':
            return 'ctrl-n'
        if ch in ('\x7f', '\b'):
            return 'backspace'
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_key_timeout(timeout_s: float = 0.08) -> Optional[str]:
    """Read a single keypress, returning ``None`` if *timeout_s* elapses."""
    if not _HAS_TTY or not sys.stdin.isatty():
        ch = input()[:1]
        return ch or 'enter'
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ready, _, _ = select.select([fd], [], [], timeout_s)
        if not ready:
            return None
        ch = os.read(fd, 1)
        if not ch:
            return None
        ch = ch.decode('utf-8', errors='replace')
        if ch == '\x1b':
            esc_ready, _, _ = select.select([fd], [], [], 0.05)
            if not esc_ready:
                return 'escape'
            ch2 = os.read(fd, 1).decode('utf-8', errors='replace')
            if ch2 == '[':
                ch3 = os.read(fd, 1).decode('utf-8', errors='replace')
                return {'A': 'up', 'B': 'down', 'C': 'right',
                        'D': 'left'}.get(ch3, '')
            return 'escape'
        if ch in ('\r', '\n'):
            return 'enter'
        if ch == '\t':
            return 'tab'
        if ch == ' ':
            return 'space'
        if ch == '\x03':
            return 'ctrl-c'
        if ch == '\x01':
            return 'ctrl-a'
        if ch == '\x0e':
            return 'ctrl-n'
        if ch in ('\x7f', '\b'):
            return 'backspace'
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


class _TabEscape(Exception):
    """Raised when Tab or Escape is pressed to navigate back."""
    pass


def _redraw_input(prompt: str, buf: list, cursor: int) -> None:
    """Redraw the input line, scrolling horizontally for long input."""
    term_w = shutil.get_terminal_size((80, 24)).columns
    prompt_w = _vlen(prompt)
    avail = max(1, term_w - prompt_w - 1)

    text = "".join(buf)

    if len(text) <= avail:
        visible = text
        vis_cursor = cursor
    else:
        half = avail // 2
        start = max(0, cursor - half)
        if start + avail > len(text):
            start = max(0, len(text) - avail)
        visible = text[start:start + avail]
        vis_cursor = cursor - start

    sys.stdout.write(f'\r{prompt}{visible}\033[K')
    back = len(visible) - vis_cursor
    if back > 0:
        sys.stdout.write(f'\033[{back}D')
    sys.stdout.flush()


def _read_line(prompt: str, history: Optional[List[str]] = None,
               on_idle: Optional[Callable] = None,
               idle_timeout: float = 0.07) -> str:
    """Read a line with basic editing and history.

    Supports left/right, home/end, backspace/delete, Ctrl+A/E/K/U/W,
    up/down for history, and raises *_TabEscape* on Tab or Escape.
    If *on_idle* is provided, it is called while waiting for input.
    """
    if not _HAS_TTY:
        return input(re.sub(r'\033\[[0-9;]*m', '', prompt))

    sys.stdout.write(prompt)
    sys.stdout.flush()

    buf: List[str] = []
    cursor = 0
    hist = list(history or [])
    hist.append("")
    hist_pos = len(hist) - 1

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    _idle_timeout = idle_timeout if on_idle else None
    try:
        tty.setraw(fd)
        sys.stdout.write('\033[?2004h')  # enable bracketed paste
        sys.stdout.flush()
        while True:
            ready, _, _ = select.select([fd], [], [], _idle_timeout)
            if not ready:
                if on_idle:
                    on_idle()
                continue
            raw = os.read(fd, 1)
            if not raw:
                continue
            b = raw[0]
            if b >= 0xC0:
                if b < 0xE0:
                    raw += os.read(fd, 1)
                elif b < 0xF0:
                    raw += os.read(fd, 2)
                else:
                    raw += os.read(fd, 3)
            ch = raw.decode('utf-8', errors='replace')

            if ch == '\x1b':
                esc_ready, _, _ = select.select([fd], [], [], 0.05)
                if not esc_ready:
                    raise _TabEscape()
                ch2 = os.read(fd, 1).decode('utf-8', errors='replace')
                if ch2 == '[':
                    ch3 = os.read(fd, 1).decode('utf-8', errors='replace')
                    if ch3 == 'A' and hist_pos > 0:
                        hist[hist_pos] = ''.join(buf)
                        hist_pos -= 1
                        buf = list(hist[hist_pos])
                        cursor = len(buf)
                        _redraw_input(prompt, buf, cursor)
                    elif ch3 == 'B' and hist_pos < len(hist) - 1:
                        hist[hist_pos] = ''.join(buf)
                        hist_pos += 1
                        buf = list(hist[hist_pos])
                        cursor = len(buf)
                        _redraw_input(prompt, buf, cursor)
                    elif ch3 == 'C' and cursor < len(buf):
                        cursor += 1
                        sys.stdout.write('\033[C')
                        sys.stdout.flush()
                    elif ch3 == 'D' and cursor > 0:
                        cursor -= 1
                        sys.stdout.write('\033[D')
                        sys.stdout.flush()
                    elif ch3 == 'H':
                        cursor = 0
                        _redraw_input(prompt, buf, cursor)
                    elif ch3 == 'F':
                        cursor = len(buf)
                        _redraw_input(prompt, buf, cursor)
                    elif ch3 in '2345678':
                        ch4 = os.read(fd, 1).decode('utf-8', errors='replace')
                        if ch3 == '3' and ch4 == '~' and cursor < len(buf):
                            buf.pop(cursor)
                            _redraw_input(prompt, buf, cursor)
                        elif ch3 == '2' and ch4 == '0':
                            # Bracketed paste: \033[200~ ... \033[201~
                            ch5 = os.read(fd, 1).decode('utf-8', errors='replace')
                            ch6 = os.read(fd, 1).decode('utf-8', errors='replace')
                            if ch5 == '0' and ch6 == '~':
                                paste_chars: List[str] = []
                                while True:
                                    pr = os.read(fd, 1)
                                    if not pr:
                                        break
                                    pb = pr[0]
                                    if pb >= 0xC0:
                                        if pb < 0xE0:
                                            pr += os.read(fd, 1)
                                        elif pb < 0xF0:
                                            pr += os.read(fd, 2)
                                        else:
                                            pr += os.read(fd, 3)
                                    pch = pr.decode('utf-8', errors='replace')
                                    if pch == '\x1b':
                                        p2 = os.read(fd, 1).decode('utf-8', errors='replace')
                                        if p2 == '[':
                                            p3 = os.read(fd, 1).decode('utf-8', errors='replace')
                                            p4 = os.read(fd, 1).decode('utf-8', errors='replace')
                                            p5 = os.read(fd, 1).decode('utf-8', errors='replace')
                                            p6 = os.read(fd, 1).decode('utf-8', errors='replace')
                                            if p3 + p4 + p5 + p6 == '201~':
                                                break
                                        continue
                                    if pch == '\r':
                                        continue  # skip CR (part of \r\n)
                                    if pch == '\n':
                                        paste_chars.append(' ')
                                    elif pch.isprintable():
                                        paste_chars.append(pch)
                                if paste_chars:
                                    buf[cursor:cursor] = paste_chars
                                    cursor += len(paste_chars)
                                    _redraw_input(prompt, buf, cursor)
                else:
                    raise _TabEscape()

            elif ch == '\t':
                raise _TabEscape()

            elif ch in ('\r', '\n'):
                sys.stdout.write('\r\n')
                sys.stdout.flush()
                return ''.join(buf)

            elif ch == '\x03':
                raise KeyboardInterrupt()

            elif ch == '\x04' and not buf:
                raise EOFError()

            elif ch in ('\x7f', '\b') and cursor > 0:
                buf.pop(cursor - 1)
                cursor -= 1
                _redraw_input(prompt, buf, cursor)

            elif ch == '\x01':
                cursor = 0
                _redraw_input(prompt, buf, cursor)

            elif ch == '\x05':
                cursor = len(buf)
                _redraw_input(prompt, buf, cursor)

            elif ch == '\x0b':
                buf = buf[:cursor]
                _redraw_input(prompt, buf, cursor)

            elif ch == '\x15':
                buf = buf[cursor:]
                cursor = 0
                _redraw_input(prompt, buf, cursor)

            elif ch == '\x17':
                while cursor > 0 and buf[cursor - 1] == ' ':
                    buf.pop(cursor - 1)
                    cursor -= 1
                while cursor > 0 and buf[cursor - 1] != ' ':
                    buf.pop(cursor - 1)
                    cursor -= 1
                _redraw_input(prompt, buf, cursor)

            elif ch.isprintable():
                buf.insert(cursor, ch)
                cursor += 1
                # Defer redraw while more input is immediately available
                # (batches rapid input like paste in non-bracketed terminals)
                if not select.select([fd], [], [], 0)[0]:
                    _redraw_input(prompt, buf, cursor)
    except _TabEscape:
        sys.stdout.write('\r\033[K')
        sys.stdout.flush()
        raise
    except (KeyboardInterrupt, EOFError):
        sys.stdout.write('\r\n')
        sys.stdout.flush()
        raise
    finally:
        sys.stdout.write('\033[?2004l')  # disable bracketed paste
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def interactive_model_menu(available_models: List[Dict[str, Any]], current_mapping: Dict[str, str],
                           pricing_lookup: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, str]]:
    """Full-screen interactive model selector."""
    if not sys.stdin.isatty() or not _HAS_TTY:
        print(f"  {S.DIM}Interactive selection requires a terminal.{S.RST}")
        return None

    pricing_lookup = pricing_lookup or {}

    items = []
    seen_ids = set()
    existing_names = set()

    for short_name, model_id in current_mapping.items():
        items.append({
            'short': short_name, 'id': model_id,
            'selected': True,
            'pricing': _format_price(pricing_lookup.get(model_id, {})),
        })
        seen_ids.add(model_id)
        existing_names.add(short_name)

    for m in available_models:
        if len(items) >= MODEL_MENU_LIMIT:
            break
        mid = m.get('id', '')

        if mid in seen_ids:
            continue
        seen_ids.add(mid)

        short = _unique_short_name(mid, existing_names)
        existing_names.add(short)

        items.append({
            'short': short, 'id': mid,
            'selected': False,
            'pricing': _format_price(pricing_lookup.get(mid, {})),
        })

    if not items:
        print(f"  {S.DIM}No models available.{S.RST}")
        return None

    cursor_pos = 0
    _CHROME_LINES = 5
    page_size = max(1, min(14, shutil.get_terminal_size((80, 24)).lines - _CHROME_LINES))
    page_index = 0
    search_query = ""
    filtered_indices = list(range(len(items)))
    _nat_short_w = max(len(it['short']) for it in items) + 2
    _nat_id_w = max(len(it['id']) for it in items) + 2
    short_w = _nat_short_w
    id_w = _nat_id_w

    _max_price_w = max((len(it['pricing']) for it in items if it['pricing']), default=0)
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

    def _page_bounds() -> Tuple[int, int]:
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
        hl = (f"  {S.DIM}↑↓{S.RST} navigate  {_dot}  "
              f"{S.DIM}Space{S.RST} toggle  {_dot}  "
              f"{S.DIM}Enter/Tab{S.RST} confirm  {_dot}  "
              f"{S.DIM}^A{S.RST} all  {_dot}  "
              f"{S.DIM}^N{S.RST} none  {_dot}  "
              f"{S.DIM}[ ]{S.RST} page  {_dot}  "
              f"{S.DIM}Esc{S.RST} cancel")
        buf.append(f"\033[K{hl}\n")

        search_label = (f"{_styles.ACCENT_HI}search{S.RST}"
                        if search_query else "search")
        query_display = search_query or f"{S.DIM}type to filter{S.RST}"
        buf.append(f"\033[K  {search_label}: {query_display}\n")

        buf.append(
            f"\033[K  {S.DIM}page {page_index + 1}/{pcount}{S.RST}\n")

        start, end = _page_bounds()
        visible_indices = filtered_indices[start:end]
        for i in visible_indices:
            item = items[i]
            is_cur = i == cursor_pos
            chk = f"{S.HGRN}✓{S.RST}" if item['selected'] else " "

            sn = _fit(item['short'], short_w)
            mi = _fit(item['id'], id_w)
            if is_cur:
                mk = f"{_styles.ACCENT_HI}▸{S.RST}"
                ns = f"{S.BOLD}{sn:<{short_w}}{S.RST}"
                ids = f"{_styles.ACCENT}{mi:<{id_w}}{S.RST}"
            else:
                mk = " "
                ns = f"{sn:<{short_w}}"
                ids = f"{S.DIM}{mi:<{id_w}}{S.RST}"

            ps = (f"  {S.DIM}{item['pricing']}{S.RST}"
                  if item['pricing'] else "")
            buf.append(f"\033[K  {mk} [{chk}] {ns} {ids}{ps}\n")
        if not visible_indices:
            buf.append(
                f"\033[K  {S.DIM}No models match the current search."
                f"{S.RST}\n")
            blank_rows = page_size - 1
        else:
            blank_rows = page_size - len(visible_indices)
        for _ in range(blank_rows):
            buf.append("\033[K\n")

        buf.append("\033[K\n")
        sel = sum(1 for it in items if it['selected'])
        buf.append(
            f"\033[K  {S.BOLD}{sel}{S.RST} of {len(items)} selected  "
            f"{S.DIM}{len(filtered_indices)} shown{S.RST}\n")
        buf.append("\033[J")

        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    if _HAS_SIGWINCH:
        _wr, _ww = os.pipe()
        os.set_blocking(_wr, False)
        os.set_blocking(_ww, False)
        def _on_winch(sig, frame):
            try:
                os.write(_ww, b'\x00')
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
            if key == 'resize':
                render()
                continue
            if key in ('escape', 'ctrl-c'):
                sys.stdout.write("\033[?25h\033[?1049l")
                print()
                return None
            elif key == 'up' and filtered_indices:
                idx = filtered_indices.index(cursor_pos)
                cursor_pos = filtered_indices[(idx - 1) % len(filtered_indices)]
                _sync_page_from_cursor()
            elif key == 'down' and filtered_indices:
                idx = filtered_indices.index(cursor_pos)
                cursor_pos = filtered_indices[(idx + 1) % len(filtered_indices)]
                _sync_page_from_cursor()
            elif key == 'space' and filtered_indices:
                items[cursor_pos]['selected'] = \
                    not items[cursor_pos]['selected']
            elif key in ('[', ']') and filtered_indices:
                pcount = _page_count()
                if key == '[':
                    page_index = (page_index - 1) % pcount
                else:
                    page_index = (page_index + 1) % pcount
                start, end = _page_bounds()
                cursor_pos = filtered_indices[start if start < end else 0]
            elif key in ('enter', 'tab'):
                break
            elif key == 'ctrl-a':
                for it in items:
                    it['selected'] = True
            elif key == 'ctrl-n':
                for it in items:
                    it['selected'] = False
            elif key == 'backspace':
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

    selected = {it['short']: it['id'] for it in items if it['selected']}
    if not selected:
        print(f"\n  {S.HYEL}No models selected — keeping current config."
              f"{S.RST}")
        return None

    print()
    return selected

def run_model_selection(api_key: str, current_mapping: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
    """Fetch available models from OpenRouter and open the selector."""
    print(f"  {_work} {S.DIM}Fetching models from OpenRouter…{S.RST}")
    available, pricing_lookup = fetch_top_models(api_key, count=100)
    if not available:
        print(f"  {S.DIM}Could not fetch remote models — "
              f"showing local config only.{S.RST}")
    print()
    _rule("Model Selection")
    print()
    if current_mapping is None:
        current_mapping = MODEL_MAPPING
    return interactive_model_menu(available, current_mapping,
                                  pricing_lookup=pricing_lookup)

def interactive_config_menu(available_models: List[Dict[str, Any]], current_mapping: Dict[str, str], current_config: Dict[str, Any],
                            pricing_lookup: Optional[Dict[str, Any]] = None) -> Tuple[Optional[Dict[str, str]], Optional[Dict[str, Any]]]:
    """Tabbed configuration menu with Models and Settings pages."""
    if not sys.stdin.isatty() or not _HAS_TTY:
        print(f"  {S.DIM}Interactive menu requires a terminal.{S.RST}")
        return None, None

    _original_theme = current_config.get("theme", "default")

    pricing_lookup = pricing_lookup or {}
    tabs = ["Models", "Settings"]
    active_tab = 0

    model_items = []
    seen_ids = set()
    existing_names = set()

    for short_name, model_id in current_mapping.items():
        model_items.append({
            'short': short_name, 'id': model_id,
            'selected': True,
            'pricing': _format_price(pricing_lookup.get(model_id, {})),
        })
        seen_ids.add(model_id)
        existing_names.add(short_name)

    for m in available_models:
        if len(model_items) >= MODEL_MENU_LIMIT:
            break
        mid = m.get('id', '')
        if mid in seen_ids:
            continue
        seen_ids.add(mid)
        short = _unique_short_name(mid, existing_names)
        existing_names.add(short)
        model_items.append({
            'short': short, 'id': mid,
            'selected': False,
            'pricing': _format_price(pricing_lookup.get(mid, {})),
        })

    from wavebench.tui.styles import THEME_NAMES

    REASONING_CHOICES = ["max", "xhigh", "high", "medium", "low", "off"]
    SORT_CHOICES = ["runs", "avg_time", "rate", "avg_tokens", "cost"]
    AUTO_OPEN_CHOICES = ["off", "incremental", "after_all"]

    settings_items = [
        {
            "key": "reasoning_effort",
            "label": "Reasoning effort",
            "value": current_config.get("reasoning_effort", "high"),
            "type": "cycle",
            "choices": REASONING_CHOICES,
        },
        {
            "key": "analytics_sort",
            "label": "Analytics sort",
            "value": current_config.get("analytics_sort", "runs"),
            "type": "cycle",
            "choices": SORT_CHOICES,
        },
        {
            "key": "theme",
            "label": "Theme",
            "value": current_config.get("theme", "default"),
            "type": "cycle",
            "choices": THEME_NAMES,
        },
        {
            "key": "auto_open",
            "label": "Auto-open files",
            "value": current_config.get("auto_open", "off"),
            "type": "cycle",
            "choices": AUTO_OPEN_CHOICES,
        },
        {
            "key": "auto_install",
            "label": "Auto-install deps",
            "value": current_config.get("auto_install", "off"),
            "type": "cycle",
            "choices": ["off", "on"],
            "parent_key": "auto_open",
            "parent_hidden_when": "off",
        },
    ]

    def _visible_settings() -> list:
        """Return [(original_index, item), ...] for visible settings."""
        values = {it["key"]: it["value"] for it in settings_items}
        visible = []
        for i, item in enumerate(settings_items):
            parent = item.get("parent_key")
            if parent:
                hidden_val = item.get("parent_hidden_when")
                if hidden_val is not None and values.get(parent) == hidden_val:
                    continue
            visible.append((i, item))
        return visible

    model_cursor = 0
    _CHROME_LINES = 8
    model_page_size = max(1, min(14, shutil.get_terminal_size((80, 24)).lines - _CHROME_LINES))
    model_page = 0
    model_search_query = ""
    filtered_model_indices = list(range(len(model_items)))
    settings_cursor = 0
    adding_model = False
    add_model_buffer = ""

    if not model_items:
        print(f"  {S.DIM}No models available.{S.RST}")
        return None, None

    _nat_short_w = max(len(it['short']) for it in model_items) + 2
    _nat_id_w = max(len(it['id']) for it in model_items) + 2
    short_w = _nat_short_w
    id_w = _nat_id_w

    _max_price_w = max((len(it['pricing']) for it in model_items if it['pricing']), default=0)
    _overhead = 7 + (2 + _max_price_w if _max_price_w else 0)

    content_height = max(model_page_size, len(settings_items))

    def _model_page_count() -> int:
        return max(1, (len(filtered_model_indices) + model_page_size - 1) // model_page_size)

    def _sync_model_page_from_cursor() -> None:
        nonlocal model_cursor, model_page
        if not filtered_model_indices:
            model_page = 0
            return
        if model_cursor not in filtered_model_indices:
            model_cursor = filtered_model_indices[0]
        model_page = filtered_model_indices.index(model_cursor) // model_page_size

    def _model_page_bounds() -> Tuple[int, int]:
        start = model_page * model_page_size
        end = min(start + model_page_size, len(filtered_model_indices))
        return start, end

    def _refresh_model_filter(preserve_current: bool = True) -> None:
        nonlocal model_cursor, model_page, filtered_model_indices
        current = model_cursor
        filtered_model_indices = _filter_model_indices(
            model_items, model_search_query)
        if not filtered_model_indices:
            model_page = 0
            return
        if preserve_current and current in filtered_model_indices:
            model_cursor = current
        else:
            model_cursor = filtered_model_indices[0]
        _sync_model_page_from_cursor()

    def render() -> None:
        nonlocal model_page_size, content_height, short_w, id_w, model_page
        term = shutil.get_terminal_size((80, 24))
        new_ps = max(1, min(14, term.lines - _CHROME_LINES))
        if new_ps != model_page_size:
            model_page_size = new_ps
            content_height = max(model_page_size, len(settings_items))
            if filtered_model_indices and model_cursor in filtered_model_indices:
                model_page = filtered_model_indices.index(model_cursor) // model_page_size
        w = max(20, min(120, term.columns) - 4)
        _avail = max(10, (w - 4) - _overhead)
        short_w, id_w = _nat_short_w, _nat_id_w
        if short_w + id_w > _avail:
            ratio = _avail / (short_w + id_w)
            short_w = max(6, int(_nat_short_w * ratio))
            id_w = max(6, _avail - short_w)

        sys.stdout.write("\033[H")
        buf: list[str] = []

        buf.append(_box_top('Configuration', w) + "\033[K\n")
        buf.append(_box_row('', w) + "\033[K\n")

        tab_parts = []
        for i, name in enumerate(tabs):
            if i == active_tab:
                tab_parts.append(f"{S.BOLD}{_styles.ACCENT_HI}[{name}]{S.RST}")
            else:
                tab_parts.append(f"{S.DIM} {name} {S.RST}")
        buf.append(_box_row('   '.join(tab_parts), w) + "\033[K\n")

        if active_tab == 0:
            if adding_model:
                add_label = f"{S.HGRN}add model{S.RST}"
                add_query = add_model_buffer or f"{S.DIM}provider/model-id{S.RST}"
                buf.append(
                    _box_row(f'{add_label}: {add_query}', w) + "\033[K\n")
            else:
                search_label = (f"{_styles.ACCENT_HI}search{S.RST}"
                                if model_search_query else "search")
                query = model_search_query or f"{S.DIM}type to filter{S.RST}"
                buf.append(
                    _box_row(f'{search_label}: {query}', w) + "\033[K\n")
        else:
            buf.append(_box_row('', w) + "\033[K\n")

        for row in range(content_height):
            if active_tab == 0:
                start, end = _model_page_bounds()
                if row < (end - start):
                    item_idx = filtered_model_indices[start + row]
                    item = model_items[item_idx]
                    is_cur = (item_idx == model_cursor)
                    chk = f"{S.HGRN}✓{S.RST}" if item['selected'] else " "
                    sn = _fit(item['short'], short_w)
                    mi = _fit(item['id'], id_w)
                    if is_cur:
                        mk = f"{_styles.ACCENT_HI}▸{S.RST}"
                        ns = f"{S.BOLD}{sn:<{short_w}}{S.RST}"
                        ids = f"{_styles.ACCENT}{mi:<{id_w}}{S.RST}"
                    else:
                        mk = " "
                        ns = f"{sn:<{short_w}}"
                        ids = f"{S.DIM}{mi:<{id_w}}{S.RST}"
                    ps = (f"  {S.DIM}{item['pricing']}{S.RST}"
                          if item['pricing'] else "")
                    buf.append(
                        _box_row(f'{mk} [{chk}] {ns} {ids}{ps}', w)
                        + "\033[K\n")
                elif row == 0 and not filtered_model_indices:
                    buf.append(
                        _box_row(f'{S.DIM}No models match the current search.{S.RST}', w)
                        + "\033[K\n")
                else:
                    buf.append(_box_row('', w) + "\033[K\n")
            else:
                visible = _visible_settings()
                if row < len(visible):
                    _, item = visible[row]
                    is_cur = (row == settings_cursor)
                    if item.get("type") == "cycle":
                        val = item["value"]
                        if item.get("key") == "reasoning_effort":
                            if val == "off":
                                val_s = f"{S.HRED}{val}{S.RST}"
                                chk = " "
                            elif val in ("high", "xhigh", "max"):
                                val_s = f"{S.HGRN}{val}{S.RST}"
                                chk = f"{S.HGRN}✓{S.RST}"
                            else:
                                val_s = f"{S.HYEL}{val}{S.RST}"
                                chk = f"{S.HYEL}~{S.RST}"
                        elif item.get("key") == "auto_open":
                            if val == "off":
                                val_s = f"{S.DIM}{val}{S.RST}"
                                chk = " "
                            elif val == "incremental":
                                val_s = f"{S.HYEL}{val}{S.RST}"
                                chk = f"{S.HYEL}~{S.RST}"
                            else:  # after_all
                                val_s = f"{S.HGRN}{val}{S.RST}"
                                chk = f"{S.HGRN}✓{S.RST}"
                        elif item.get("key") == "auto_install":
                            if val == "off":
                                val_s = f"{S.DIM}{val}{S.RST}"
                                chk = " "
                            else:  # on
                                val_s = f"{S.HGRN}{val}{S.RST}"
                                chk = f"{S.HGRN}✓{S.RST}"
                        elif item.get("key") == "theme":
                            tc = THEMES.get(val, {}).get("accent_hi", _styles.ACCENT_HI)
                            val_s = f"{tc}{val}{S.RST}"
                            chk = f"{tc}✓{S.RST}"
                        else:
                            val_s = f"{_styles.ACCENT_HI}{val}{S.RST}"
                            chk = f"{_styles.ACCENT_HI}✓{S.RST}"
                    else:
                        val_s = (f"{S.HGRN}ON{S.RST}" if item["value"]
                                 else f"{S.HRED}OFF{S.RST}")
                        chk = (f"{S.HGRN}✓{S.RST}" if item["value"]
                               else " ")
                    indent = "  " if item.get("parent_key") else ""
                    if is_cur:
                        mk = f"{_styles.ACCENT_HI}▸{S.RST}"
                        label = f"{S.BOLD}{indent}{item['label']}{S.RST}"
                    else:
                        mk = " "
                        label = f"{indent}{item['label']}"
                    buf.append(
                        _box_row(f'{mk} [{chk}] {label}  {val_s}', w)
                        + "\033[K\n")
                else:
                    buf.append(_box_row('', w) + "\033[K\n")

        buf.append(_box_row('', w) + "\033[K\n")

        if active_tab == 0:
            sel = sum(1 for it in model_items if it['selected'])
            pcount = _model_page_count()
            status = (
                f"{S.BOLD}{sel}{S.RST} of "
                f"{len(model_items)} selected  "
                f"{S.DIM}{len(filtered_model_indices)} shown{S.RST}  "
                f"{S.DIM}page {model_page + 1}/{pcount}{S.RST}")
        else:
            defaults = {"reasoning_effort": "high", "analytics_sort": "runs", "theme": "default", "auto_open": "off", "auto_install": "off"}
            changed = any(
                it["value"] != current_config.get(
                    it["key"], defaults.get(it["key"]))
                for it in settings_items)
            tag = f"  {S.HYEL}(modified){S.RST}" if changed else ""
            vis_count = len(_visible_settings())
            status = (f"{S.DIM}{vis_count} "
                      f"setting(s){S.RST}{tag}")
        buf.append(_box_row(status, w) + "\033[K\n")

        hl_parts = ["←→ tab", "↑↓", "Space", "Enter/Tab"]
        if active_tab == 0:
            hl_parts.extend(["^A all", "^N none", "[ ] page", "+ add"])
        hl_parts.append("Esc")
        hl = f"{S.DIM}{' · '.join(hl_parts)}{S.RST}"
        buf.append(_box_row(hl, w) + "\033[K\n")

        buf.append(_box_bot(w) + "\033[K")
        buf.append("\033[J")

        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    if _HAS_SIGWINCH:
        _wr, _ww = os.pipe()
        os.set_blocking(_wr, False)
        os.set_blocking(_ww, False)
        def _on_winch(sig, frame):
            try:
                os.write(_ww, b'\x00')
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
            if key == 'resize':
                render()
                continue
            if adding_model:
                if key in ('escape', 'ctrl-c'):
                    adding_model = False
                    add_model_buffer = ""
                    sys.stdout.write("\033[?25l")
                elif key == 'enter':
                    mid = add_model_buffer.strip()
                    if mid and mid not in seen_ids:
                        short = _unique_short_name(mid, existing_names)
                        existing_names.add(short)
                        seen_ids.add(mid)
                        model_items.append({
                            'short': short, 'id': mid,
                            'selected': True, 'pricing': '',
                        })
                        _nat_short_w = max(
                            _nat_short_w, len(short) + 2)
                        _nat_id_w = max(
                            _nat_id_w, len(mid) + 2)
                        _refresh_model_filter(preserve_current=False)
                        model_cursor = len(model_items) - 1
                        _sync_model_page_from_cursor()
                    adding_model = False
                    add_model_buffer = ""
                    sys.stdout.write("\033[?25l")
                elif key == 'backspace':
                    add_model_buffer = add_model_buffer[:-1]
                elif (_is_printable_search_char(key)
                      or key in ('+', '/', '[', ']')):
                    add_model_buffer += key
                render()
                continue
            if key in ('escape', 'ctrl-c'):
                _styles.apply_theme(_original_theme)
                sys.stdout.write("\033[?25h\033[?1049l")
                print()
                return None, None
            elif key == 'left':
                active_tab = (active_tab - 1) % len(tabs)
            elif key == 'right':
                active_tab = (active_tab + 1) % len(tabs)
            elif key == 'up':
                if active_tab == 0 and filtered_model_indices:
                    idx = filtered_model_indices.index(model_cursor)
                    model_cursor = filtered_model_indices[
                        (idx - 1) % len(filtered_model_indices)]
                    _sync_model_page_from_cursor()
                elif active_tab != 0:
                    visible = _visible_settings()
                    if visible:
                        settings_cursor = ((settings_cursor - 1)
                                           % len(visible))
            elif key == 'down':
                if active_tab == 0 and filtered_model_indices:
                    idx = filtered_model_indices.index(model_cursor)
                    model_cursor = filtered_model_indices[
                        (idx + 1) % len(filtered_model_indices)]
                    _sync_model_page_from_cursor()
                elif active_tab != 0:
                    visible = _visible_settings()
                    if visible:
                        settings_cursor = ((settings_cursor + 1)
                                           % len(visible))
            elif key == 'space':
                if active_tab == 0 and filtered_model_indices:
                    model_items[model_cursor]['selected'] = \
                        not model_items[model_cursor]['selected']
                elif active_tab != 0:
                    visible = _visible_settings()
                    if visible and settings_cursor < len(visible):
                        _, item = visible[settings_cursor]
                        if item.get("type") == "cycle":
                            choices = item["choices"]
                            idx = choices.index(item["value"]) if item["value"] in choices else 0
                            item["value"] = choices[(idx + 1) % len(choices)]
                            if item.get("key") == "theme":
                                _styles.apply_theme(item["value"])
                        else:
                            item['value'] = not item['value']
                        # Clamp cursor if visibility changed
                        new_visible = _visible_settings()
                        if settings_cursor >= len(new_visible):
                            settings_cursor = max(0, len(new_visible) - 1)
            elif key in ('enter', 'tab'):
                break
            elif key == 'ctrl-a' and active_tab == 0:
                for it in model_items:
                    it['selected'] = True
            elif key == 'ctrl-n' and active_tab == 0:
                for it in model_items:
                    it['selected'] = False
            elif key in ('[', ']') and active_tab == 0 and filtered_model_indices:
                pcount = _model_page_count()
                if key == '[':
                    model_page = (model_page - 1) % pcount
                else:
                    model_page = (model_page + 1) % pcount
                start, end = _model_page_bounds()
                model_cursor = filtered_model_indices[start if start < end else 0]
            elif key == '+' and active_tab == 0:
                adding_model = True
                add_model_buffer = ""
                sys.stdout.write("\033[?25h")
            elif active_tab == 0 and key == 'backspace':
                if model_search_query:
                    model_search_query = model_search_query[:-1]
                    _refresh_model_filter(preserve_current=False)
            elif active_tab == 0 and _is_printable_search_char(key):
                model_search_query += key
                _refresh_model_filter(preserve_current=False)
            render()
    finally:
        sys.stdout.write("\033[?25h\033[?1049l")
        if _HAS_SIGWINCH:
            signal.signal(signal.SIGWINCH, _old_sigwinch)
            os.close(_wr)
            os.close(_ww)

    selected = {it['short']: it['id'] for it in model_items if it['selected']}
    if not selected:
        print(f"\n  {S.HYEL}No models selected — keeping current config."
              f"{S.RST}")
        return None, None

    new_config = dict(current_config)
    for item in settings_items:
        new_config[item["key"]] = item["value"]

    print()
    return selected, new_config

def run_config_menu(api_key: str, current_mapping: Optional[Dict[str, str]] = None, current_config: Optional[Dict[str, Any]] = None,
                    prefetched: Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]] = None) -> Tuple[Optional[Dict[str, str]], Optional[Dict[str, Any]]]:
    """Fetch models from OpenRouter and open the tabbed config menu.

    If *prefetched* is supplied as ``(available, pricing_lookup)`` the
    network call is skipped entirely.
    """
    from wavebench.storage import load_config
    if prefetched is not None:
        available, pricing_lookup = prefetched
    else:
        print(f"  {_work} {S.DIM}Fetching models from OpenRouter…{S.RST}")
        available, pricing_lookup = fetch_top_models(api_key, count=100)
    if not available:
        print(f"  {S.DIM}Could not fetch remote models — "
              f"showing local config only.{S.RST}")
    print()
    if current_mapping is None:
        current_mapping = MODEL_MAPPING
    if current_config is None:
        current_config = load_config()
    return interactive_config_menu(available, current_mapping, current_config,
                                   pricing_lookup=pricing_lookup)
