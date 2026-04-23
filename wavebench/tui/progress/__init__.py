"""Progress-display package.

Re-exports the two names callers reach for — ``ProgressTracker`` for the
live multi-model animation, and ``render_idle_wave`` for the idle-menu
background wave used by ``__main__``. Internal helpers live in
``wave.py`` and ``tracker.py``; callers should import from this package.
"""

from .tracker import ProgressTracker
from .wave import render_idle_wave

__all__ = ["ProgressTracker", "render_idle_wave"]
