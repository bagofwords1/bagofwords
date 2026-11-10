"""Health and readiness reporting.

Two endpoints with deliberately different jobs (wired in ``main.py``):

* ``/health`` — the **liveness** probe used by k8s, the Docker ``HEALTHCHECK``
  and CI wait loops. It must stay dependency-free and answer ``200`` as long as
  the web process is responding, because the k8s ``livenessProbe``
  (``k8s/chart/values.yaml``) *restarts the pod* when it fails. If this endpoint
  started returning ``503`` on a brief database outage, k8s would kill every
  otherwise-healthy pod and ``CrashLoopBackOff`` the whole app over a problem
  that lives outside the pod. So ``/health`` only ever adds cheap, non-failing
  facts (version, uptime).

* ``/health/ready`` — **readiness / observability**. It is free to return
  ``503`` because nothing restarts a pod over it. It runs the dependency checks
  that *can* fail (database, scheduler) and reports, per check, what the state
  is and — on failure — a coarse reason, so an operator or a monitoring system
  learns *which* component is down and *what kind* of failure it is without
  reading the logs.

Error reporting is intentionally coarse: these endpoints are unauthenticated
(a probe cannot log in), so a failing check reports the failing component and
the exception *type* — never the raw exception string, which for a database
error can carry the host, port or credentials. The full exception is written to
the server log, where topology details are already visible.
"""

import asyncio
import os
import socket
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Tuple

from sqlalchemy import text

# Process start, captured when this module is first imported during app
# bootstrap. Monotonic so a wall-clock adjustment can never make uptime run
# backwards or jump.
_PROCESS_START = time.monotonic()

# Hard ceiling on the database probe. A DB that is up but hung (pool exhausted,
# a lock, an overloaded server) must not make /health/ready hang with it: the
# k8s readiness probe has its own short timeout, and a check that blocks past it
# is reported by k8s as a bare timeout with no detail. Bounding it here means a
# hung DB returns a fast, explicit "down" (error_type "TimeoutError") instead.
_DB_CHECK_TIMEOUT_SECONDS = 2.0


def uptime_seconds() -> float:
    """Seconds since this worker process started."""
    return round(time.monotonic() - _PROCESS_START, 3)


def utcnow_iso() -> str:
    """Current server time (UTC, ISO-8601). Lets a monitor spot a stale or
    clock-skewed response."""
    return datetime.now(timezone.utc).isoformat()


def worker_identity() -> Dict[str, Any]:
    """Which process answered. Behind a k8s Service with N replicas this is the
    only way to tell *which* pod a given response came from."""
    return {"hostname": socket.gethostname(), "pid": os.getpid()}


async def _check_database(
    session_maker: Callable[[], Any],
) -> Dict[str, Any]:
    """Run ``SELECT 1`` to confirm the database is reachable and answering.

    Read-only (no commit) so it is safe against a read replica. Bounded by
    ``_DB_CHECK_TIMEOUT_SECONDS`` so a hung DB fails fast rather than blocking.
    On failure the report carries only the exception *type*, not its message.
    """

    async def _probe() -> None:
        async with session_maker() as session:
            await session.execute(text("SELECT 1"))

    started = time.monotonic()
    try:
        await asyncio.wait_for(_probe(), timeout=_DB_CHECK_TIMEOUT_SECONDS)
        latency_ms = round((time.monotonic() - started) * 1000, 2)
        # latency distinguishes a healthy DB from one that is up but struggling.
        return {"status": "ok", "critical": True, "latency_ms": latency_ms}
    except Exception as e:  # noqa: BLE001 — any driver error (or timeout) means "not ready"
        return {
            "status": "down",
            "critical": True,
            "error": "database query failed",
            "error_type": type(e).__name__,
        }


def _check_scheduler(scheduler: Any, is_leader: bool) -> Dict[str, Any]:
    """Report the APScheduler state.

    Informational (``critical: False``): a web worker can still serve API
    traffic while the scheduler is stopped, so a stopped scheduler must not flip
    the endpoint to ``503`` and pull the worker out of the Service. ``is_leader``
    tells an operator which worker actually registers and runs scheduled jobs.
    """
    running = bool(getattr(scheduler, "running", False))
    return {
        "status": "ok" if running else "down",
        "critical": False,
        "running": running,
        "is_leader": bool(is_leader),
    }


async def build_readiness_report(
    session_maker: Callable[[], Any],
    scheduler: Any,
    is_leader: bool,
    version: str,
) -> Tuple[Dict[str, Any], bool]:
    """Run every readiness check and assemble the report.

    Returns ``(report, healthy)``. ``healthy`` is ``True`` only when every
    **critical** check passed; the caller maps it to ``200`` / ``503``.
    Non-critical checks appear in the report and in the top-level ``status`` but
    never make ``healthy`` false on their own.
    """
    checks: Dict[str, Dict[str, Any]] = {
        "database": await _check_database(session_maker),
        "scheduler": _check_scheduler(scheduler, is_leader),
    }

    healthy = all(
        c["status"] == "ok" for c in checks.values() if c.get("critical")
    )
    all_ok = all(c["status"] == "ok" for c in checks.values())

    report = {
        # "ok" when everything passed; "degraded" when only non-critical checks
        # are down (still 200); "down" when a critical check failed (503).
        "status": "ok" if all_ok else ("down" if not healthy else "degraded"),
        "version": version,
        "uptime_seconds": uptime_seconds(),
        "timestamp": utcnow_iso(),
        "worker": worker_identity(),
        "checks": checks,
    }
    return report, healthy
