import sys
import math
import time
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional

from llm_benchmarks.tui.styles import (
    S, _SPIN, format_duration, _tw, _truncate, _dot,
    _box_top, _box_row, _box_sep, _box_bot,
    PHASE_GRADIENT, PULSE_GRADIENT, PULSE_DIM,
)

BAR_WIDTH = 20

_PULSE_CHARS: list = [
    ['⠀'],                          # 0: blank
    ['⡀', '⢀', '⣀'],              # 1: bottom row
    ['⡄', '⢠', '⣤', '⣄'],        # 2: bottom 2 rows
    ['⡆', '⢰', '⣶', '⣦'],        # 3: bottom 3 rows
    ['⡇', '⢸', '⣏', '⣼'],        # 4: all 4 rows, partial
    ['⣷', '⣾', '⣻', '⣯'],        # 5: near-full (7/8 dots)
    ['⣿'],                          # 6: full block
]


def _render_pulse_bar(chars: int, scale: float, tick: int) -> str:
    """Animated braille pulse-wave progress bar.

    Density builds from the bottom row of each braille cell upward,
    creating a waveform that rises and falls as packets scroll right.
    A density floor keeps the baseline alive between peaks.
    """
    ratio = min(chars / max(scale, 1), 1.0)
    filled = round(ratio * BAR_WIDTH)
    empty = BAR_WIDTH - filled

    phase = tick * 0.35
    parts: list[str] = []
    prev_level = -1

    densities: list[float] = []
    for i in range(filled):
        w1 = max(0.0, math.cos(i * 0.9 - phase)) ** 1.5
        w2 = max(0.0, math.cos(i * 1.5 - phase * 0.7 + 1.5)) ** 1.8
        d = min(1.0, w1 * 0.65 + w2 * 0.35 + 0.12)

        edge = filled - 1 - i
        if edge < 3 and filled > 4:
            d = min(1.0, d + (3 - edge) * 0.2)
        densities.append(d)

    for i, d in enumerate(densities):
        level = max(1, min(6, round(d * 6)))

        if level >= 6:
            prev_d = densities[i - 1] if i > 0 else 0.0
            next_d = densities[i + 1] if i < filled - 1 else 0.0
            if not (d > prev_d and d >= next_d):
                level = 5

        pool = _PULSE_CHARS[level]
        ch = pool[(i + tick) % len(pool)]

        if level != prev_level:
            if prev_level >= 0:
                parts.append(S.RST)
            parts.append(PULSE_GRADIENT[level])
            prev_level = level
        parts.append(ch)

    if filled:
        parts.append(S.RST)

    stray: list[str] = []
    for i in range(empty):
        pos = filled + i
        prox = 1.0 - i / max(empty, 1)
        scatter_val = (pos * 7 + tick * 3) % 17
        if scatter_val < 2 + int(prox * 4):
            level_val = (pos * 11 + tick * 5) % 11
            if level_val < 1 and prox > 0.5:
                sl = 3
            elif level_val < 3 and prox > 0.2:
                sl = 2
            else:
                sl = 1
            pool = _PULSE_CHARS[sl]
            stray.append(pool[(pos + tick) % len(pool)])
        else:
            stray.append('⠀')

    if stray:
        parts.append(PULSE_DIM)
        parts.extend(stray)
        parts.append(S.RST)

    return ''.join(parts)


class ProgressTracker:
    """Multi-line animated progress display with live token bars.

    While active every ``print()`` is intercepted: the drawn progress
    section is cleared first so output never overlaps the animation.

    Models register / unregister themselves and push character-count
    updates; the animation loop renders a green bar for each active
    model plus a summary spinner line at the bottom.
    """

    DEFAULT_AVG_TOKENS = 2000

    def __init__(self, total: int, results: Dict[str, Any],
                 pad: int = 16, label: str = "Generating",
                 model_names: Optional[list] = None,
                 avg_tokens: Optional[Dict[str, float]] = None):
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
        self._model_names = model_names or []
        self._avg_tokens = avg_tokens or {}

    @property
    def is_running(self) -> bool:
        return self._running

    def register(self, model_name: str) -> None:
        """Mark a model as actively streaming."""
        self._active[model_name] = {
            "chars": 0, "start": time.monotonic(),
            "last_chars": 0, "last_rate_time": 0.0, "smoothed_rate": 0.0,
        }

    def update(self, model_name: str, chars: int) -> None:
        """Update the character count for an active model."""
        if model_name in self._active:
            self._active[model_name]["chars"] = chars

    def unregister(self, model_name: str) -> None:
        """Remove a model from the active set."""
        self._active.pop(model_name, None)

    def mark_parsing(self, model_name: str) -> None:
        """Move a model into the parsing state (shown in animated section)."""
        last_chars = 0
        if model_name in self._active:
            last_chars = self._active[model_name].get("chars", 0)
        self._parsing[model_name] = {"start": time.monotonic(), "chars": last_chars}

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

    @staticmethod
    def _phase_boxes(filled: int) -> str:
        """Render a 5-stage inline progress indicator with uniform color that
        shifts from deep blue → bright neon cyan as more boxes fill."""
        filled = max(0, min(filled, 5))
        color = PHASE_GRADIENT[filled - 1] if filled > 0 else ""
        on = f"{color}{'■' * filled}{S.RST}" if filled else ""
        off = f"{S.DIM}{'□' * (5 - filled)}{S.RST}" if filled < 5 else ""
        return f"{on}{off}"

    def _token_boxes(self, model_name: str, chars: int) -> str:
        """Boxes based on token progress: each box = 20% of avg tokens."""
        avg = self._avg_tokens.get(model_name, self.DEFAULT_AVG_TOKENS)
        est_tokens = chars / 4
        pct = min(est_tokens / max(avg, 1), 1.0)
        filled = int(pct * 5)
        if pct > 0 and filled == 0:
            filled = 1
        return self._phase_boxes(filled)

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
                    boxes = self._token_boxes(name, info.get("chars", 0))
                    buf.append(
                        f"  {boxes} "
                        f"{name:<{self._pad}}  "
                        f"{S.DIM}parsing{dots:<4}"
                        f"  {mel:>7}{S.RST}\033[K\n")
                    lines += 1

                if self._active:
                    for name, info in self._active.items():
                        mel = format_duration(
                            time.monotonic() - info["start"])
                        if info["chars"] == 0:
                            dots = "·" * (1 + (idx // 4) % 3)
                            boxes = self._token_boxes(name, 0)
                            buf.append(
                                f"  {boxes} "
                                f"{name:<{self._pad}}  "
                                f"{S.DIM}reasoning{dots:<4}"
                                f" {mel:>7}{S.RST}\033[K\n")
                        else:
                            now = time.monotonic()
                            dt = now - info["last_rate_time"]
                            if dt >= 0.5:
                                if info["last_rate_time"] == 0.0:
                                    info["last_chars"] = info["chars"]
                                    info["last_rate_time"] = now
                                else:
                                    d_chars = info["chars"] - info["last_chars"]
                                    instant_tks = (d_chars / 4) / dt
                                    if info["smoothed_rate"] <= 0:
                                        info["smoothed_rate"] = instant_tks
                                    else:
                                        info["smoothed_rate"] = (
                                            0.3 * instant_tks
                                            + 0.7 * info["smoothed_rate"])
                                    info["last_chars"] = info["chars"]
                                    info["last_rate_time"] = now

                            boxes = self._token_boxes(name, info["chars"])
                            avg_tk = self._avg_tokens.get(
                                name, self.DEFAULT_AVG_TOKENS)
                            model_scale = avg_tk * 4
                            bar = _render_pulse_bar(
                                info["chars"], model_scale, idx)
                            est_tk = info["chars"] // 4
                            rate_s = ""
                            if info["smoothed_rate"] > 0:
                                rate_s = (
                                    f"  {S.CYN}"
                                    f"{int(info['smoothed_rate']):,} tk/s"
                                    f"{S.RST}")
                            buf.append(
                                f"  {boxes} "
                                f"{name:<{self._pad}}  "
                                f"{bar}  "
                                f"{S.DIM}~{est_tk:,} tk{S.RST}"
                                f"{rate_s}"
                                f"  {S.DIM}{mel}{S.RST}\033[K\n")
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

    w = _tw() - 4
    inner = w - 4

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

    # ── Box header ─────────────────────────────────────────────────────────
    print()
    print(_box_top(f"Lifetime Analytics ({n} run{'s' if n != 1 else ''})", w))
    print(_box_row("", w))

    # ── Table header ───────────────────────────────────────────────────────
    hdr = (f"{S.BOLD}{'MODEL':<{col}}{'RUNS':>5}  {'RATE':>5}"
           f"  {'AVG':>8}  {'BEST':>8}  {'WORST':>8}  {'AVG TKNS':>9}{S.RST}")
    print(_box_row(hdr, w))
    print(_box_row(f"{S.DIM}{'─' * min(col + 51, inner)}{S.RST}", w))

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

        print(_box_row(
            f"{name:<{col}}{s['runs']:>5}  {rate_c}"
            f"  {S.CYN}{format_duration(avg_v):>8}{S.RST}"
            f"  {S.GRN}{format_duration(best_v):>8}{S.RST}"
            f"  {S.DIM}{format_duration(wrst_v):>8}{S.RST}"
            f"  {S.DIM}{avg_tk_s:>9}{S.RST}", w))

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

    print(_box_row("", w))
    print(_box_row(
        f"{total_calls} calls {_dot} {total_ok} passed {_dot} "
        f"{S.BOLD}{overall:.0f}%{S.RST} {_dot} avg {S.CYN}{avg_all}{S.RST} "
        f"{_dot} avg tkns {S.DIM}{avg_tk_all}{S.RST}", w))

    # ── Recent prompts (full view only) ────────────────────────────────────
    if not compact and runs:
        print(_box_sep("Recent Prompts", w))
        print(_box_row("", w))
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
            prompt = _truncate(run.get("prompt", "—"), inner - 20)
            print(_box_row(
                f"{S.DIM}{date_s}{S.RST}  {ok}/{tot}  {prompt}", w))

    print(_box_bot(w))
