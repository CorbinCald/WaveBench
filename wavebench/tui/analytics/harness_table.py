"""Wrapped Harness summaries for both lifetime and post-benchmark analytics."""

from __future__ import annotations

import textwrap

from wavebench.harness.accounting import Measurement
from wavebench.tui.styles import S, _box_row, _box_sep, _truncate, format_cost, format_duration

from .harness import HarnessStats, Ratio


def _coverage(count: int, expected: int) -> str:
    return f" ({count}/{expected} runs)" if count and count < expected else ""


def _amount(value: Measurement, *, money: bool = False) -> str:
    if value.value is None:
        return "—"
    number = (format_cost(value.value) or "$0.000") if money else f"{value.value:,.0f}"
    return f"{value.prefix}{number}{value.suffix}"


def _ratio(value: Ratio, expected: int, *, speed: bool = False) -> str:
    if value.value is None:
        return "—"
    number = f"{value.value:,.1f} tk/s" if speed else f"{value.value:.1%}"
    return number + _coverage(value.count, expected)


def _rows(text: str, width: int, *, dim: bool = False) -> None:
    for line in textwrap.wrap(text, max(1, width - 4)):
        print(_box_row(f"{S.DIM}{line}{S.RST}" if dim else line, width))


def _summary(stats: HarnessStats, width: int, *, compact: bool = False) -> None:
    def total(key: str, *, money: bool = False, expected: int | None = None) -> str:
        return _amount(
            stats.totals[key].measurement(stats.runs if expected is None else expected), money=money
        )

    _rows(
        f"{stats.runs:,} attempt{'s' if stats.runs != 1 else ''} · {stats.passed:,} passed ({stats.passed / stats.runs:.1%})"
        f" · {stats.failed:,} failed · {stats.cancelled:,} cancelled",
        width,
    )
    _rows(
        f"Output {total('completion_tokens')} tk · speed {_ratio(stats.speed, stats.runs, speed=True)}"
        f" · cache hit {_ratio(stats.cache, stats.runs)}",
        width,
    )
    _rows(
        f"Tools {total('tools')} · tool fail {_ratio(stats.tool_failures, stats.runs)}"
        f" · searches {total('web_search')} · page reads {total('web_fetch')}",
        width,
    )
    _rows(
        f"Spend {total('cost', money=True)} · cost/pass {_amount(stats.cost_per_pass, money=True)}",
        width,
    )
    if compact:
        return

    turns = stats.totals["turns"]
    average_turns = f"{turns.mean:.1f}" if turns.mean is not None else "—"
    _rows(
        f"Input {total('prompt_tokens')} tk · total {total('total_tokens')} tk"
        f" · API turns {total('turns')} · avg turns {average_turns}{_coverage(turns.count, stats.runs)}",
        width,
    )
    unsuccessful = stats.failed + stats.cancelled
    if unsuccessful:
        _rows(
            f"Failed/cancelled spend {total('unsuccessful_cost', money=True, expected=unsuccessful)}",
            width,
        )

    p50, p95 = stats.latency()
    _rows(
        f"Passed active time: median {format_duration(p50)} · p95 {format_duration(p95)}"
        f" · {len(stats.successful_times):,}/{stats.passed:,} measured",
        width,
    )
    first_pass = (
        f"{stats.first_pass}/{stats.repair_known} ({stats.first_pass / stats.repair_known:.1%})"
        if stats.repair_known
        else "—"
    )
    repairs = f"{stats.repairs}/{stats.repair_known}" if stats.repair_known else "—"
    recovered = (
        f"{stats.recovered}/{stats.repairs} ({stats.recovered / stats.repairs:.1%})"
        if stats.repairs
        else "—"
    )
    _rows(
        f"First-pass rate {first_pass} · needed repair {repairs} · repair recovery {recovered}"
        + _coverage(stats.repair_known, stats.runs),
        width,
    )
    budget = stats.budget
    marker = "~" if budget.estimated else ""
    budget_mean = f"{marker}{budget.mean:.1%}" if budget.mean is not None else "—"
    near = f"{marker}{stats.near_budget}/{budget.count}" if budget.count else "—"
    _rows(
        f"Budget used: avg {budget_mean} · at least 90% used {near}"
        + _coverage(budget.count, stats.runs),
        width,
    )
    _rows(
        f"API retries {total('retries')} · compaction turns {total('compaction_turns')}"
        f" · compaction spend {total('compaction_cost', money=True)} (included above)",
        width,
    )
    phases = []
    for key, label in (
        ("generation_s", "build"),
        ("repair_s", "repair"),
        ("api_s", "API"),
        ("tool_s", "tools"),
        ("queued_s", "queue"),
        ("setup_s", "setup"),
        ("runtime_s", "runtime"),
        ("compaction_s", "compaction"),
    ):
        timing = stats.timing[key]
        phases.append(
            f"{label} {format_duration(timing.mean)}{_coverage(timing.count, stats.runs)}"
        )
    _rows("Avg phase time: " + " · ".join(phases), width)
    if stats.failures:
        _rows(
            "Failure causes: "
            + " · ".join(
                f"{label} {count:,} ({count / stats.failed:.1%})"
                for label, count in stats.failures.most_common()
            ),
            width,
        )


def display_harness_analytics(
    models: dict[str, HarnessStats],
    total: HarnessStats,
    order: list[str],
    width: int,
    *,
    compact: bool,
) -> None:
    if not total.runs:
        return
    print(_box_sep("Harness Totals", width))
    _rows(
        "All outcomes included. ≥ partial total; ~ estimate; — unknown. "
        "Rates use measured runs; partial coverage is shown in parentheses.",
        width,
        dim=True,
    )
    _summary(total, width, compact=compact)
    if compact:
        _rows(
            "wavebench --stats: per-model speed, costs, repairs, budgets and failure causes.",
            width,
            dim=True,
        )
        return
    _rows(
        "Pass = runtime/startup success, not project quality. Active time = build + repair. "
        "Phase times overlap; do not add them. Cost/pass includes failed and cancelled spend. "
        "Search service charges are separate. p95 uses nearest rank; small samples are noisy.",
        width,
        dim=True,
    )
    for name in order:
        print(_box_sep(_truncate(f"{name} [harness]", width - 6), width))
        _summary(models[name], width)
