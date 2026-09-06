"""Identical, recorded resource limits for every model in a benchmark."""

from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class Limits:
    build_turns: int = 32
    repair_turns: int = 12
    total_tokens: int = 256_000
    turn_tokens: int = 16_384
    build_seconds: int = 900
    repair_seconds: int = 300
    process_seconds: int = 60
    startup_seconds: int = 20
    setup_seconds: int = 120
    lint_seconds: int = 30
    review_seconds: int = 600
    output_chars: int = 16_000
    diagnostic_bytes: int = 8 * 1024 * 1024
    workspace_bytes: int = 512 * 1024 * 1024
    parallel_calls: int = 4
    batch_calls: int = 64
    process_concurrency: int = 4

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value < 1:
                raise ValueError(f"harness.{field.name} must be a positive integer")

    @classmethod
    def from_config(cls, config: dict):
        return cls(**(config.get("harness") or {}))

    def record(self) -> dict:
        return asdict(self)
