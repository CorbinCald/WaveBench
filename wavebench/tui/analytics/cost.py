"""Cost computation shared by the live progress tracker and the analytics table.

Kept in ``tui/analytics/`` (rather than ``tui/progress/``) because analytics
is the primary consumer — the tracker imports cost, not the other way
around, which keeps the dependency direction one-way.
"""

from __future__ import annotations

from typing import Any


def compute_cost(usage: dict[str, Any], pricing: dict[str, Any]) -> float | None:
    """Compute the dollar cost of a single model call from usage + pricing.

    Returns None when pricing data is unavailable or the cost is zero.
    """
    if not pricing or not usage:
        return None
    try:
        pp = float(pricing.get("prompt") or 0)
        cp = float(pricing.get("completion") or 0)
    except (TypeError, ValueError):
        return None
    prompt_tokens = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    cost = prompt_tokens * pp + completion_tokens * cp
    return cost if cost > 0 else None
