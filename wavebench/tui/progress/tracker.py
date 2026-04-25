"""``ProgressTracker`` — multi-line animated progress display.

Owns the alternate-screen lifecycle, the per-model row animation, the news
ticker on the summary line, the ``print()`` hook that clears drawn lines
before any user output lands, and the final Run Results box rendered on
``stop()``.

Streaming character counts come in via ``register`` / ``update`` /
``unregister``; parsing-phase state via ``mark_parsing`` / ``finish_parsing``.
The animation loop in ``_animate`` reads that state once per frame (~80 ms).
"""

from __future__ import annotations

import asyncio
import math
import shutil
import sys
import time
from typing import Any

try:
    import termios
except ImportError:
    termios = None  # type: ignore[assignment]

from wavebench.tui import styles as _styles
from wavebench.tui.analytics.cost import compute_cost
from wavebench.tui.progress.wave import (
    BAR_WIDTH,
    _render_pre_wave_bar,
    _render_pulse_bar,
    _title_wave,
    render_idle_wave,
)
from wavebench.tui.styles import (
    _SPIN,
    PHASE_GRADIENT,
    S,
    _arrow,
    _box_bot,
    _box_row,
    _box_sep,
    _box_top,
    _dot,
    _fail,
    _ok,
    _rpad,
    _skip,
    _truncate,
    _tw,
    _vlen,
    format_cost,
    format_duration,
)


class ProgressTracker:
    """Multi-line animated progress display with live token bars.

    While active every ``print()`` is intercepted: the drawn progress
    section is cleared first so output never overlaps the animation.

    Models register / unregister themselves and push character-count
    updates; the animation loop renders a green bar for each active
    model plus a summary spinner line at the bottom.
    """

    DEFAULT_AVG_TOKENS = 2000

    def __init__(
        self,
        total: int,
        results: dict[str, Any],
        pad: int = 16,
        label: str = "Generating",
        model_names: list | None = None,
        avg_tokens: dict[str, float] | None = None,
        pricing_lookup: dict[str, Any] | None = None,
        model_id_map: dict[str, str] | None = None,
        alt_screen: bool = False,
    ):
        self._total = total
        self._results = results
        self._label = label
        self._pad = pad
        self._running = False
        self._task: asyncio.Task | None = None
        self._start = time.monotonic()
        self._is_tty = sys.stdout.isatty()
        self._original_print = None
        self._active: dict[str, dict[str, Any]] = {}
        self._parsing: dict[str, dict[str, Any]] = {}
        self._phases: dict[str, float] = {}
        # Live throttle/retry state per model. Cleared once the retry's
        # `until` deadline passes; total count survives so the active-row
        # render can show "(2 retries)" cumulatively.
        self._retries: dict[str, dict[str, Any]] = {}
        self._drawn_lines = 0
        self._model_names = model_names or []
        self._avg_tokens = avg_tokens or {}
        self._output_dir: str | None = None
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
        # News-ticker marquee rendered on the summary line — joined text
        # scrolls once right-to-left, then the strip renders empty.
        self._ticker_text: str = ""
        self._ticker_started_at: float = 0.0
        self._ticker_speed_cps: float = 12.0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def rendered_final(self) -> bool:
        return self._rendered

    def set_output_dir(self, path: str) -> None:
        self._output_dir = path

    def set_ticker(self, messages: list) -> None:
        """Attach a one-pass news-scroll ticker of short plain-text
        messages to the right of the summary spinner line.  Joined with a
        bullet separator and scrolled right-to-left once; after fully
        exiting the left edge the ticker strip renders empty.
        """
        parts = [str(m).strip() for m in messages if m and str(m).strip()]
        if not parts:
            self._ticker_text = ""
            return
        self._ticker_text = "   •   ".join(parts)
        self._ticker_started_at = 0.0

    def _ticker_slice(self, width: int) -> str:
        """Return a *width*-wide slice of the scrolling ticker (plain
        text, no ANSI).  Text enters from the right edge, advances one
        char per (1 / speed) seconds, exits on the left, then blanks.
        """
        if not self._ticker_text or width <= 0:
            return " " * max(0, width)
        if self._ticker_started_at == 0.0:
            self._ticker_started_at = time.monotonic()
        elapsed = time.monotonic() - self._ticker_started_at
        # Left edge of text starts at +width (off-screen right) and
        # decreases linearly with elapsed time.
        offset = width - int(elapsed * self._ticker_speed_cps)
        text_len = len(self._ticker_text)
        if offset <= -text_len:
            return " " * width
        strip = [" "] * width
        for i, ch in enumerate(self._ticker_text):
            col = offset + i
            if 0 <= col < width:
                strip[col] = ch
        return "".join(strip)

    def _model_pricing(self, model_name: str) -> dict[str, Any]:
        """Return the pricing dict for a model, or empty dict."""
        mid = self._model_id_map.get(model_name, "")
        return self._pricing_lookup.get(mid, {})

    def _model_cost(self, model_name: str, info: dict[str, Any]) -> float | None:
        """Compute cost for a completed model result."""
        return compute_cost(info.get("usage", {}), self._model_pricing(model_name))

    def _total_cost(self) -> float | None:
        """Sum costs across all completed results that have pricing."""
        total = 0.0
        any_cost = False
        for name, info in self._results.items():
            c = self._model_cost(name, info)
            if c is not None:
                total += c
                any_cost = True
        return total if any_cost else None

    def _live_cost_for_active(self, model_name: str, chars: int) -> float | None:
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
            "chars": 0,
            "start": time.monotonic(),
            "last_chars": 0,
            "last_rate_time": 0.0,
            "smoothed_rate": 0.0,
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

    def note_retry(
        self,
        model_name: str,
        status: int,
        attempt: int,
        max_attempts: int,
        wait_s: float,
    ) -> None:
        """Record a transient-failure retry so the active row can surface it.

        ``until`` lets the renderer auto-clear the "throttled" line once
        the backoff sleep has elapsed; ``count`` survives the retry so the
        total appears next to the model name even after recovery.
        """
        prev = self._retries.get(model_name, {})
        self._retries[model_name] = {
            "status": status,
            "attempt": attempt,
            "max": max_attempts,
            "until": time.monotonic() + wait_s,
            "count": prev.get("count", 0) + 1,
        }

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
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, self._saved_termios)
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
        on = f"{color}{'▪' * filled}{S.RST}" if filled else ""
        off = f"{S.DIM}{'▫' * (5 - filled)}{S.RST}" if filled < 5 else ""
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
        """Write a rendered frame to stdout atomically.

        Writes directly to the binary buffer to bypass TextIOWrapper's
        line-buffered flushing, preventing the terminal from rendering
        partial frames between newlines.  Called via ``run_in_executor``
        so that PTY back-pressure blocks the worker thread instead of
        the asyncio event loop.
        """
        data = frame.encode()
        try:
            sys.stdout.flush()
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (AttributeError, OSError):
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

    def _format_output_dir(self, inner_w: int) -> str | None:
        if not self._output_dir:
            return None
        out = self._output_dir
        max_path = inner_w - 10
        if len(out) > max_path:
            out = "…" + out[-(max_path - 1) :]
        return f"{S.DIM}{'OUTPUT':>8}  {out}{S.RST}"

    def _format_result_row(self, name: str, info: dict[str, Any], rank: int, inner_w: int) -> str:
        st = info.get("status", "failed")
        t = format_duration(info.get("time_s", 0))
        cost = self._model_cost(name, info)
        cost_s = f"  {S.HYEL}{format_cost(cost)}{S.RST}" if cost else ""
        retries = info.get("retries") or []
        retry_s = ""
        if retries:
            statuses = sorted({r.get("status", "?") for r in retries})
            status_part = "/".join(str(s) for s in statuses)
            label = f"{len(retries)} retry" if len(retries) == 1 else f"{len(retries)} retries"
            retry_s = f"  {S.YEL}({label} on {status_part}){S.RST}"
        if st == "success":
            sym = _ok
            usage = info.get("usage", {})
            tokens = usage.get("total_tokens")
            fname = info.get("file", "")
            tk_part = f"  {S.DIM}{tokens:,} tk{S.RST}" if tokens else ""
            detail = f"saved {_arrow} {S.GRN}{fname}{S.RST}{tk_part}{cost_s}{retry_s}"
        elif st == "cancelled":
            sym = _skip
            detail = f"{S.DIM}cancelled{S.RST}{retry_s}"
            fname = ""
        else:
            sym = _fail
            detail = f"{S.RED}failed{S.RST}{cost_s}{retry_s}"
            fname = ""
        rank_s = f"{S.DIM}{rank:>2}.{S.RST}"
        content = f"{rank_s} {sym} {_rpad(name, self._pad)}  {detail}"
        if st == "success" and _vlen(content) + 2 + len(t) > inner_w:
            overflow = _vlen(content) + 2 + len(t) - inner_w
            max_fname = max(8, len(fname) - overflow)
            fname = _truncate(fname, max_fname)
            detail = f"saved {_arrow} {S.GRN}{fname}{S.RST}{tk_part}{cost_s}{retry_s}"
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
            return (order.get(v.get("status", "failed"), 3), v.get("time_s", 0))

        for i, (name, info) in enumerate(sorted(self._results.items(), key=_rank_key), 1):
            buf.append(_box_row(self._format_result_row(name, info, i, inner_w), w))

        buf.append(_box_sep("", w))
        ok = sum(1 for v in self._results.values() if v.get("status") == "success")
        fail = sum(1 for v in self._results.values() if v.get("status") == "failed")
        canc = sum(1 for v in self._results.values() if v.get("status") == "cancelled")
        parts: list[str] = []
        if ok:
            parts.append(f"{S.HGRN}{ok} passed{S.RST}")
        if fail:
            parts.append(f"{S.HRED}{fail} failed{S.RST}")
        if canc:
            parts.append(f"{S.DIM}{canc} cancelled{S.RST}")
        parts.append(f"{format_duration(total_time)} total")
        total_cost = self._total_cost()
        if total_cost is not None:
            parts.append(f"{S.HYEL}{format_cost(total_cost)}{S.RST}")
        sep = f" {_dot} "
        buf.append(_box_row(sep.join(parts), w))
        buf.append(_box_bot(w))

        sys.stdout.write("\n".join(buf) + "\n")
        self._rendered = True

    async def _animate(self) -> None:
        idx = 0
        try:
            while self._running:
                if self._entered_alt_screen:
                    _clear_seq = "\033[H"
                elif self._drawn_lines > 0:
                    _up = f"\033[{self._drawn_lines - 1}A" if self._drawn_lines > 1 else ""
                    _clear_seq = f"{_up}\r\033[J"
                else:
                    _clear_seq = "\r"
                self._drawn_lines = 0

                term = shutil.get_terminal_size((80, 24))
                w = max(20, min(120, term.columns)) - 4
                inner_w = w - 4
                done = len(self._results)
                elapsed = format_duration(time.monotonic() - self._start)
                frame = _SPIN[idx % len(_SPIN)]

                buf: list[str] = []
                lines = 0

                wave = _title_wave(idx)
                buf.append(_box_top(f"Generating \033[22m{wave}", w) + "\033[K\n")
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
                            name, self._results[name], completed_idx, inner_w
                        )
                    elif name in self._parsing:
                        pinfo = self._parsing[name]
                        mel = format_duration(time.monotonic() - pinfo["start"])
                        dots = "·" * (1 + (idx // 4) % 3)
                        boxes = self._token_boxes(name, pinfo.get("chars", 0))
                        row = (
                            f"{boxes}   "
                            f"{_rpad(name, self._pad)}  "
                            f"{S.DIM}parsing{dots:<4}"
                            f"  {mel:>7}{S.RST}"
                        )
                    elif name in self._active:
                        ainfo = self._active[name]
                        mel = format_duration(time.monotonic() - ainfo["start"])
                        rinfo = self._retries.get(name)
                        now_t = time.monotonic()
                        if rinfo and rinfo["until"] > now_t:
                            # Throttle override: replace bar/suffix with a
                            # one-line throttle status until the backoff
                            # sleep elapses, then resume the normal render.
                            remain = max(0.0, rinfo["until"] - now_t)
                            boxes = self._token_boxes(name, ainfo.get("chars", 0))
                            suffix = (
                                f"{S.YEL}HTTP {rinfo['status']}{S.RST}  "
                                f"{S.DIM}retry {rinfo['attempt']}/{rinfo['max']}"
                                f" in {remain:.1f}s  {mel:>7}{S.RST}"
                            )
                            row = f"{boxes}   {_rpad(name, self._pad)}  {suffix}"
                            buf.append(_box_row(row, w) + "\033[K\n")
                            lines += 1
                            visible += 1
                            continue
                        if ainfo["chars"] == 0:
                            boxes = self._token_boxes(name, 0)
                            dots = "·" * (1 + (idx // 4) % 3)
                            suffix = f"{S.DIM}reasoning{dots:<4} {mel:>7}{S.RST}"
                            pfx = 5 + 3 + self._pad + 2 + 2
                            bw = max(5, min(BAR_WIDTH, inner_w - pfx - _vlen(suffix)))
                            bar = _render_pre_wave_bar(bw, idx)
                            row = f"{boxes}   {_rpad(name, self._pad)}  {bar}  {suffix}"
                        else:
                            now = time.monotonic()
                            dt = now - ainfo["last_rate_time"]
                            if dt >= 0.5:
                                if ainfo["last_rate_time"] == 0.0:
                                    ainfo["last_chars"] = ainfo["chars"]
                                    ainfo["last_rate_time"] = now
                                else:
                                    d_chars = ainfo["chars"] - ainfo["last_chars"]
                                    instant_tks = (d_chars / 4) / dt
                                    if ainfo["smoothed_rate"] <= 0:
                                        ainfo["smoothed_rate"] = instant_tks
                                    else:
                                        ainfo["smoothed_rate"] = (
                                            0.3 * instant_tks + 0.7 * ainfo["smoothed_rate"]
                                        )
                                    ainfo["last_chars"] = ainfo["chars"]
                                    ainfo["last_rate_time"] = now

                            boxes = self._token_boxes(name, ainfo["chars"])
                            avg_tk = self._avg_tokens.get(name, self.DEFAULT_AVG_TOKENS)
                            model_scale = avg_tk * 4

                            rate = ainfo["smoothed_rate"]
                            rf = min(1.0, math.sqrt(max(0.0, rate) / 200.0))
                            spd = 0.12 + 0.33 * rf
                            self._phases[name] = self._phases.get(name, 0.0) + spd

                            est_tk = ainfo["chars"] // 4
                            rate_s = ""
                            if ainfo["smoothed_rate"] > 0:
                                rate_s = (
                                    f"  {_styles.ACCENT}{int(ainfo['smoothed_rate']):,} tk/s{S.RST}"
                                )
                            live_c = self._live_cost_for_active(name, ainfo["chars"])
                            cost_s = f"  {S.HYEL}{format_cost(live_c)}{S.RST}" if live_c else ""
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
                                ainfo["chars"], model_scale, idx, self._phases[name], bw
                            )
                            row = f"{boxes}   {_rpad(name, self._pad)}  {bar}  {suffix}"
                    else:
                        boxes = self._phase_boxes(0)
                        row = f"{boxes}   {_rpad(name, self._pad)}  {S.DIM}waiting…{S.RST}"

                    buf.append(_box_row(row, w) + "\033[K\n")
                    lines += 1
                    visible += 1

                if hidden > 0:
                    buf.append(_box_row(f"{S.DIM}+{hidden} more…{S.RST}", w) + "\033[K\n")
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
                    f"{_styles.ACCENT_HI}{frame}{S.RST} "
                    f"{self._label}  "
                    f"{S.DIM}{done}/{self._total} complete · "
                    f"{elapsed}{cost_part}{S.RST}"
                )
                if self._ticker_text:
                    inner_w = w - 4
                    gap = 3
                    ticker_w = inner_w - _vlen(summary) - gap
                    if ticker_w > 0:
                        strip = self._ticker_slice(ticker_w)
                        if strip.strip():
                            summary = summary + " " * gap + f"{S.DIM}{strip}{S.RST}"
                buf.append(_box_row(summary, w) + "\033[K\n")
                lines += 1

                buf.append(_box_bot(w) + "\033[K")
                lines += 1

                if self._entered_alt_screen:
                    gross_chars = self._wave_completed_chars + sum(
                        ai["chars"] for ai in self._active.values()
                    )
                    now = time.monotonic()
                    dt = now - self._wave_rate_time
                    if self._wave_rate_time <= 0.0:
                        self._wave_rate_time = now
                        self._wave_last_chars = gross_chars
                    elif dt >= 0.4:
                        delta = gross_chars - self._wave_last_chars
                        instant = (delta / 4.0) / dt
                        self._wave_agg_rate = 0.12 * instant + 0.88 * self._wave_agg_rate
                        self._wave_last_chars = gross_chars
                        self._wave_rate_time = now
                    gross_tokens = gross_chars / 4.0
                    rate_target = min(1.0, math.sqrt(max(0.0, self._wave_agg_rate) / 1500.0))
                    volume_factor = min(1.0, math.sqrt(gross_tokens / 20000.0))
                    target = min(1.0, max(rate_target, volume_factor) * 0.6 + volume_factor * 0.4)
                    self._wave_intensity += (target - self._wave_intensity) * 0.04

                    wave_spd = (0.35 + 0.75 * self._wave_intensity) * (1.0 + 0.5 * volume_factor)
                    self._wave_phase += wave_spd

                    remaining = max(0, term.lines - lines)
                    if remaining > 0:
                        wave_w = max(10, term.columns - 2)
                        wave_frames = render_idle_wave(
                            idx,
                            wave_w,
                            remaining,
                            self._wave_intensity,
                            wave_phase=self._wave_phase,
                        )
                        for wf in wave_frames:
                            buf.append(f"\n {wf}\033[K")
                    buf.append("\033[J")

                frame = f"\033[?2026h{_clear_seq}" + "".join(buf) + "\033[?2026l"
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._flush_frame, frame)
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
