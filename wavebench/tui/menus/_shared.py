"""Small helpers shared by both the model-list menu and the tabbed config menu.

Pure functions only — no I/O, no side effects. Kept private (underscored
module name) because these are implementation details of the menu
package, not public API.
"""

from __future__ import annotations

import re
from typing import Any

# UI-side cap applied after fetch_top_models(); fetch call sites should
# request at least this many so the catalog is not capped twice.
MODEL_MENU_LIMIT = 200


def _format_price(pricing_dict: dict[str, Any]) -> str:
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
    name = model_id.split("/")[-1] if "/" in model_id else model_id
    parts = re.split(r"[-_]+", name)
    if not parts:
        return name
    result = parts[0].lower()
    for p in parts[1:]:
        if p:
            result += p if p[0].isdigit() else p[0].upper() + p[1:]
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
    return text if len(text) <= width else text[: width - 1] + "…"


def _filter_model_indices(items: list[dict[str, Any]], query: str) -> list[int]:
    """Return item indices matching the current search query."""
    needle = query.strip().lower()
    if not needle:
        return list(range(len(items)))
    return [
        i
        for i, item in enumerate(items)
        if needle in item["short"].lower() or needle in item["id"].lower()
    ]


def _is_printable_search_char(key: str) -> bool:
    """Allow plain printable characters in search mode."""
    return len(key) == 1 and key.isprintable() and key not in ("\t", "\r", "\n")
