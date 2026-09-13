"""Lifetime Harness measurements, weighted by their observed denominators.

Only saved results are read. Missing measurements never become zero, and
known portions of incomplete usage remain lower bounds. No turn logs or
generated projects need to be opened to build the report.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import median

from wavebench.harness.accounting import Measurement, reported_total, valid_number
from wavebench.harness.failure import budget_record, result_failure


@dataclass
class Total:
    value: float = 0
    count: int = 0
    partial: bool = False
    estimated: bool = False

    def add(self, value, *, partial: bool = False, estimated: bool = False) -> None:
        if valid_number(value):
            self.value += value
            self.count += 1
            self.partial |= partial
            self.estimated |= estimated

    def measurement(self, expected: int) -> Measurement:
        return Measurement(
            self.value if self.count else None,
            estimated=self.estimated,
            incomplete=self.partial or self.count < expected,
        )

    @property
    def mean(self) -> float | None:
        return self.value / self.count if self.count else None


@dataclass
class Ratio:
    numerator: float = 0
    denominator: float = 0
    count: int = 0

    def add(self, numerator, denominator, *, bounded: bool = True) -> None:
        if not (valid_number(numerator) and valid_number(denominator)):
            return
        if denominator <= 0 or (bounded and numerator > denominator):
            return
        self.numerator += numerator
        self.denominator += denominator
        self.count += 1

    @property
    def value(self) -> float | None:
        return self.numerator / self.denominator if self.denominator else None


def usage_measurement(result: dict, key: str) -> Measurement:
    """Prefer complete saved accounting, falling back to known partial usage."""
    usage = result.get("usage") or {}
    value = usage.get(key)
    if key == "cost" and valid_number(result.get("cost")):
        value = result["cost"]
    elif key == "total_tokens":
        value = reported_total(usage)
    if valid_number(value):
        return Measurement(value)
    known = usage.get(f"known_{key}")
    return Measurement(known if valid_number(known) else None, incomplete=True)


FAILURE_LABELS = {
    "token_budget": "Token budget",
    "harness_limit": "Time / turn limit",
    "stream_limit": "Stream limit",
    "request_timeout": "Request timeout",
    "model_protocol": "Model / protocol",
    "project_runtime": "Project runtime",
    "environment": "Environment",
    "unknown": "Unknown",
}


@dataclass
class HarnessStats:
    runs: int = 0
    passed: int = 0
    failed: int = 0
    cancelled: int = 0
    totals: dict[str, Total] = field(default_factory=lambda: defaultdict(Total))
    timing: dict[str, Total] = field(default_factory=lambda: defaultdict(Total))
    speed: Ratio = field(default_factory=Ratio)
    cache: Ratio = field(default_factory=Ratio)
    tool_failures: Ratio = field(default_factory=Ratio)
    successful_times: list[float] = field(default_factory=list)
    repair_known: int = 0
    repairs: int = 0
    recovered: int = 0
    first_pass: int = 0
    budget: Total = field(default_factory=Total)
    near_budget: int = 0
    failures: Counter = field(default_factory=Counter)

    def add(self, result: dict) -> None:
        harness = result["harness"]
        usage = result.get("usage") or {}
        timing = harness.get("timing") or {}
        status = result.get("status", "failed")
        self.runs += 1
        if status == "success":
            self.passed += 1
            if valid_number(result.get("time_s")):
                self.successful_times.append(result["time_s"])
        elif status == "cancelled":
            self.cancelled += 1
        else:
            self.failed += 1
            category = (result_failure(result) or {}).get("category", "unknown")
            self.failures[FAILURE_LABELS.get(category, "Unknown")] += 1

        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost"):
            measured = usage_measurement(result, key)
            self.totals[key].add(measured.value, partial=measured.incomplete)
            if key == "cost" and status != "success":
                self.totals["unsuccessful_cost"].add(measured.value, partial=measured.incomplete)

        turns = usage.get("api_turns")
        if not valid_number(turns) and isinstance(harness.get("turns"), list):
            turns = len(harness["turns"])
        self.totals["turns"].add(turns)
        self.speed.add(usage.get("completion_tokens"), timing.get("api_s"), bounded=False)
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        self.cache.add(cached, usage.get("prompt_tokens"))
        tools = harness.get("tool_usage") or {}
        self.totals["tools"].add(tools.get("calls"))
        self.tool_failures.add(tools.get("failures"), tools.get("calls"))
        for key in ("web_search", "web_fetch"):
            web = harness.get(key) or {}
            self.totals[key].add(0 if web.get("enabled") is False else web.get("calls"))

        repair = harness.get("repair")
        if repair in {
            "not_needed",
            "repairing",
            "submitted",
            "cancelled",
            "budget_exhausted",
            "abandoned",
        }:
            self.repair_known += 1
            repaired = repair != "not_needed"
            self.repairs += repaired
            self.recovered += repaired and status == "success"
            self.first_pass += not repaired and status == "success"

        budget = budget_record(harness)
        used, limit = budget.get("used_tokens"), budget.get("limit_tokens")
        if valid_number(used) and valid_number(limit) and limit > 0:
            self.budget.add(used / limit, estimated=bool(budget.get("estimated")))
            self.near_budget += used / limit >= 0.9

        for key in (
            "api_s",
            "tool_s",
            "generation_s",
            "repair_s",
            "queued_s",
            "setup_s",
            "runtime_s",
            "compaction_s",
        ):
            self.timing[key].add(timing.get(key))
        retries = result.get("retries")
        if isinstance(retries, list):
            self.totals["retries"].add(len(retries))
        compaction = (harness.get("compaction") or {}).get("usage") or {}
        self.totals["compaction_turns"].add(compaction.get("api_turns"))
        if compaction.get("api_turns") == 0:
            self.totals["compaction_cost"].add(0)
        else:
            cost = usage_measurement({"usage": compaction}, "cost")
            self.totals["compaction_cost"].add(cost.value, partial=cost.incomplete)

    @property
    def cost_per_pass(self) -> Measurement:
        spend = self.totals["cost"].measurement(self.runs)
        return Measurement(
            spend.value / self.passed if spend.value is not None and self.passed else None,
            incomplete=spend.incomplete,
        )

    def latency(self) -> tuple[float | None, float | None]:
        """Median and nearest-rank p95 of successful active build + repair time."""
        if not self.successful_times:
            return None, None
        times = sorted(self.successful_times)
        return median(times), times[math.ceil(len(times) * 0.95) - 1]


def aggregate_harness(history: dict) -> tuple[dict[str, HarnessStats], HarnessStats]:
    models: dict[str, HarnessStats] = {}
    total = HarnessStats()
    for run in history.get("runs", []):
        for name, result in run.get("models", {}).items():
            if result.get("harness"):
                if name not in models:
                    models[name] = HarnessStats()
                models[name].add(result)
                total.add(result)
    return models, total
