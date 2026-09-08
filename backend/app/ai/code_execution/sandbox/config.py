"""Sandbox configuration, read from the environment.

All knobs are process-level (ops-facing), not per-organization: they describe
what the host can enforce, which is a deployment property.

    BOW_CODE_SANDBOX              subprocess (default) | inprocess
                                  `inprocess` is the pre-sandbox behavior
                                  (exec() inside the API worker). Debug only.
    BOW_SANDBOX_TIMEOUT_SECONDS   wall-clock budget per execution; the child
                                  is SIGKILLed when it is exceeded. Default 600.
    BOW_SANDBOX_MEMORY_MB         RLIMIT_AS for the child. 0 disables.
                                  Default 4096.
    BOW_SANDBOX_CPU_SECONDS       RLIMIT_CPU for the child. 0 disables.
                                  Default = timeout.
    BOW_SANDBOX_REQUIRE_LANDLOCK  1 → fail executions when the kernel cannot
                                  apply Landlock (default 0: log a warning
                                  once and continue with rlimits + env scrub).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


MODE_SUBPROCESS = "subprocess"
MODE_INPROCESS = "inprocess"

DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MEMORY_MB = 4096


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(minimum, int(str(raw).strip()))
    except ValueError:
        return default


def sandbox_mode() -> str:
    raw = (os.environ.get("BOW_CODE_SANDBOX") or MODE_SUBPROCESS).strip().lower()
    return MODE_INPROCESS if raw == MODE_INPROCESS else MODE_SUBPROCESS


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: int
    memory_mb: int
    cpu_seconds: int
    require_landlock: bool

    @classmethod
    def from_env(cls) -> "SandboxLimits":
        timeout = _int_env("BOW_SANDBOX_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, minimum=1)
        return cls(
            timeout_seconds=timeout,
            memory_mb=_int_env("BOW_SANDBOX_MEMORY_MB", DEFAULT_MEMORY_MB),
            cpu_seconds=_int_env("BOW_SANDBOX_CPU_SECONDS", timeout),
            require_landlock=os.environ.get("BOW_SANDBOX_REQUIRE_LANDLOCK", "").strip() == "1",
        )

    def as_dict(self) -> dict:
        return {
            "timeout_seconds": self.timeout_seconds,
            "memory_mb": self.memory_mb,
            "cpu_seconds": self.cpu_seconds,
            "require_landlock": self.require_landlock,
        }
