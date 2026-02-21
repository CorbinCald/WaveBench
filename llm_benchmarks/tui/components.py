import sys
import time
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional

from llm_benchmarks.tui.styles import S, _SPIN, format_duration, _rule, _tw, _truncate, _dot

BAR_WIDTH = 20
_BAR_FILL = "█"
_BAR_EMPTY = "░"


def _render_token_bar(chars: int, scale: float) -> str:
    """Return a green progress bar string of fixed *BAR_WIDTH* characters."""
    ratio = min(chars / max(scale, 1), 1.0)
    filled = round(ratio * BAR_WIDTH)
    empty = BAR_WIDTH - filled
    return (f"{S.HGRN}{_BAR_FILL * filled}{S.RST}"
            f"{S.DIM}{_BAR_EMPTY * empty}{S.RST}")


class ProgressTracker:
    """Multi-line animated progress display with live token bars.

    While active every ``print()`` is intercepted: the drawn progress
    section is cleared first so output never overlaps the animation.

    Models register / unregister themselves and push character-count
    updates; the animation loop renders a green bar for each active
    model plus a summary spinner line at the bottom.
    """

    def __init__(self, total: int, results: Dict[str, Any],
                 pad: int = 16, label: str = "Generating"):
        self._total = total
        self._results = results
        self._label = label
        self._pad = pad
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._start = time.monotonic()
        self._is_tty = sys.stdout.isatty()
        self._original_print = None
        self._active: Dict[str, Dict[str, Any]] = {}
        self._parsing: Dict[str, Dict[str, Any]] = {}
        self._drawn_lines = 0

    @property
    def is_running(self) -> bool:
        return self._running

    def register(self, model_name: str) -> None:
        """Mark a model as actively streaming."""
        self._active[model_name] = {"chars": 0, "start": time.monotonic()}

    def update(self, model_name: str, chars: int) -> None:
        """Update the character count for an active model."""
        if model_name in self._active:
            self._active[model_name]["chars"] = chars

    def unregister(self, model_name: str) -> None:
        """Remove a model from the active set."""
        self._active.pop(model_name, None)

    def mark_parsing(self, model_name: str) -> None:
        """Move a model into the parsing state (shown in animated section)."""
        self._parsing[model_name] = {"start": time.monotonic()}

    def finish_parsing(self, model_name: str) -> None:
        """Remove a model from the parsing state."""
        self._parsing.pop(model_name, None)

    async def start(self) -> None:
        if not self._is_tty:
            return
        self._running = True
        self._start = time.monotonic()
        self._install_hook()
        self._task = asyncio.create_task(self._animate())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._clear_drawn()
        sys.stdout.flush()
        self._uninstall_hook()

    # ── internal rendering ────────────────────────────────────────────────

    def _clear_drawn(self) -> None:
        """Erase all previously drawn progress lines."""
        if self._drawn_lines <= 0:
            return
        if self._drawn_lines > 1:
            sys.stdout.write(f"\033[{self._drawn_lines - 1}A")
        sys.stdout.write("\r\033[J")
        self._drawn_lines = 0

    async def _animate(self) -> None:
        idx = 0
        try:
            while self._running:
                self._clear_drawn()

                done = len(self._results)
                elapsed = format_duration(time.monotonic() - self._start)
                frame = _SPIN[idx % len(_SPIN)]

                buf: list[str] = []
                lines = 0

                for name, info in list(self._parsing.items()):
                    mel = format_duration(
                        time.monotonic() - info["start"])
                    dots = "·" * (1 + (idx // 4) % 3)
                    buf.append(
                        f"  {S.HCYN}◌{S.RST} "
                        f"{name:<{self._pad}}  "
                        f"{S.DIM}parsing{dots:<4}"
                        f"  {mel:>7}{S.RST}\033[K\n")
                    lines += 1

                if self._active:
                    max_chars = max(
                        m["chars"] for m in self._active.values())
                    scale = max(max_chars * 1.3, 2000)

                    for name, info in self._active.items():
                        mel = format_duration(
                            time.monotonic() - info["start"])
                        if info["chars"] == 0:
                            dots = "·" * (1 + (idx // 4) % 3)
                            buf.append(
                                f"  {S.HYEL}●{S.RST} "
                                f"{name:<{self._pad}}  "
                                f"{S.DIM}reasoning{dots:<4}"
                                f" {mel:>7}{S.RST}\033[K\n")
                        else:
                            bar = _render_token_bar(
                                info["chars"], scale)
                            est_tk = info["chars"] // 4
                            buf.append(
                                f"  {S.HYEL}●{S.RST} "
                                f"{name:<{self._pad}}  "
                                f"{bar}  "
                                f"{S.DIM}~{est_tk:,} tk"
                                f"  {mel}{S.RST}\033[K\n")
                        lines += 1

                buf.append(
                    f"  {S.HCYN}{frame}{S.RST} "
                    f"{self._label}  "
                    f"{S.DIM}{done}/{self._total} complete · "
                    f"{elapsed}{S.RST}\033[K")
                lines += 1

                sys.stdout.write("\r" + "".join(buf))
                sys.stdout.flush()
                self._drawn_lines = lines
                idx += 1
                await asyncio.sleep(0.08)
        except asyncio.CancelledError:
            pass

    # ── print() hook ──────────────────────────────────────────────────────

    def _install_hook(self) -> None:
        import builtins
        self._original_print = builtins.print
        tracker = self
        original = self._original_print

        def _hooked(*args: Any, **kwargs: Any) -> None:
            if tracker._running:
                tracker._clear_drawn()
            original(*args, **kwargs)

        builtins.print = _hooked  # type: ignore[assignment]

    def _uninstall_hook(self) -> None:
        import builtins
        if self._original_print is not None:
            builtins.print = self._original_print  # type: ignore[assignment]
            self._original_print = None


def display_analytics(history: Dict[str, Any], compact: bool = False, pad: int = 16) -> None:
    """Print lifetime model performance analytics."""
    runs = history.get("runs", [])
    if not runs:
        print(f"  {S.DIM}No history yet. Complete a run to begin tracking.{S.RST}")
        return

    # ── Aggregate per-model stats ──────────────────────────────────────────
    stats: Dict[str, Any] = {}
    for run in runs:
        for name, res in run.get("models", {}).items():
            if name not in stats:
                stats[name] = {"runs": 0, "ok": 0, "fail": 0,
                               "cancel": 0, "times": [], "tokens": []}
            s = stats[name]
            s["runs"] += 1
            status = res.get("status", "failed")
            if status == "success":
                s["ok"] += 1
                t = res.get("time_s")
                if t is not None:
                    s["times"].append(t)
                usage = res.get("usage", {})
                tkns = usage.get("total_tokens")
                if tkns is not None:
                    s["tokens"].append(tkns)
            elif status == "cancelled":
                s["cancel"] += 1
            else:
                s["fail"] += 1

    # Sort: success-rate desc, then average time asc
    ranked = sorted(stats.items(), key=lambda x: (
        -(x[1]["ok"] / x[1]["runs"] if x[1]["runs"] else 0),
        (sum(x[1]["times"]) / len(x[1]["times"])
         if x[1]["times"] else float("inf")),
    ))

    n = len(runs)
    col = max((len(name) for name, _ in ranked), default=12) + 2
    col = max(col, pad)

    # ── Section header ─────────────────────────────────────────────────────
    print()
    _rule(f"Lifetime Analytics ({n} run{'s' if n != 1 else ''})")
    print()

    # ── Table header ───────────────────────────────────────────────────────
    hdr = (f"  {S.BOLD}{'MODEL':<{col}}{'RUNS':>5}  {'RATE':>5}"
           f"  {'AVG':>8}  {'BEST':>8}  {'WORST':>8}  {'AVG TKNS':>9}{S.RST}")
    print(hdr)
    print(f"  {S.DIM}{'─' * min(col + 51, _tw() - 4)}{S.RST}")

    # ── Table rows ─────────────────────────────────────────────────────────
    total_calls = total_ok = 0
    all_times: list[float] = []

    for name, s in ranked:
        rate = (s["ok"] / s["runs"] * 100) if s["runs"] else 0
        avg_v  = sum(s["times"]) / len(s["times"]) if s["times"] else None
        best_v = min(s["times"]) if s["times"] else None
        wrst_v = max(s["times"]) if s["times"] else None
        avg_tk = sum(s["tokens"]) / len(s["tokens"]) if s["tokens"] else None

        rate_s = f"{rate:>4.0f}%"
        if rate >= 90:
            rate_c = f"{S.HGRN}{rate_s}{S.RST}"
        elif rate >= 60:
            rate_c = f"{S.HYEL}{rate_s}{S.RST}"
        else:
            rate_c = f"{S.HRED}{rate_s}{S.RST}"

        avg_tk_s = f"{int(avg_tk):,}" if avg_tk is not None else "—"

        print(f"  {name:<{col}}{s['runs']:>5}  {rate_c}"
              f"  {S.CYN}{format_duration(avg_v):>8}{S.RST}"
              f"  {S.GRN}{format_duration(best_v):>8}{S.RST}"
              f"  {S.DIM}{format_duration(wrst_v):>8}{S.RST}"
              f"  {S.DIM}{avg_tk_s:>9}{S.RST}")

        total_calls += s["runs"]
        total_ok += s["ok"]
        all_times.extend(s["times"])

    # ── Totals ─────────────────────────────────────────────────────────────
    overall = (total_ok / total_calls * 100) if total_calls else 0
    avg_all = format_duration(
        sum(all_times) / len(all_times) if all_times else None
    )
    all_tokens = [t for s in stats.values() for t in s["tokens"]]
    avg_tk_all = f"{int(sum(all_tokens) / len(all_tokens)):,}" if all_tokens else "—"

    print()
    print(f"  {total_calls} calls {_dot} {total_ok} passed {_dot} "
          f"{S.BOLD}{overall:.0f}%{S.RST} {_dot} avg {S.CYN}{avg_all}{S.RST} "
          f"{_dot} avg tkns {S.DIM}{avg_tk_all}{S.RST}")

    # ── Recent prompts (full view only) ────────────────────────────────────
    if not compact and runs:
        print()
        _rule("Recent Prompts")
        print()
        for run in reversed(runs[-8:]):
            ts = run.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts)
                date_s = dt.strftime("%b %d %H:%M")
            except (ValueError, TypeError):
                date_s = "—"
            models = run.get("models", {})
            ok = sum(1 for r in models.values()
                     if r.get("status") == "success")
            tot = len(models)
            prompt = _truncate(run.get("prompt", "—"), _tw() - 26)
            print(f"  {S.DIM}{date_s}{S.RST}  {ok}/{tot}  {prompt}")
