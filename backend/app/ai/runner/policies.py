from dataclasses import dataclass


@dataclass
class RetryPolicy:
    max_attempts: int = 2
    backoff_ms: int = 500
    backoff_multiplier: float = 2.0
    jitter_ms: int = 200
    retry_on: tuple[str, ...] = ("runtime_error", "timeout_error", "rate_limit")


@dataclass
class TimeoutPolicy:
    start_timeout_s: int = 5
    idle_timeout_s: int = 30
    hard_timeout_s: int = 90

