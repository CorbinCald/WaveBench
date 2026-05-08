"""Tabbed configuration menu — Models/TTS/Image tabs (catalog browser + manual add)
and Settings tab (theme, reasoning-effort, analytics sort, directory naming,
auto-open, auto-install).

``interactive_config_menu`` is a single function that drives the tabs through
a shared event loop; further decomposition is deferred per the maintainability
spec. ``run_config_menu`` is the thin wrapper that fetches the OpenRouter
catalog (or accepts a prefetched pair) before entering the menu.

Theme changes are applied live during cycling and reverted on cancel so
the user sees each theme before committing.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
from typing import Any

from wavebench.api import fetch_top_models
from wavebench.models import (
    IMAGE_MODEL_MAPPING,
    MODEL_MAPPING,
    TTS_MODEL_MAPPING,
    is_image_model,
    is_tts_model,
)
from wavebench.parsers import _DIRECTORY_NAMING_CHOICES
from wavebench.tui import styles as _styles
from wavebench.tui.input import _read_key_or_resize
from wavebench.tui.menus._shared import (
    MODEL_MENU_LIMIT,
    _fit,
    _format_price,
    _is_printable_search_char,
    _unique_short_name,
)
from wavebench.tui.styles import (
    THEMES,
    S,
    _box_bot,
    _box_row,
    _box_top,
    _work,
)

try:
    import termios  # noqa: F401
    import tty  # noqa: F401

    _HAS_TTY = True
except ImportError:
    _HAS_TTY = False

_HAS_SIGWINCH = hasattr(signal, "SIGWINCH")


def _model_category(model_id: str, metadata: dict[str, Any] | None = None) -> str:
    """Return the config-tab category for a model id."""
    if is_image_model(model_id, metadata):
        return "image"
    if is_tts_model(model_id):
        return "tts"
    return "model"


def _build_config_model_items(
    available_models: list[dict[str, Any]],
    current_mapping: dict[str, str],
    pricing_lookup: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build config-menu model rows with text, TTS, and Image defaults separated."""
    pricing_lookup = pricing_lookup or {}
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    existing_names: set[str] = set()
    category_counts = {"model": 0, "tts": 0, "image": 0}

    selected_pairs = list(current_mapping.items())
    selected_ids = {model_id for _, model_id in selected_pairs}
    if not any(
        _model_category(model_id, pricing_lookup.get(model_id, {})) == "model"
        for _, model_id in selected_pairs
    ):
        for short_name, model_id in MODEL_MAPPING.items():
            if model_id not in selected_ids:
                selected_pairs.append((short_name, model_id))
                selected_ids.add(model_id)
    if not any(
        _model_category(model_id, pricing_lookup.get(model_id, {})) == "tts"
        for _, model_id in selected_pairs
    ):
        for short_name, model_id in TTS_MODEL_MAPPING.items():
            if model_id not in selected_ids:
                selected_pairs.append((short_name, model_id))
                selected_ids.add(model_id)
    if not any(
        _model_category(model_id, pricing_lookup.get(model_id, {})) == "image"
        for _, model_id in selected_pairs
    ):
        for short_name, model_id in IMAGE_MODEL_MAPPING.items():
            if model_id not in selected_ids:
                selected_pairs.append((short_name, model_id))
                selected_ids.add(model_id)

    def add_item(
        short_name: str,
        model_id: str,
        *,
        selected: bool,
        metadata: dict[str, Any] | None = None,
        category_override: str | None = None,
    ) -> int | None:
        if not model_id or model_id in seen_ids:
            return None
        if selected and short_name and short_name not in existing_names:
            short = short_name
        else:
            short = _unique_short_name(model_id, existing_names)
        seen_ids.add(model_id)
        existing_names.add(short)
        metadata = metadata or pricing_lookup.get(model_id, {})
        category = category_override or _model_category(model_id, metadata)
        category_counts[category] += 1
        items.append(
            {
                "short": short,
                "id": model_id,
                "selected": selected,
                "pricing": _format_price(pricing_lookup.get(model_id, {})),
                "category": category,
                "is_tts": category == "tts",
                "is_image": category == "image",
            }
        )
        return len(items) - 1

    for short_name, model_id in selected_pairs:
        add_item(short_name, model_id, selected=True)

    for m in available_models:
        mid = m.get("id", "")
        category = _model_category(mid, m)
        if category_counts[category] >= MODEL_MENU_LIMIT or mid in seen_ids:
            continue
        add_item("", mid, selected=False, metadata=m, category_override=category)

    return items


def _filter_config_model_indices(
    items: list[dict[str, Any]], query: str, *, tts: bool = False, image: bool = False
) -> list[int]:
    """Return model row indices for one config-menu model tab."""
    category = "image" if image else ("tts" if tts else "model")
    tab_indices = [i for i, item in enumerate(items) if item.get("category") == category]
    needle = query.strip().lower()
    if not needle:
        return tab_indices
    return [
        i
        for i in tab_indices
        if needle in items[i]["short"].lower() or needle in items[i]["id"].lower()
    ]


def interactive_config_menu(
    available_models: list[dict[str, Any]],
    current_mapping: dict[str, str],
    current_config: dict[str, Any],
    pricing_lookup: dict[str, Any] | None = None,
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """Tabbed configuration menu with separate Models, TTS, and Settings pages."""
    if not sys.stdin.isatty() or not _HAS_TTY:
        print(f"  {S.DIM}Interactive menu requires a terminal.{S.RST}")
        return None, None

    _original_theme = current_config.get("theme", "default")

    pricing_lookup = pricing_lookup or {}
    MODEL_TAB = 0
    TTS_TAB = 1
    IMAGE_TAB = 2
    SETTINGS_TAB = 3
    MODEL_TABS = (MODEL_TAB, TTS_TAB, IMAGE_TAB)
    tabs = ["Models", "TTS", "Image", "Settings"]
    active_tab = MODEL_TAB

    model_items = _build_config_model_items(available_models, current_mapping, pricing_lookup)
    seen_ids = {item["id"] for item in model_items}
    existing_names = {item["short"] for item in model_items}

    from wavebench.tui.styles import THEME_NAMES

    REASONING_CHOICES = ["max", "xhigh", "high", "medium", "low", "off"]
    SORT_CHOICES = ["runs", "avg_time", "rate", "avg_tokens", "cost"]
    AUTO_OPEN_CHOICES = ["off", "incremental", "after_all"]
    TTS_VOICE_CHOICES = [
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "fable",
        "nova",
        "onyx",
        "sage",
        "shimmer",
        "verse",
        "Kore",
        "Puck",
        "en_paul_neutral",
        "american_female",
        "conversational_a",
        "tara",
        "af_alloy",
    ]
    TTS_SPEED_CHOICES = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
    IMAGE_ASPECT_RATIO_CHOICES = [
        "1:1",
        "2:3",
        "3:2",
        "3:4",
        "4:3",
        "4:5",
        "5:4",
        "9:16",
        "16:9",
        "21:9",
    ]
    IMAGE_SIZE_CHOICES = ["1K", "2K", "4K"]
    image_settings_value = current_config.get("image_settings", "provider defaults")
    if image_settings_value == "custom":
        image_aspect_ratio_value = current_config.get("image_aspect_ratio", "1:1")
        image_size_value = current_config.get("image_size", "1K")
    else:
        image_aspect_ratio_value = "1:1"
        image_size_value = "1K"

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
            "key": "directory_naming",
            "label": "Directory naming",
            "value": current_config.get("directory_naming", "llm"),
            "type": "cycle",
            "choices": list(_DIRECTORY_NAMING_CHOICES),
        },
        {
            "key": "auto_open",
            "label": "Auto-open files",
            "value": current_config.get("auto_open", "off"),
            "type": "cycle",
            "choices": AUTO_OPEN_CHOICES,
        },
        {
            "key": "tts_voice",
            "label": "TTS voice",
            "value": current_config.get("tts_voice", "alloy"),
            "type": "cycle",
            "choices": TTS_VOICE_CHOICES,
        },
        {
            "key": "tts_format",
            "label": "TTS format",
            "value": current_config.get("tts_format", "mp3"),
            "type": "cycle",
            "choices": ["mp3", "pcm"],
        },
        {
            "key": "tts_speed",
            "label": "TTS speed",
            "value": current_config.get("tts_speed", 1.0),
            "type": "cycle",
            "choices": TTS_SPEED_CHOICES,
        },
        {
            "key": "image_settings",
            "label": "Image settings",
            "value": image_settings_value,
            "type": "cycle",
            "choices": ["provider defaults", "custom"],
        },
        {
            "key": "image_aspect_ratio",
            "label": "Image aspect ratio",
            "value": image_aspect_ratio_value,
            "type": "cycle",
            "choices": IMAGE_ASPECT_RATIO_CHOICES,
            "parent_key": "image_settings",
            "parent_hidden_when": "provider defaults",
        },
        {
            "key": "image_size",
            "label": "Image size",
            "value": image_size_value,
            "type": "cycle",
            "choices": IMAGE_SIZE_CHOICES,
            "parent_key": "image_settings",
            "parent_hidden_when": "provider defaults",
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

    def _model_tab_category(tab: int) -> str:
        if tab == TTS_TAB:
            return "tts"
        if tab == IMAGE_TAB:
            return "image"
        return "model"

    model_cursor = {
        tab: next(
            (
                i
                for i, item in enumerate(model_items)
                if item.get("category") == _model_tab_category(tab)
            ),
            0,
        )
        for tab in MODEL_TABS
    }
    _CHROME_LINES = 8
    model_page_size = max(1, min(14, shutil.get_terminal_size((80, 24)).lines - _CHROME_LINES))
    model_page = {MODEL_TAB: 0, TTS_TAB: 0, IMAGE_TAB: 0}
    model_search_query = {MODEL_TAB: "", TTS_TAB: "", IMAGE_TAB: ""}
    filtered_model_indices = {
        MODEL_TAB: _filter_config_model_indices(model_items, "", tts=False),
        TTS_TAB: _filter_config_model_indices(model_items, "", tts=True),
        IMAGE_TAB: _filter_config_model_indices(model_items, "", image=True),
    }
    settings_cursor = 0
    adding_model = False
    adding_model_tab = MODEL_TAB
    add_model_buffer = ""

    if not model_items:
        print(f"  {S.DIM}No models available.{S.RST}")
        return None, None

    _nat_short_w = max(len(it["short"]) for it in model_items) + 2
    _nat_id_w = max(len(it["id"]) for it in model_items) + 2
    short_w = _nat_short_w
    id_w = _nat_id_w

    _max_price_w = max((len(it["pricing"]) for it in model_items if it["pricing"]), default=0)
    _overhead = 7 + (2 + _max_price_w if _max_price_w else 0)

    content_height = max(model_page_size, len(settings_items))

    def _is_model_tab(tab: int | None = None) -> bool:
        return (active_tab if tab is None else tab) in MODEL_TABS

    def _model_tab_is_tts(tab: int) -> bool:
        return tab == TTS_TAB

    def _model_tab_is_image(tab: int) -> bool:
        return tab == IMAGE_TAB

    def _model_page_count(tab: int | None = None) -> int:
        tab = active_tab if tab is None else tab
        indices = filtered_model_indices[tab]
        return max(1, (len(indices) + model_page_size - 1) // model_page_size)

    def _sync_model_page_from_cursor(tab: int | None = None) -> None:
        tab = active_tab if tab is None else tab
        indices = filtered_model_indices[tab]
        if not indices:
            model_page[tab] = 0
            return
        if model_cursor[tab] not in indices:
            model_cursor[tab] = indices[0]
        model_page[tab] = indices.index(model_cursor[tab]) // model_page_size

    def _model_page_bounds(tab: int | None = None) -> tuple[int, int]:
        tab = active_tab if tab is None else tab
        indices = filtered_model_indices[tab]
        start = model_page[tab] * model_page_size
        end = min(start + model_page_size, len(indices))
        return start, end

    def _refresh_model_filter(tab: int | None = None, preserve_current: bool = True) -> None:
        tab = active_tab if tab is None else tab
        current = model_cursor[tab]
        filtered_model_indices[tab] = _filter_config_model_indices(
            model_items,
            model_search_query[tab],
            tts=_model_tab_is_tts(tab),
            image=_model_tab_is_image(tab),
        )
        indices = filtered_model_indices[tab]
        if not indices:
            model_page[tab] = 0
            return
        if preserve_current and current in indices:
            model_cursor[tab] = current
        else:
            model_cursor[tab] = indices[0]
        _sync_model_page_from_cursor(tab)

    def render() -> None:
        nonlocal model_page_size, content_height, short_w, id_w, model_page
        term = shutil.get_terminal_size((80, 24))
        new_ps = max(1, min(14, term.lines - _CHROME_LINES))
        if new_ps != model_page_size:
            model_page_size = new_ps
            content_height = max(model_page_size, len(settings_items))
            for tab in MODEL_TABS:
                indices = filtered_model_indices[tab]
                if indices and model_cursor[tab] in indices:
                    model_page[tab] = indices.index(model_cursor[tab]) // model_page_size
        w = max(20, min(120, term.columns) - 4)
        _avail = max(10, (w - 4) - _overhead)
        short_w, id_w = _nat_short_w, _nat_id_w
        if short_w + id_w > _avail:
            ratio = _avail / (short_w + id_w)
            short_w = max(6, int(_nat_short_w * ratio))
            id_w = max(6, _avail - short_w)

        sys.stdout.write("\033[H")
        buf: list[str] = []

        buf.append(_box_top("Configuration", w) + "\033[K\n")
        buf.append(_box_row("", w) + "\033[K\n")

        tab_parts = []
        for i, name in enumerate(tabs):
            if i == active_tab:
                tab_parts.append(f"{S.BOLD}{_styles.ACCENT_HI}[{name}]{S.RST}")
            else:
                tab_parts.append(f"{S.DIM} {name} {S.RST}")
        buf.append(_box_row("   ".join(tab_parts), w) + "\033[K\n")

        if _is_model_tab():
            if adding_model:
                if adding_model_tab == TTS_TAB:
                    label = "add TTS model"
                elif adding_model_tab == IMAGE_TAB:
                    label = "add image model"
                else:
                    label = "add model"
                add_label = f"{S.HGRN}{label}{S.RST}"
                add_query = add_model_buffer or f"{S.DIM}provider/model-id{S.RST}"
                buf.append(_box_row(f"{add_label}: {add_query}", w) + "\033[K\n")
            else:
                query = model_search_query[active_tab]
                search_label = f"{_styles.ACCENT_HI}search{S.RST}" if query else "search"
                query_display = query or f"{S.DIM}type to filter{S.RST}"
                buf.append(_box_row(f"{search_label}: {query_display}", w) + "\033[K\n")
        else:
            buf.append(_box_row("", w) + "\033[K\n")

        for row in range(content_height):
            if _is_model_tab():
                indices = filtered_model_indices[active_tab]
                start, end = _model_page_bounds()
                if row < (end - start):
                    item_idx = indices[start + row]
                    item = model_items[item_idx]
                    is_cur = item_idx == model_cursor[active_tab]
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
                    buf.append(_box_row(f"{mk} [{chk}] {ns} {ids}{ps}", w) + "\033[K\n")
                elif row == 0 and not indices:
                    message = "No TTS models match the current search."
                    if active_tab == IMAGE_TAB:
                        message = "No image models match the current search."
                    elif active_tab == MODEL_TAB:
                        message = "No models match the current search."
                    buf.append(_box_row(f"{S.DIM}{message}{S.RST}", w) + "\033[K\n")
                else:
                    buf.append(_box_row("", w) + "\033[K\n")
            else:
                visible = _visible_settings()
                if row < len(visible):
                    _, item = visible[row]
                    is_cur = row == settings_cursor
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
                        val_s = f"{S.HGRN}ON{S.RST}" if item["value"] else f"{S.HRED}OFF{S.RST}"
                        chk = f"{S.HGRN}✓{S.RST}" if item["value"] else " "
                    indent = "  " if item.get("parent_key") else ""
                    if is_cur:
                        mk = f"{_styles.ACCENT_HI}▸{S.RST}"
                        label = f"{S.BOLD}{indent}{item['label']}{S.RST}"
                    else:
                        mk = " "
                        label = f"{indent}{item['label']}"
                    buf.append(_box_row(f"{mk} [{chk}] {label}  {val_s}", w) + "\033[K\n")
                else:
                    buf.append(_box_row("", w) + "\033[K\n")

        buf.append(_box_row("", w) + "\033[K\n")

        if _is_model_tab():
            category = _model_tab_category(active_tab)
            tab_total = sum(1 for it in model_items if it.get("category") == category)
            sel = sum(
                1
                for it in model_items
                if it["selected"] and it.get("category") == category
            )
            pcount = _model_page_count()
            status = (
                f"{S.BOLD}{sel}{S.RST} of "
                f"{tab_total} selected  "
                f"{S.DIM}{len(filtered_model_indices[active_tab])} shown{S.RST}  "
                f"{S.DIM}page {model_page[active_tab] + 1}/{pcount}{S.RST}"
            )
        else:
            defaults = {
                "reasoning_effort": "high",
                "analytics_sort": "runs",
                "theme": "default",
                "auto_open": "off",
                "auto_install": "off",
                "directory_naming": "llm",
                "tts_voice": "alloy",
                "tts_format": "mp3",
                "tts_speed": 1.0,
                "image_settings": "provider defaults",
                "image_aspect_ratio": "1:1",
                "image_size": "1K",
                "image_model_ids": [],
            }
            changed = any(
                it["value"] != current_config.get(it["key"], defaults.get(it["key"]))
                for it in settings_items
            )
            tag = f"  {S.HYEL}(modified){S.RST}" if changed else ""
            vis_count = len(_visible_settings())
            status = f"{S.DIM}{vis_count} setting(s){S.RST}{tag}"
        buf.append(_box_row(status, w) + "\033[K\n")

        hl_parts = ["←→ tab", "↑↓", "Space", "Enter/Tab"]
        if _is_model_tab():
            hl_parts.extend(["^A tab all", "^N tab none", "[ ] page", "+ add"])
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
            if adding_model:
                if key in ("escape", "ctrl-c"):
                    adding_model = False
                    add_model_buffer = ""
                    sys.stdout.write("\033[?25l")
                elif key == "enter":
                    mid = add_model_buffer.strip()
                    if mid and mid not in seen_ids:
                        short = _unique_short_name(mid, existing_names)
                        category = _model_tab_category(adding_model_tab)
                        existing_names.add(short)
                        seen_ids.add(mid)
                        model_items.append(
                            {
                                "short": short,
                                "id": mid,
                                "selected": True,
                                "pricing": "",
                                "category": category,
                                "is_tts": category == "tts",
                                "is_image": category == "image",
                            }
                        )
                        _nat_short_w = max(_nat_short_w, len(short) + 2)
                        _nat_id_w = max(_nat_id_w, len(mid) + 2)
                        added_tab = adding_model_tab
                        _refresh_model_filter(MODEL_TAB, preserve_current=True)
                        _refresh_model_filter(TTS_TAB, preserve_current=True)
                        _refresh_model_filter(IMAGE_TAB, preserve_current=True)
                        model_cursor[added_tab] = len(model_items) - 1
                        _sync_model_page_from_cursor(added_tab)
                        active_tab = added_tab
                    adding_model = False
                    add_model_buffer = ""
                    sys.stdout.write("\033[?25l")
                elif key == "backspace":
                    add_model_buffer = add_model_buffer[:-1]
                elif _is_printable_search_char(key) or key in ("+", "/", "[", "]"):
                    add_model_buffer += key
                render()
                continue
            if key in ("escape", "ctrl-c"):
                _styles.apply_theme(_original_theme)
                sys.stdout.write("\033[?25h\033[?1049l")
                print()
                return None, None
            elif key == "left":
                active_tab = (active_tab - 1) % len(tabs)
            elif key == "right":
                active_tab = (active_tab + 1) % len(tabs)
            elif key == "up":
                if _is_model_tab() and filtered_model_indices[active_tab]:
                    indices = filtered_model_indices[active_tab]
                    idx = indices.index(model_cursor[active_tab])
                    model_cursor[active_tab] = indices[(idx - 1) % len(indices)]
                    _sync_model_page_from_cursor()
                elif active_tab == SETTINGS_TAB:
                    visible = _visible_settings()
                    if visible:
                        settings_cursor = (settings_cursor - 1) % len(visible)
            elif key == "down":
                if _is_model_tab() and filtered_model_indices[active_tab]:
                    indices = filtered_model_indices[active_tab]
                    idx = indices.index(model_cursor[active_tab])
                    model_cursor[active_tab] = indices[(idx + 1) % len(indices)]
                    _sync_model_page_from_cursor()
                elif active_tab == SETTINGS_TAB:
                    visible = _visible_settings()
                    if visible:
                        settings_cursor = (settings_cursor + 1) % len(visible)
            elif key == "space":
                if _is_model_tab() and filtered_model_indices[active_tab]:
                    cur = model_cursor[active_tab]
                    model_items[cur]["selected"] = not model_items[cur]["selected"]
                elif active_tab == SETTINGS_TAB:
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
                            item["value"] = not item["value"]
                        # Clamp cursor if visibility changed
                        new_visible = _visible_settings()
                        if settings_cursor >= len(new_visible):
                            settings_cursor = max(0, len(new_visible) - 1)
            elif key in ("enter", "tab"):
                break
            elif key == "ctrl-a" and _is_model_tab():
                category = _model_tab_category(active_tab)
                for it in model_items:
                    if it.get("category") == category:
                        it["selected"] = True
            elif key == "ctrl-n" and _is_model_tab():
                category = _model_tab_category(active_tab)
                for it in model_items:
                    if it.get("category") == category:
                        it["selected"] = False
            elif key in ("[", "]") and _is_model_tab() and filtered_model_indices[active_tab]:
                pcount = _model_page_count()
                if key == "[":
                    model_page[active_tab] = (model_page[active_tab] - 1) % pcount
                else:
                    model_page[active_tab] = (model_page[active_tab] + 1) % pcount
                start, end = _model_page_bounds()
                indices = filtered_model_indices[active_tab]
                model_cursor[active_tab] = indices[start if start < end else 0]
            elif key == "+" and _is_model_tab():
                adding_model = True
                adding_model_tab = active_tab
                add_model_buffer = ""
                sys.stdout.write("\033[?25h")
            elif _is_model_tab() and key == "backspace":
                if model_search_query[active_tab]:
                    model_search_query[active_tab] = model_search_query[active_tab][:-1]
                    _refresh_model_filter(preserve_current=False)
            elif _is_model_tab() and _is_printable_search_char(key):
                model_search_query[active_tab] += key
                _refresh_model_filter(preserve_current=False)
            render()
    finally:
        sys.stdout.write("\033[?25h\033[?1049l")
        if _HAS_SIGWINCH:
            signal.signal(signal.SIGWINCH, _old_sigwinch)
            os.close(_wr)
            os.close(_ww)

    selected = {it["short"]: it["id"] for it in model_items if it["selected"]}
    if not selected:
        print(f"\n  {S.HYEL}No models selected — keeping current config.{S.RST}")
        return None, None

    new_config = dict(current_config)
    for item in settings_items:
        new_config[item["key"]] = item["value"]
    new_config["image_model_ids"] = [
        it["id"] for it in model_items if it["selected"] and it.get("category") == "image"
    ]

    print()
    return selected, new_config


def run_config_menu(
    api_key: str,
    current_mapping: dict[str, str] | None = None,
    current_config: dict[str, Any] | None = None,
    prefetched: tuple[list[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """Fetch models from OpenRouter and open the tabbed config menu.

    If *prefetched* is supplied as ``(available, pricing_lookup)`` the
    network call is skipped entirely.
    """
    from wavebench.storage import load_config

    if prefetched is not None:
        available, pricing_lookup = prefetched
    else:
        print(f"  {_work} {S.DIM}Fetching models from OpenRouter…{S.RST}")
        # Keep this at least as high as MODEL_MENU_LIMIT; the UI applies its own cap.
        available, pricing_lookup = fetch_top_models(api_key, count=200)
    if not available:
        print(f"  {S.DIM}Could not fetch remote models — showing local config only.{S.RST}")
    print()
    if current_mapping is None:
        current_mapping = MODEL_MAPPING
    if current_config is None:
        current_config = load_config()
    return interactive_config_menu(
        available, current_mapping, current_config, pricing_lookup=pricing_lookup
    )
