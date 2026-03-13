import sys
import math
import time
import shutil
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional

try:
    import termios
except ImportError:
    termios = None  # type: ignore[assignment]

from wavebench.tui.styles import (
    S, _SPIN, format_duration, format_cost, _tw, _truncate, _dot,
    _ok, _fail, _skip, _arrow, _rpad, _vlen,
    _box_top, _box_row, _box_sep, _box_bot, _box_divider,
    PHASE_GRADIENT, PULSE_GRADIENT, PULSE_DIM, TITLE_WAVE_GRADIENT, _NO_COLOR,
)

BAR_WIDTH = 20

_WAVE_CHARS: list = [
    ['⠀'],                          # 0: empty
    ['⡀', '⢀'],                    # 1: single bottom dot
    ['⣀'],                          # 2: full bottom row
    ['⣄', '⣠'],                    # 3: bottom row + half of row 3
    ['⣤'],                          # 4: bottom 2 rows
    ['⣦', '⣴'],                    # 5: bottom 2 rows + half of row 2
    ['⣶'],                          # 6: bottom 3 rows
    ['⣷', '⣾'],                    # 7: bottom 3 rows + half of top
    ['⣿'],                          # 8: full block
]


def _title_wave(tick: int, width: int = 5) -> str:
    """Animated braille wave for the 'Generating' box title."""
    parts: list[str] = []
    prev_level = -1
    for i in range(width):
        val = math.sin(tick * 0.15 - i * 0.7) * 0.5 + 0.5
        level = max(1, min(8, round(val * 8)))
        pool = _WAVE_CHARS[level]
        ch = pool[(i + tick) % len(pool)]
        if not _NO_COLOR and level != prev_level:
            if prev_level >= 0:
                parts.append(S.RST)
            parts.append(TITLE_WAVE_GRADIENT[level])
            prev_level = level
        parts.append(ch)
    if not _NO_COLOR and prev_level >= 0:
        parts.append(S.RST)
    return ''.join(parts)


def _render_pulse_bar(chars: int, scale: float, tick: int,
                      phase: float = 0.0,
                      bar_width: int = BAR_WIDTH) -> str:
    """Animated braille wave progress bar.

    Height builds from the bottom row of each braille cell upward
    following a sine wave that scrolls right.  *phase* is accumulated
    externally (driven by token rate) so scroll speed tracks throughput.
    Amplitude scales with token progress so the wave grows as output fills.
    """
    ratio = min(chars / max(scale, 1), 1.0)
    filled = round(ratio * bar_width)
    empty = bar_width - filled

    t = tick * 0.008
    bandwidth = 0.70
    amplitude = (0.35 + 0.65 * ratio) * (0.92 + 0.08 * math.sin(t * 1.7 + 2.0))
    parts: list[str] = []
    prev_level = -1

    for i in range(filled):
        w = math.sin(i * bandwidth - phase)
        w2 = math.sin(i * bandwidth * 1.8 - phase * 0.6 + 1.2) * 0.25
        val = max(0.0, min(1.0, (w + w2) * 0.5 + 0.5)) * amplitude
        val = max(0.12, val)

        edge = filled - 1 - i
        if edge < 3 and filled > 4:
            val = min(1.0, val + (3 - edge) * 0.15)

        level = max(1, min(8, round(val * 8)))

        pool = _WAVE_CHARS[level]
        ch = pool[(i + tick) % len(pool)]

        if level != prev_level:
            if prev_level >= 0:
                parts.append(S.RST)
            parts.append(PULSE_GRADIENT[level])
            prev_level = level
        parts.append(ch)

    if filled:
        parts.append(S.RST)

    if empty > 0:
        parts.append(_render_pre_wave_bar(empty, tick))

    return ''.join(parts)


def _render_pre_wave_bar(width: int, tick: int) -> str:
    """Mini braille wave shown during pre-generation 'reasoning' state.

    Same wave style as the main progress bar but constrained to 1-3 dots
    high, with a per-character gradient from bright green (matching the
    leading edge of the main bar) to grey-blue, pulsing over time.
    """
    parts: list[str] = []
    t = tick * 0.06
    phase = tick * 0.10

    for i in range(width):
        pos = i / max(width - 1, 1)

        w = math.sin(i * 0.6 - phase)
        w2 = math.sin(i * 1.05 - phase * 0.65 + 1.3) * 0.28
        val = max(0.0, min(1.0, (w + w2) * 0.5 + 0.5))
        level = max(1, min(3, round(val * 3)))

        pool = _WAVE_CHARS[level]
        ch = pool[(i + tick) % len(pool)]

        if _NO_COLOR:
            parts.append(ch)
            continue

        p1 = math.sin(t + i * 0.35) * 0.5 + 0.5
        p2 = math.sin(t * 0.6 + i * 0.2 + 2.0) * 0.3 + 0.5
        p = p1 * 0.65 + p2 * 0.35

        sr, sg, sb = 30 + p * 35, 225 + p * 18, 72 + p * 22
        tr, tg, tb = 60 + p * 15, 88 + p * 14, 130 + p * 20

        r = max(0, min(255, int(sr + (tr - sr) * pos)))
        g = max(0, min(255, int(sg + (tg - sg) * pos)))
        b = max(0, min(255, int(sb + (tb - sb) * pos)))

        parts.append(f"\033[38;2;{r};{g};{b}m{ch}")

    if not _NO_COLOR:
        parts.append(S.RST)
    return ''.join(parts)


def render_idle_wave(tick: int, width: int, height: int,
                     intensity: float = 0.0,
                     wave_phase: Optional[float] = None) -> list[str]:
    """Render one frame of an animated ocean wave.

    Returns *height* ANSI-colored strings, each *width* visible characters
    wide.  *intensity* (0.0--1.0) controls wave energy: 0.0 gives a calm,
    low swell; 1.0 gives tall, fast, sharply cresting waves.  All wave
    physics -- amplitude, phase speed, harmonic content, Stokes nonlinearity,
    and gradient color -- interpolate smoothly between those extremes.
    """
    if _NO_COLOR or height <= 0 or width <= 0:
        return [' ' * width] * max(height, 0)

    intensity = max(0.0, min(1.0, intensity))
    total_sp = height * 8

    amp_scale = 0.14 + 0.24 * intensity
    amp_breath = 0.06 + 0.12 * intensity
    amp = total_sp * amp_scale * (1.0 + amp_breath * math.sin(tick * 0.019 + 1.0))

    center_norm = 0.75 - 0.20 * intensity
    center_sway = 0.12 + 0.22 * intensity
    center = total_sp * center_norm + amp * center_sway * math.sin(tick * 0.024)

    spd = 0.35 + 0.75 * intensity
    if wave_phase is None:
        wave_phase = tick * spd

    h1_amp = 0.62
    h2_amp = 0.08 + 0.14 * intensity
    h3_amp = 0.02 + 0.08 * intensity
    h4_amp = 0.01 + 0.03 * intensity

    stokes = 0.06 + 0.18 * intensity
    crest_exp = 1.3 + 0.5 * intensity
    limiter = 0.35 - 0.15 * intensity

    ct = max(0.0, min(1.0, intensity + 0.03 * math.sin(tick * 0.03)))
    if ct < 0.5:
        f = ct * 2.0
        cr, cg, cb = 10 * (1.0 - f), 48 + 107 * f, 90 - 30 * f
    else:
        f = (ct - 0.5) * 2.0
        cr, cg, cb = 85 * f, 155 + 100 * f, 60 + 105 * f
    wave_color = f"\033[38;2;{int(cr)};{int(cg)};{int(cb)}m"

    surfaces: list[float] = []
    for col in range(width):
        nx = col / max(width - 1, 1)
        h = (h1_amp * math.sin(nx * 14.0 - wave_phase * 0.107)
             + h2_amp * math.sin(nx * 26.0 - wave_phase * 0.147 + 1.7)
             + h3_amp * math.sin(nx * 44.0 - wave_phase * 0.187 + 3.1)
             + h4_amp * math.sin(nx * 68.0 - wave_phase * 0.240 + 0.9))
        h += stokes * h * h
        if h > 0:
            h = h ** crest_exp
            h /= 1.0 + limiter * h
        else:
            h = -(abs(h) ** 1.3)
        surfaces.append(center - h * amp)

    rows: list[str] = []
    for row in range(height):
        cell_top = row * 8
        cell_bot = cell_top + 8
        parts: list[str] = []
        in_color = False

        for col in range(width):
            s = surfaces[col]

            if s <= cell_top:
                level = 8
            elif s >= cell_bot:
                level = 0
            else:
                level = max(1, min(7, round(cell_bot - s)))

            if level == 0:
                if in_color:
                    parts.append(S.RST)
                    in_color = False
                parts.append(' ')
                continue

            pool = _WAVE_CHARS[level]
            ch = pool[(col + tick) % len(pool)]

            if not in_color:
                parts.append(wave_color)
                in_color = True
            parts.append(ch)

        if in_color:
            parts.append(S.RST)
        rows.append(''.join(parts))

    return rows


def compute_cost(usage: Dict[str, Any], pricing: Dict[str, Any]) -> Optional[float]:
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
                 avg_tokens: Optional[Dict[str, float]] = None,
                 pricing_lookup: Optional[Dict[str, Any]] = None,
                 model_id_map: Optional[Dict[str, str]] = None,
                 alt_screen: bool = False):
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
        self._phases: Dict[str, float] = {}
        self._drawn_lines = 0
        self._model_names = model_names or []
        self._avg_tokens = avg_tokens or {}
        self._output_dir: Optional[str] = None
        self._rendered = False
        self._pricing_lookup = pricing_lookup or {}
        self._model_id_map = model_id_map or {}
        self._alt_screen = alt_screen and self._is_tty
        self._entered_alt_screen = False
        self._saved_termios = None
        self._wave_intensity = 0.0
        self._wave_completed_chars = 0
        self._wave_last_chars = 0
        self._wave_rate_time = 0.0
        self._wave_agg_rate = 0.0
        self._wave_phase = 0.0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def rendered_final(self) -> bool:
        return self._rendered

    def set_output_dir(self, path: str) -> None:
        self._output_dir = path

    def _model_pricing(self, model_name: str) -> Dict[str, Any]:
        """Return the pricing dict for a model, or empty dict."""
        mid = self._model_id_map.get(model_name, "")
        return self._pricing_lookup.get(mid, {})

    def _model_cost(self, model_name: str, info: Dict[str, Any]) -> Optional[float]:
        """Compute cost for a completed model result."""
        return compute_cost(info.get("usage", {}), self._model_pricing(model_name))

    def _total_cost(self) -> Optional[float]:
        """Sum costs across all completed results that have pricing."""
        total = 0.0
        any_cost = False
        for name, info in self._results.items():
            c = self._model_cost(name, info)
            if c is not None:
                total += c
                any_cost = True
        return total if any_cost else None

    def _live_cost_for_active(self, model_name: str, chars: int) -> Optional[float]:
        """Estimate live cost for an in-progress model from streamed chars."""
        pricing = self._model_pricing(model_name)
        if not pricing:
            return None
        try:
            cp = float(pricing.get("completion") or 0)
        except (TypeError, ValueError):
            return None
        if cp <= 0:
            return None
        est_tokens = chars / 4
        return est_tokens * cp

    def register(self, model_name: str) -> None:
        """Mark a model as actively streaming."""
        self._active[model_name] = {
            "chars": 0, "start": time.monotonic(),
            "last_chars": 0, "last_rate_time": 0.0, "smoothed_rate": 0.0,
        }
        self._phases[model_name] = 0.0

    def update(self, model_name: str, chars: int) -> None:
        """Update the character count for an active model."""
        if model_name in self._active:
            self._active[model_name]["chars"] = chars

    def unregister(self, model_name: str) -> None:
        """Remove a model from the active set."""
        info = self._active.pop(model_name, None)
        if info:
            self._wave_completed_chars += info.get("chars", 0)
        self._phases.pop(model_name, None)

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
        if self._alt_screen:
            if termios is not None:
                try:
                    fd = sys.stdin.fileno()
                    self._saved_termios = termios.tcgetattr(fd)
                    new = termios.tcgetattr(fd)
                    new[3] &= ~termios.ECHO
                    termios.tcsetattr(fd, termios.TCSANOW, new)
                except Exception:
                    self._saved_termios = None
            sys.stdout.write("\033[?1049h\033[?25l\033[H")
            sys.stdout.flush()
            self._entered_alt_screen = True
        self._running = True
        self._start = time.monotonic()
        self._install_hook()
        self._task = asyncio.create_task(self._animate())

    def _exit_alt_screen(self) -> None:
        """Leave the alternate screen buffer and restore terminal settings."""
        if not self._entered_alt_screen:
            return
        self._drawn_lines = 0
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        self._entered_alt_screen = False
        if self._saved_termios is not None and termios is not None:
            try:
                termios.tcsetattr(
                    sys.stdin.fileno(), termios.TCSANOW, self._saved_termios)
            except Exception:
                pass
            self._saved_termios = None

    async def stop(self) -> None:
        if not self._running:
            self._exit_alt_screen()
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._exit_alt_screen()
        self._render_final()
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

    @staticmethod
    def _flush_frame(frame: str) -> None:
        """Write a rendered frame to stdout in a thread-safe way.

        Called via ``run_in_executor`` so that PTY back-pressure (e.g.
        terminal window in the background) blocks the worker thread
        instead of the asyncio event loop.
        """
        sys.stdout.write(frame)
        sys.stdout.flush()

    def _clear_drawn(self) -> None:
        """Erase all previously drawn progress lines."""
        if self._entered_alt_screen:
            sys.stdout.write("\033[H")
            self._drawn_lines = 0
            return
        if self._drawn_lines <= 0:
            return
        if self._drawn_lines > 1:
            sys.stdout.write(f"\033[{self._drawn_lines - 1}A")
        sys.stdout.write("\r\033[J")
        self._drawn_lines = 0

    def _format_output_dir(self, inner_w: int) -> Optional[str]:
        if not self._output_dir:
            return None
        out = self._output_dir
        max_path = inner_w - 10
        if len(out) > max_path:
            out = "…" + out[-(max_path - 1):]
        return f"{S.DIM}{'OUTPUT':>8}  {out}{S.RST}"

    def _format_result_row(self, name: str, info: Dict[str, Any],
                           rank: int, inner_w: int) -> str:
        st = info.get("status", "failed")
        t = format_duration(info.get("time_s", 0))
        cost = self._model_cost(name, info)
        cost_s = f"  {S.HYEL}{format_cost(cost)}{S.RST}" if cost else ""
        if st == "success":
            sym = _ok
            usage = info.get("usage", {})
            tokens = usage.get("total_tokens")
            fname = info.get("file", "")
            tk_part = f"  {S.DIM}{tokens:,} tk{S.RST}" if tokens else ""
            detail = f"saved {_arrow} {S.GRN}{fname}{S.RST}{tk_part}{cost_s}"
        elif st == "cancelled":
            sym = _skip
            detail = f"{S.DIM}cancelled{S.RST}"
            fname = ""
        else:
            sym = _fail
            detail = f"{S.RED}failed{S.RST}{cost_s}"
            fname = ""
        rank_s = f"{S.DIM}{rank:>2}.{S.RST}"
        content = f"{rank_s} {sym} {_rpad(name, self._pad)}  {detail}"
        if st == "success" and _vlen(content) + 2 + len(t) > inner_w:
            overflow = _vlen(content) + 2 + len(t) - inner_w
            max_fname = max(8, len(fname) - overflow)
            fname = _truncate(fname, max_fname)
            detail = f"saved {_arrow} {S.GRN}{fname}{S.RST}{tk_part}{cost_s}"
            content = f"{rank_s} {sym} {_rpad(name, self._pad)}  {detail}"
        gap = max(inner_w - _vlen(content) - len(t), 2)
        return f"{content}{' ' * gap}{S.DIM}{t}{S.RST}"

    def _render_final(self) -> None:
        """Render the completed Run Results box (permanent output)."""
        self._clear_drawn()
        w = _tw() - 4
        inner_w = w - 4
        total_time = time.monotonic() - self._start

        buf: list[str] = []
        buf.append(_box_top("Run Results", w))
        od = self._format_output_dir(inner_w)
        if od:
            buf.append(_box_row(od, w))
            buf.append(_box_sep("", w))
        else:
            buf.append(_box_row("", w))

        def _rank_key(item: Any) -> Any:
            _, v = item
            order = {"success": 0, "failed": 1, "cancelled": 2}
            return (order.get(v.get("status", "failed"), 3),
                    v.get("time_s", 0))

        for i, (name, info) in enumerate(
            sorted(self._results.items(), key=_rank_key), 1
        ):
            buf.append(_box_row(
                self._format_result_row(name, info, i, inner_w), w))

        buf.append(_box_sep("", w))
        ok = sum(1 for v in self._results.values()
                 if v.get("status") == "success")
        fail = sum(1 for v in self._results.values()
                   if v.get("status") == "failed")
        canc = sum(1 for v in self._results.values()
                   if v.get("status") == "cancelled")
        parts: list[str] = []
        if ok:   parts.append(f"{S.HGRN}{ok} passed{S.RST}")
        if fail: parts.append(f"{S.HRED}{fail} failed{S.RST}")
        if canc: parts.append(f"{S.DIM}{canc} cancelled{S.RST}")
        parts.append(f"{format_duration(total_time)} total")
        total_cost = self._total_cost()
        if total_cost is not None:
            parts.append(f"{S.HYEL}{format_cost(total_cost)}{S.RST}")
        sep = f" {_dot} "
        buf.append(_box_row(sep.join(parts), w))
        buf.append(_box_bot(w))

        sys.stdout.write('\n'.join(buf) + '\n')
        self._rendered = True

    async def _animate(self) -> None:
        idx = 0
        try:
            while self._running:
                self._clear_drawn()

                term = shutil.get_terminal_size((80, 24))
                w = max(20, min(120, term.columns)) - 4
                inner_w = w - 4
                done = len(self._results)
                elapsed = format_duration(time.monotonic() - self._start)
                frame = _SPIN[idx % len(_SPIN)]

                buf: list[str] = []
                lines = 0

                wave = _title_wave(idx)
                buf.append(_box_top(f"Generating {wave}", w) + "\033[K\n")
                lines += 1

                od = self._format_output_dir(inner_w)
                if od:
                    buf.append(_box_row(od, w) + "\033[K\n")
                    buf.append(_box_sep("", w) + "\033[K\n")
                    lines += 2
                else:
                    buf.append(_box_row("", w) + "\033[K\n")
                    lines += 1

                _chrome = lines + 3
                max_model_rows = max(1, term.lines - _chrome)

                completed_idx = 0
                visible = 0
                hidden = 0
                for name in self._model_names:
                    if visible >= max_model_rows:
                        hidden += 1
                        if name in self._results:
                            completed_idx += 1
                        continue

                    if name in self._results:
                        completed_idx += 1
                        row = self._format_result_row(
                            name, self._results[name],
                            completed_idx, inner_w)
                    elif name in self._parsing:
                        pinfo = self._parsing[name]
                        mel = format_duration(
                            time.monotonic() - pinfo["start"])
                        dots = "·" * (1 + (idx // 4) % 3)
                        boxes = self._token_boxes(
                            name, pinfo.get("chars", 0))
                        row = (f"{boxes}   "
                               f"{_rpad(name, self._pad)}  "
                               f"{S.DIM}parsing{dots:<4}"
                               f"  {mel:>7}{S.RST}")
                    elif name in self._active:
                        ainfo = self._active[name]
                        mel = format_duration(
                            time.monotonic() - ainfo["start"])
                        if ainfo["chars"] == 0:
                            boxes = self._token_boxes(name, 0)
                            dots = "·" * (1 + (idx // 4) % 3)
                            suffix = (f"{S.DIM}reasoning{dots:<4}"
                                      f" {mel:>7}{S.RST}")
                            pfx = 5 + 3 + self._pad + 2 + 2
                            bw = max(5, min(
                                BAR_WIDTH,
                                inner_w - pfx - _vlen(suffix)))
                            bar = _render_pre_wave_bar(bw, idx)
                            row = (f"{boxes}   "
                                   f"{_rpad(name, self._pad)}  "
                                   f"{bar}  {suffix}")
                        else:
                            now = time.monotonic()
                            dt = now - ainfo["last_rate_time"]
                            if dt >= 0.5:
                                if ainfo["last_rate_time"] == 0.0:
                                    ainfo["last_chars"] = ainfo["chars"]
                                    ainfo["last_rate_time"] = now
                                else:
                                    d_chars = (ainfo["chars"]
                                               - ainfo["last_chars"])
                                    instant_tks = (d_chars / 4) / dt
                                    if ainfo["smoothed_rate"] <= 0:
                                        ainfo["smoothed_rate"] = instant_tks
                                    else:
                                        ainfo["smoothed_rate"] = (
                                            0.3 * instant_tks
                                            + 0.7 * ainfo["smoothed_rate"])
                                    ainfo["last_chars"] = ainfo["chars"]
                                    ainfo["last_rate_time"] = now

                            boxes = self._token_boxes(
                                name, ainfo["chars"])
                            avg_tk = self._avg_tokens.get(
                                name, self.DEFAULT_AVG_TOKENS)
                            model_scale = avg_tk * 4

                            rate = ainfo["smoothed_rate"]
                            rf = min(1.0, math.sqrt(max(0.0, rate) / 200.0))
                            spd = 0.12 + 0.33 * rf
                            self._phases[name] = (
                                self._phases.get(name, 0.0) + spd)

                            est_tk = ainfo["chars"] // 4
                            rate_s = ""
                            if ainfo["smoothed_rate"] > 0:
                                rate_s = (
                                    f"  {S.CYN}"
                                    f"{int(ainfo['smoothed_rate']):,}"
                                    f" tk/s{S.RST}")
                            live_c = self._live_cost_for_active(
                                name, ainfo["chars"])
                            cost_s = (f"  {S.HYEL}{format_cost(live_c)}{S.RST}"
                                      if live_c else "")
                            meta = f"{S.DIM}~{est_tk:,} tk{S.RST}"
                            time_s = f"  {S.DIM}{mel}{S.RST}"

                            pfx = 5 + 3 + self._pad + 2 + 2
                            suffix = f"{meta}{rate_s}{cost_s}{time_s}"
                            avail = inner_w - pfx - _vlen(suffix)
                            if avail < 5 and rate_s:
                                rate_s = ""
                                suffix = f"{meta}{cost_s}{time_s}"
                                avail = inner_w - pfx - _vlen(suffix)
                            if avail < 5 and cost_s:
                                cost_s = ""
                                suffix = f"{meta}{time_s}"
                                avail = inner_w - pfx - _vlen(suffix)
                            bw = max(5, min(BAR_WIDTH, avail))

                            bar = _render_pulse_bar(
                                ainfo["chars"], model_scale, idx,
                                self._phases[name], bw)
                            row = (f"{boxes}   "
                                   f"{_rpad(name, self._pad)}  "
                                   f"{bar}  {suffix}")
                    else:
                        boxes = self._phase_boxes(0)
                        row = (f"{boxes}   "
                               f"{_rpad(name, self._pad)}  "
                               f"{S.DIM}waiting…{S.RST}")

                    buf.append(_box_row(row, w) + "\033[K\n")
                    lines += 1
                    visible += 1

                if hidden > 0:
                    buf.append(_box_row(
                        f"{S.DIM}+{hidden} more…{S.RST}", w) + "\033[K\n")
                    lines += 1

                buf.append(_box_sep("", w) + "\033[K\n")
                lines += 1

                running_cost = 0.0
                has_cost = False
                for rn, ri in self._results.items():
                    c = self._model_cost(rn, ri)
                    if c is not None:
                        running_cost += c
                        has_cost = True
                for an, ai in self._active.items():
                    c = self._live_cost_for_active(an, ai["chars"])
                    if c is not None:
                        running_cost += c
                        has_cost = True
                cost_part = ""
                if has_cost:
                    cost_part = f" · {S.RST}{S.HYEL}{format_cost(running_cost)}{S.RST}{S.DIM}"

                summary = (
                    f"{S.HCYN}{frame}{S.RST} "
                    f"{self._label}  "
                    f"{S.DIM}{done}/{self._total} complete · "
                    f"{elapsed}{cost_part}{S.RST}")
                buf.append(_box_row(summary, w) + "\033[K\n")
                lines += 1

                buf.append(_box_bot(w) + "\033[K")
                lines += 1

                if self._entered_alt_screen:
                    gross_chars = self._wave_completed_chars + sum(
                        ai["chars"] for ai in self._active.values())
                    now = time.monotonic()
                    dt = now - self._wave_rate_time
                    if self._wave_rate_time <= 0.0:
                        self._wave_rate_time = now
                        self._wave_last_chars = gross_chars
                    elif dt >= 0.4:
                        delta = gross_chars - self._wave_last_chars
                        instant = (delta / 4.0) / dt
                        self._wave_agg_rate = (
                            0.12 * instant + 0.88 * self._wave_agg_rate)
                        self._wave_last_chars = gross_chars
                        self._wave_rate_time = now
                    gross_tokens = gross_chars / 4.0
                    rate_target = min(1.0, math.sqrt(
                        max(0.0, self._wave_agg_rate) / 1500.0))
                    volume_factor = min(
                        1.0, math.sqrt(gross_tokens / 20000.0))
                    target = min(1.0, max(rate_target, volume_factor)
                                 * 0.6 + volume_factor * 0.4)
                    self._wave_intensity += (
                        target - self._wave_intensity) * 0.04

                    wave_spd = ((0.35 + 0.75 * self._wave_intensity)
                                * (1.0 + 0.5 * volume_factor))
                    self._wave_phase += wave_spd

                    remaining = max(0, term.lines - lines)
                    if remaining > 0:
                        wave_w = max(10, term.columns - 2)
                        wave_frames = render_idle_wave(
                            idx, wave_w, remaining, self._wave_intensity,
                            wave_phase=self._wave_phase)
                        for wf in wave_frames:
                            buf.append(f"\n {wf}\033[K")
                    buf.append("\033[J")

                frame = "\r" + "".join(buf)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, self._flush_frame, frame)
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


def display_analytics(history: Dict[str, Any], compact: bool = False,
                      pad: int = 16, sort_by: str = "runs") -> None:
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
                               "cancel": 0, "times": [], "tokens": [],
                               "costs": []}
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
                c = res.get("cost")
                if c is not None and c > 0:
                    s["costs"].append(c)
            elif status == "cancelled":
                s["cancel"] += 1
            else:
                s["fail"] += 1

    def _sort_key(item: Any) -> Any:
        _, s = item
        rate = s["ok"] / s["runs"] if s["runs"] else 0
        avg_t = sum(s["times"]) / len(s["times"]) if s["times"] else float("inf")
        avg_tk = sum(s["tokens"]) / len(s["tokens"]) if s["tokens"] else 0
        total_cost = sum(s["costs"]) if s["costs"] else float("inf")
        if sort_by == "runs":
            return (-s["runs"], -rate, avg_t)
        elif sort_by == "avg_time":
            return (avg_t, -rate)
        elif sort_by == "rate":
            return (-rate, avg_t)
        elif sort_by == "avg_tokens":
            return (-avg_tk, -rate)
        elif sort_by == "cost":
            return (total_cost, -rate)
        return (-s["runs"], -rate, avg_t)

    ranked = sorted(stats.items(), key=_sort_key)

    n = len(runs)
    col = max((len(name) for name, _ in ranked), default=12) + 2
    col = max(col, pad)

    # ── Box header ─────────────────────────────────────────────────────────
    print()
    print(_box_top(f"Lifetime Analytics ({n} run{'s' if n != 1 else ''})", w))
    print(_box_row("", w))

    hdr = (f"{S.BOLD}{'MODEL':<{col}}{'RUNS':>5}  {'RATE':>5}"
           f"  {'AVG':>8}  {'AVG TKNS':>9}"
           f"  {'AVG COST':>9}  {'TOTAL':>9}{S.RST}")
    print(_box_row(hdr, w))
    print(_box_sep("", w))

    # ── Table rows (top 10 by usage, totals from all) ───────────────────────
    total_calls = total_ok = 0
    all_times: list[float] = []
    all_costs: list[float] = []

    MAX_DISPLAY = 10
    for idx, (name, s) in enumerate(ranked):
        total_calls += s["runs"]
        total_ok += s["ok"]
        all_times.extend(s["times"])
        all_costs.extend(s["costs"])

        if idx >= MAX_DISPLAY:
            continue

        rate = (s["ok"] / s["runs"] * 100) if s["runs"] else 0
        avg_v  = sum(s["times"]) / len(s["times"]) if s["times"] else None
        avg_tk = sum(s["tokens"]) / len(s["tokens"]) if s["tokens"] else None
        avg_cost = sum(s["costs"]) / len(s["costs"]) if s["costs"] else None
        total_cost = sum(s["costs"]) if s["costs"] else None

        rate_s = f"{rate:>4.0f}%"
        if rate >= 90:
            rate_c = f"{S.HGRN}{rate_s}{S.RST}"
        elif rate >= 60:
            rate_c = f"{S.HYEL}{rate_s}{S.RST}"
        else:
            rate_c = f"{S.HRED}{rate_s}{S.RST}"

        avg_tk_s = f"{int(avg_tk):,}" if avg_tk is not None else "—"
        avg_cost_s = format_cost(avg_cost) if avg_cost else "—"
        total_cost_s = format_cost(total_cost) if total_cost else "—"

        print(_box_row(
            f"{name:<{col}}{s['runs']:>5}  {rate_c}"
            f"  {S.CYN}{format_duration(avg_v):>8}{S.RST}"
            f"  {S.DIM}{avg_tk_s:>9}{S.RST}"
            f"  {S.YEL}{avg_cost_s:>9}{S.RST}"
            f"  {S.YEL}{total_cost_s:>9}{S.RST}", w))

    if len(ranked) > MAX_DISPLAY:
        hidden = len(ranked) - MAX_DISPLAY
        print(_box_row(
            f"{S.DIM}+{hidden} more model{'s' if hidden != 1 else ''}{S.RST}", w))

    overall = (total_ok / total_calls * 100) if total_calls else 0
    avg_all = format_duration(
        sum(all_times) / len(all_times) if all_times else None
    )
    all_tokens = [t for s in stats.values() for t in s["tokens"]]
    avg_tk_all = f"{int(sum(all_tokens) / len(all_tokens)):,}" if all_tokens else "—"
    total_spend = sum(all_costs) if all_costs else None
    avg_cost_all = (sum(all_costs) / len(all_costs)) if all_costs else None
    total_spend_s = format_cost(total_spend) if total_spend else "—"
    avg_cost_all_s = format_cost(avg_cost_all) if avg_cost_all else "—"

    print(_box_sep("Totals", w))
    print(_box_row(
        f"{total_calls} calls {_dot} {total_ok} passed {_dot} "
        f"{S.BOLD}{overall:.0f}%{S.RST} {_dot} avg {S.CYN}{avg_all}{S.RST} "
        f"{_dot} avg tkns {S.DIM}{avg_tk_all}{S.RST}", w))
    print(_box_row(
        f"avg cost {S.YEL}{avg_cost_all_s}{S.RST} {_dot} "
        f"total spend {S.BOLD}{S.YEL}{total_spend_s}{S.RST}", w))

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
