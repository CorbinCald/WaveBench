"""Menu package.

Exposes the four menu entry points that other parts of the package
consume. Internally split into ``model_list.py`` (catalog browser),
``config_menu.py`` (tabbed configuration), and ``_shared.py`` (private
helpers used by both).
"""

from .config_menu import interactive_config_menu, run_config_menu
from .model_list import interactive_model_menu, run_model_selection

__all__ = [
    "interactive_config_menu",
    "interactive_model_menu",
    "run_config_menu",
    "run_model_selection",
]
