"""Analytics-rendering package.

Exposes ``compute_cost`` (pure helper shared with the live progress
tracker) and ``display_analytics`` (lifetime leaderboard printer).
"""

from .cost import compute_cost
from .table import display_analytics

__all__ = ["compute_cost", "display_analytics"]
