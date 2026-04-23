"""``display_analytics`` — renders the lifetime leaderboard to stdout.

Aggregates per-model stats across every recorded run, sorts by the
selected criterion, prints a ranked table with pass rate, average time,
token usage, and cost, followed by a totals row and (in full mode) a
"recent prompts" tail.

Read-only with respect to ``history``; writes only to stdout via
``print()``. The sort criterion is a string from the set
``{"runs", "avg_time", "rate", "avg_tokens", "cost"}``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from wavebench.tui import styles as _styles
from wavebench.tui.styles import (
    S,
    _box_bot,
    _box_row,
    _box_sep,
    _box_top,
    _dot,
    _truncate,
    _tw,
    format_cost,
    format_duration,
)


def display_analytics(
    history: dict[str, Any], compact: bool = False, pad: int = 16, sort_by: str = "runs"
) -> None:
    """Print lifetime model performance analytics."""
    runs = history.get("runs", [])
    if not runs:
        print(f"  {S.DIM}No history yet. Complete a run to begin tracking.{S.RST}")
        return

    w = _tw() - 4
    inner = w - 4

    # ── Aggregate per-model stats ──────────────────────────────────────────
    stats: dict[str, Any] = {}
    for run in runs:
        for name, res in run.get("models", {}).items():
            if name not in stats:
                stats[name] = {
                    "runs": 0,
                    "ok": 0,
                    "fail": 0,
                    "cancel": 0,
                    "times": [],
                    "tokens": [],
                    "costs": [],
                }
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

    hdr = (
        f"{S.BOLD}{'MODEL':<{col}}{'RUNS':>5}  {'RATE':>5}"
        f"  {'AVG':>8}  {'AVG TKNS':>9}"
        f"  {'AVG COST':>9}  {'TOTAL':>9}{S.RST}"
    )
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
        avg_v = sum(s["times"]) / len(s["times"]) if s["times"] else None
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

        print(
            _box_row(
                f"{name:<{col}}{s['runs']:>5}  {rate_c}"
                f"  {_styles.ACCENT}{format_duration(avg_v):>8}{S.RST}"
                f"  {S.DIM}{avg_tk_s:>9}{S.RST}"
                f"  {S.YEL}{avg_cost_s:>9}{S.RST}"
                f"  {S.YEL}{total_cost_s:>9}{S.RST}",
                w,
            )
        )

    if len(ranked) > MAX_DISPLAY:
        hidden = len(ranked) - MAX_DISPLAY
        print(_box_row(f"{S.DIM}+{hidden} more model{'s' if hidden != 1 else ''}{S.RST}", w))

    overall = (total_ok / total_calls * 100) if total_calls else 0
    avg_all = format_duration(sum(all_times) / len(all_times) if all_times else None)
    all_tokens = [t for s in stats.values() for t in s["tokens"]]
    avg_tk_all = f"{int(sum(all_tokens) / len(all_tokens)):,}" if all_tokens else "—"
    total_spend = sum(all_costs) if all_costs else None
    avg_cost_all = (sum(all_costs) / len(all_costs)) if all_costs else None
    total_spend_s = format_cost(total_spend) if total_spend else "—"
    avg_cost_all_s = format_cost(avg_cost_all) if avg_cost_all else "—"

    print(_box_sep("Totals", w))
    print(
        _box_row(
            f"{total_calls} calls {_dot} {total_ok} passed {_dot} "
            f"{S.BOLD}{overall:.0f}%{S.RST} {_dot} avg {_styles.ACCENT}{avg_all}{S.RST} "
            f"{_dot} avg tkns {S.DIM}{avg_tk_all}{S.RST}",
            w,
        )
    )
    print(
        _box_row(
            f"avg cost {S.YEL}{avg_cost_all_s}{S.RST} {_dot} "
            f"total spend {S.BOLD}{S.YEL}{total_spend_s}{S.RST}",
            w,
        )
    )

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
            ok = sum(1 for r in models.values() if r.get("status") == "success")
            tot = len(models)
            prompt = _truncate(run.get("prompt", "—"), inner - 20)
            print(_box_row(f"{S.DIM}{date_s}{S.RST}  {ok}/{tot}  {prompt}", w))

    print(_box_bot(w))
