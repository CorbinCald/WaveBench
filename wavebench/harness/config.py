"""Identical, recorded resource limits for every model in a benchmark."""

from dataclasses import asdict, dataclass, fields

# Settings from earlier Harness versions that no longer exist. Saved configs may
# still contain them; they are ignored rather than rejected.
RETIRED_SETTINGS = frozenset(
    {"total_tokens", "research_turns", "research_seconds", "research_tokens"}
)


@dataclass(frozen=True)
class Limits:
    build_turns: int = 50
    repair_turns: int = 20
    turn_tokens: int = 128_000
    build_seconds: int = 1800
    repair_seconds: int = 300
    process_seconds: int = 60
    startup_seconds: int = 20
    setup_seconds: int = 120
    lint_seconds: int = 30
    review_seconds: int = 600
    output_chars: int = 32_000
    read_chars: int = 100_000
    diagnostic_bytes: int = 8 * 1024 * 1024
    workspace_bytes: int = 512 * 1024 * 1024
    parallel_calls: int = 4
    batch_calls: int = 64
    process_concurrency: int = 4
    web_search_calls: int = 20
    web_fetch_calls: int = 20
    stream_raw_min_bytes: int = 16 * 1024 * 1024
    stream_raw_bytes_per_token: int = 1024
    stream_raw_max_bytes: int = 128 * 1024 * 1024
    stream_output_min_bytes: int = 1024 * 1024
    stream_output_bytes_per_token: int = 64
    stream_output_max_bytes: int = 32 * 1024 * 1024
    stream_frame_bytes: int = 2 * 1024 * 1024
    stream_assembly_bytes: int = 32 * 1024 * 1024
    response_headers_seconds: int = 60
    stream_seconds: int = 1800
    stream_idle_seconds: int = 60
    subagent_parallel: int = 4
    subagent_cap: int = 8
    subagent_turns: int = 20
    subagent_seconds: int = 600
    subagent_report_chars: int = 6_000

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value < 1:
                raise ValueError(f"harness.{field.name} must be a positive integer")
        if not 2 <= self.subagent_parallel <= 5:
            raise ValueError("harness.subagent_parallel must be between 2 and 5")
        for category in ("raw", "output"):
            if getattr(self, f"stream_{category}_min_bytes") > getattr(
                self, f"stream_{category}_max_bytes"
            ):
                raise ValueError(f"harness.stream_{category}_min_bytes exceeds its maximum")

    @classmethod
    def from_config(cls, config: dict):
        settings = config.get("harness") or {}
        return cls(**{k: v for k, v in settings.items() if k not in RETIRED_SETTINGS})

    def record(self) -> dict:
        return asdict(self)
