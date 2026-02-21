import sys
import re
import shutil
from typing import Dict, Any, Optional, List, Tuple

try:
    import tty
    import termios
    _HAS_TTY = True
except ImportError:
    _HAS_TTY = False

from llm_benchmarks.tui.styles import S, _dot, _rule, _work
from llm_benchmarks.api import fetch_top_models
from llm_benchmarks.models import MODEL_MAPPING

MODEL_MENU_LIMIT = 40

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
        return ch
    finally:
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
    page_size = max(6, min(14, shutil.get_terminal_size((80, 24)).lines - 12))
    page_index = 0
    short_w = max(len(it['short']) for it in items) + 2
    id_w = max(len(it['id']) for it in items) + 2
    menu_lines = page_size + 5

    def _page_count() -> int:
        return max(1, (len(items) + page_size - 1) // page_size)

    def _sync_page_from_cursor() -> None:
        nonlocal page_index
        page_index = cursor_pos // page_size

    def _page_bounds() -> Tuple[int, int]:
        start = page_index * page_size
        end = min(start + page_size, len(items))
        return start, end

    def render(first: bool = False) -> None:
        if not first:
            sys.stdout.write(f"\033[{menu_lines}A")

        pcount = _page_count()
        sys.stdout.write(
            f"\033[K  {S.DIM}↑↓{S.RST} navigate  {_dot}  "
            f"{S.DIM}Space{S.RST} toggle  {_dot}  "
            f"{S.DIM}Enter{S.RST} confirm  {_dot}  "
            f"{S.DIM}a{S.RST} all  {_dot}  "
            f"{S.DIM}n{S.RST} none  {_dot}  "
            f"{S.DIM}[ ]{S.RST} page  {_dot}  "
            f"{S.DIM}q{S.RST} cancel\n")
        sys.stdout.write(
            f"\033[K  {S.DIM}page {page_index + 1}/{pcount}{S.RST}\n")

        start, end = _page_bounds()
        visible = items[start:end]
        for off, item in enumerate(visible):
            i = start + off
            is_cur = i == cursor_pos
            chk = f"{S.HGRN}✓{S.RST}" if item['selected'] else " "

            if is_cur:
                mk = f"{S.HCYN}▸{S.RST}"
                ns = f"{S.BOLD}{item['short']:<{short_w}}{S.RST}"
                ids = f"{S.CYN}{item['id']:<{id_w}}{S.RST}"
            else:
                mk = " "
                ns = f"{item['short']:<{short_w}}"
                ids = f"{S.DIM}{item['id']:<{id_w}}{S.RST}"

            ps = (f"  {S.DIM}{item['pricing']}{S.RST}"
                  if item['pricing'] else "")
            sys.stdout.write(
                f"\033[K  {mk} [{chk}] {ns} {ids}{ps}\n")
        for _ in range(page_size - len(visible)):
            sys.stdout.write("\033[K\n")

        sys.stdout.write("\033[K\n")
        sel = sum(1 for it in items if it['selected'])
        sys.stdout.write(
            f"\033[K  {S.BOLD}{sel}{S.RST} of {len(items)} selected\n")
        sys.stdout.flush()

    sys.stdout.write("\033[?25l")
    try:
        render(first=True)
        while True:
            key = _read_key()
            if key in ('q', 'escape', 'ctrl-c'):
                sys.stdout.write("\033[?25h")
                print()
                return None
            elif key == 'up':
                cursor_pos = (cursor_pos - 1) % len(items)
                _sync_page_from_cursor()
            elif key == 'down':
                cursor_pos = (cursor_pos + 1) % len(items)
                _sync_page_from_cursor()
            elif key == 'space':
                items[cursor_pos]['selected'] = \
                    not items[cursor_pos]['selected']
            elif key in ('[', ']'):
                pcount = _page_count()
                if key == '[':
                    page_index = (page_index - 1) % pcount
                else:
                    page_index = (page_index + 1) % pcount
                start, end = _page_bounds()
                cursor_pos = min(max(cursor_pos, start), end - 1)
            elif key == 'enter':
                break
            elif key == 'a':
                for it in items:
                    it['selected'] = True
            elif key == 'n':
                for it in items:
                    it['selected'] = False
            render()
    finally:
        sys.stdout.write("\033[?25h")

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
    available, pricing_lookup = fetch_top_models(api_key, count=60)
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

    settings_items = [
        {
            "key": "auto_use_venv",
            "label": "Auto-activate virtual environment",
            "value": current_config.get("auto_use_venv", True),
        },
    ]

    model_cursor = 0
    model_page_size = max(6, min(14, shutil.get_terminal_size((80, 24)).lines - 14))
    model_page = 0
    settings_cursor = 0

    if not model_items:
        print(f"  {S.DIM}No models available.{S.RST}")
        return None, None

    short_w = max(len(it['short']) for it in model_items) + 2
    id_w = max(len(it['id']) for it in model_items) + 2
    from llm_benchmarks.tui.styles import _tw
    content_height = max(model_page_size, len(settings_items))
    total_lines = content_height + 6

    def _model_page_count() -> int:
        return max(1, (len(model_items) + model_page_size - 1) // model_page_size)

    def _sync_model_page_from_cursor() -> None:
        nonlocal model_page
        model_page = model_cursor // model_page_size

    def _model_page_bounds() -> Tuple[int, int]:
        start = model_page * model_page_size
        end = min(start + model_page_size, len(model_items))
        return start, end

    def render(first: bool = False) -> None:
        if not first:
            sys.stdout.write(f"\033[{total_lines}A")

        tab_parts = []
        for i, name in enumerate(tabs):
            if i == active_tab:
                tab_parts.append(f"{S.BOLD}{S.HCYN}[{name}]{S.RST}")
            else:
                tab_parts.append(f"{S.DIM} {name} {S.RST}")
        sys.stdout.write(f"\033[K  {'   '.join(tab_parts)}\n")

        sys.stdout.write(
            f"\033[K  {S.DIM}{'─' * min(50, _tw() - 4)}{S.RST}\n")

        hl = (f"{S.DIM}←→{S.RST} switch tab  {_dot}  "
              f"{S.DIM}↑↓{S.RST} navigate  {_dot}  "
              f"{S.DIM}Space{S.RST} toggle  {_dot}  "
              f"{S.DIM}Enter{S.RST} confirm")
        if active_tab == 0:
            hl += (f"  {_dot}  {S.DIM}a{S.RST} all"
                   f"  {_dot}  {S.DIM}n{S.RST} none"
                   f"  {_dot}  {S.DIM}[ ]{S.RST} page")
        hl += f"  {_dot}  {S.DIM}Tab{S.RST}/{S.DIM}q{S.RST} cancel"
        sys.stdout.write(f"\033[K  {hl}\n")

        sys.stdout.write("\033[K\n")

        for row in range(content_height):
            if active_tab == 0:
                start, end = _model_page_bounds()
                if row < (end - start):
                    item = model_items[start + row]
                    is_cur = (start + row == model_cursor)
                    chk = f"{S.HGRN}✓{S.RST}" if item['selected'] else " "
                    if is_cur:
                        mk = f"{S.HCYN}▸{S.RST}"
                        ns = f"{S.BOLD}{item['short']:<{short_w}}{S.RST}"
                        ids = f"{S.CYN}{item['id']:<{id_w}}{S.RST}"
                    else:
                        mk = " "
                        ns = f"{item['short']:<{short_w}}"
                        ids = f"{S.DIM}{item['id']:<{id_w}}{S.RST}"
                    ps = (f"  {S.DIM}{item['pricing']}{S.RST}"
                          if item['pricing'] else "")
                    sys.stdout.write(
                        f"\033[K  {mk} [{chk}] {ns} {ids}{ps}\n")
                else:
                    sys.stdout.write("\033[K\n")
            else:
                if row < len(settings_items):
                    item = settings_items[row]
                    is_cur = (row == settings_cursor)
                    val_s = (f"{S.HGRN}ON{S.RST}" if item["value"]
                             else f"{S.HRED}OFF{S.RST}")
                    chk = (f"{S.HGRN}✓{S.RST}" if item["value"]
                           else " ")
                    if is_cur:
                        mk = f"{S.HCYN}▸{S.RST}"
                        label = f"{S.BOLD}{item['label']}{S.RST}"
                    else:
                        mk = " "
                        label = item['label']
                    sys.stdout.write(
                        f"\033[K  {mk} [{chk}] {label}  {val_s}\n")
                else:
                    sys.stdout.write("\033[K\n")

        sys.stdout.write("\033[K\n")

        if active_tab == 0:
            sel = sum(1 for it in model_items if it['selected'])
            pcount = _model_page_count()
            sys.stdout.write(
                f"\033[K  {S.BOLD}{sel}{S.RST} of "
                f"{len(model_items)} selected  "
                f"{S.DIM}page {model_page + 1}/{pcount}{S.RST}\n")
        else:
            changed = any(
                it["value"] != current_config.get(it["key"], True)
                for it in settings_items)
            tag = f"  {S.HYEL}(modified){S.RST}" if changed else ""
            sys.stdout.write(
                f"\033[K  {S.DIM}{len(settings_items)} "
                f"setting(s){S.RST}{tag}\n")
        sys.stdout.flush()

    sys.stdout.write("\033[?25l")
    try:
        render(first=True)
        while True:
            key = _read_key()
            if key in ('q', 'escape', 'ctrl-c', 'tab'):
                sys.stdout.write("\033[?25h")
                print()
                return None, None
            elif key == 'left':
                active_tab = (active_tab - 1) % len(tabs)
            elif key == 'right':
                active_tab = (active_tab + 1) % len(tabs)
            elif key == 'up':
                if active_tab == 0:
                    model_cursor = (model_cursor - 1) % len(model_items)
                    _sync_model_page_from_cursor()
                elif settings_items:
                    settings_cursor = ((settings_cursor - 1)
                                       % len(settings_items))
            elif key == 'down':
                if active_tab == 0:
                    model_cursor = (model_cursor + 1) % len(model_items)
                    _sync_model_page_from_cursor()
                elif settings_items:
                    settings_cursor = ((settings_cursor + 1)
                                       % len(settings_items))
            elif key == 'space':
                if active_tab == 0:
                    model_items[model_cursor]['selected'] = \
                        not model_items[model_cursor]['selected']
                elif settings_items:
                    settings_items[settings_cursor]['value'] = \
                        not settings_items[settings_cursor]['value']
            elif key == 'enter':
                break
            elif key == 'a' and active_tab == 0:
                for it in model_items:
                    it['selected'] = True
            elif key == 'n' and active_tab == 0:
                for it in model_items:
                    it['selected'] = False
            elif key in ('[', ']') and active_tab == 0:
                pcount = _model_page_count()
                if key == '[':
                    model_page = (model_page - 1) % pcount
                else:
                    model_page = (model_page + 1) % pcount
                start, end = _model_page_bounds()
                model_cursor = min(max(model_cursor, start), end - 1)
            render()
    finally:
        sys.stdout.write("\033[?25h")

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

def run_config_menu(api_key: str, current_mapping: Optional[Dict[str, str]] = None, current_config: Optional[Dict[str, Any]] = None) -> Tuple[Optional[Dict[str, str]], Optional[Dict[str, Any]]]:
    """Fetch models from OpenRouter and open the tabbed config menu."""
    from llm_benchmarks.storage import load_config
    print(f"  {_work} {S.DIM}Fetching models from OpenRouter…{S.RST}")
    available, pricing_lookup = fetch_top_models(api_key, count=60)
    if not available:
        print(f"  {S.DIM}Could not fetch remote models — "
              f"showing local config only.{S.RST}")
    print()
    _rule("Configuration")
    print()
    if current_mapping is None:
        current_mapping = MODEL_MAPPING
    if current_config is None:
        current_config = load_config()
    return interactive_config_menu(available, current_mapping, current_config,
                                   pricing_lookup=pricing_lookup)
