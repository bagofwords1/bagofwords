"""Env-gated phase tracing for load/perf investigation.

Answers the question ordinary latency metrics cannot: when a completion takes
90s under load, *which part* took the time — waiting for an agent slot, waiting
for a DB connection, or actually running the agent loop.

Disabled unless ``BOW_PHASE_TRACE`` names a target file. When disabled every
entry point is a single module-level boolean check and returns immediately, so
this is safe to leave in the code path.

One JSON object per line:
    {"t": 1785..., "pid": 123, "cid": "<completion id>", "phase": "sem_acquired",
     "dt_ms": 12043.2, "extra": {...}}

``dt_ms`` is measured from the completion's own start marker, so records are
directly comparable across concurrency levels and across workers.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

_PATH = os.getenv("BOW_PHASE_TRACE", "")
ENABLED = bool(_PATH)

# completion id -> monotonic start. Bounded below so a long-running process
# cannot accumulate entries for completions that never emitted an end marker.
_starts: dict[str, float] = {}
_MAX_TRACKED = 5000


def start(cid: str) -> None:
    if not ENABLED or not cid:
        return
    if len(_starts) > _MAX_TRACKED:
        _starts.clear()
    _starts[cid] = time.monotonic()
    mark(cid, "start")


def mark(cid: str, phase: str, **extra: Any) -> None:
    """Record a phase transition. Never raises — tracing must not break a run."""
    if not ENABLED or not cid:
        return
    try:
        t0 = _starts.get(cid)
        dt = (time.monotonic() - t0) * 1000.0 if t0 is not None else None
        rec = {
            "t": time.time(),
            "pid": os.getpid(),
            "cid": str(cid),
            "phase": phase,
            "dt_ms": round(dt, 1) if dt is not None else None,
        }
        if extra:
            rec["extra"] = extra
        # Line-buffered append. Each record is well under PIPE_BUF, so
        # concurrent appends from multiple workers do not interleave.
        with open(_PATH, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def end(cid: str, phase: str = "end", **extra: Any) -> None:
    if not ENABLED or not cid:
        return
    mark(cid, phase, **extra)
    _starts.pop(str(cid), None)


class Span:
    """Context manager emitting ``<name>_begin`` / ``<name>_end`` markers."""

    __slots__ = ("cid", "name", "_t0", "extra")

    def __init__(self, cid: str, name: str, **extra: Any):
        self.cid = cid
        self.name = name
        self.extra = extra
        self._t0: Optional[float] = None

    def __enter__(self):
        if ENABLED:
            self._t0 = time.monotonic()
            mark(self.cid, f"{self.name}_begin", **self.extra)
        return self

    def __exit__(self, exc_type, exc, tb):
        if ENABLED and self._t0 is not None:
            mark(self.cid, f"{self.name}_end",
                 span_ms=round((time.monotonic() - self._t0) * 1000.0, 1),
                 ok=exc_type is None, **self.extra)
        return False
