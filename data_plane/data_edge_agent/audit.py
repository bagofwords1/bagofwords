"""Local audit trail for the data edge agent (design C4 / C5).

Every operation the agent executes against a data source is recorded here:
timestamp, connection, operation, duration, row count, and outcome. The trail
is *local* — it lives on the customer's box, the same place the credentials do,
and is the operator's own record of what Bow asked this agent to run.

Two layers:
  * an append-only JSONL file, the durable trail, and
  * an in-memory ring the admin UI reads (seeded from the file's tail on start,
    so history survives a restart).

What is never written: credentials of any kind. SQL text *is* recorded (the
whole point of an audit trail is what ran), truncated to a sane length; a
deployment that considers SQL sensitive can point `audit_path` at an
access-controlled location — it never leaves the box regardless.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# SQL is recorded for provenance, not replay — cap it so one pathological query
# can't bloat every audit line.
_MAX_SQL_CHARS = 2000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AuditLog:
    """Append-only operation log with a bounded in-memory tail.

    `path=None` keeps the log in memory only (tests, or a deployment that
    disables the durable file). Thread-safe: `record` runs on the agent's event
    loop while `recent` is read from the admin handler on that same loop, but a
    lock keeps it correct even if that ever changes.
    """

    def __init__(self, path: Optional[str | Path] = None, retain: int = 2000) -> None:
        self._path = Path(path) if path else None
        self._ring: deque[dict[str, Any]] = deque(maxlen=max(1, retain))
        self._lock = threading.Lock()
        if self._path is not None:
            self._load_tail()

    def _load_tail(self) -> None:
        """Seed the ring from the end of the file so the UI shows history after
        a restart. Best-effort: a corrupt or unreadable line is skipped, never
        fatal — an audit reader must not be what stops the agent booting."""
        if not self._path.is_file():
            return
        try:
            lines = self._path.read_text().splitlines()
        except OSError as e:  # pragma: no cover - unreadable file
            logger.warning("edge_agent.audit.load_failed", extra={"error": str(e)})
            return
        for line in lines[-self._ring.maxlen:]:
            line = line.strip()
            if not line:
                continue
            try:
                self._ring.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    def record(
        self,
        *,
        connection: Optional[str],
        operation: str,
        outcome: str,
        duration_ms: int,
        row_count: Optional[int] = None,
        request_id: Any = None,
        error: Optional[str] = None,
        sql: Optional[str] = None,
    ) -> dict[str, Any]:
        """Append one entry. Returns it (handy for tests)."""
        entry: dict[str, Any] = {
            "ts": _now_iso(),
            "connection": connection,
            "operation": operation,
            "outcome": outcome,
            "duration_ms": duration_ms,
        }
        if row_count is not None:
            entry["row_count"] = row_count
        if request_id is not None:
            entry["request_id"] = request_id
        if error:
            entry["error"] = error
        if sql:
            entry["sql"] = sql[:_MAX_SQL_CHARS]

        with self._lock:
            self._ring.append(entry)
            if self._path is not None:
                self._append_line(entry)
        return entry

    def _append_line(self, entry: dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Open per-append rather than holding a handle: an audit line is
            # tiny and infrequent relative to a query, and a persistent handle
            # is one more thing to own and flush across the process's life.
            # 0600 — the trail records what ran, on the same box as the secrets.
            fd = os.open(str(self._path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (json.dumps(entry) + "\n").encode())
            finally:
                os.close(fd)
        except OSError as e:  # never let audit persistence break a live request
            logger.warning("edge_agent.audit.write_failed", extra={"error": str(e)})

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """The most recent entries, newest first."""
        with self._lock:
            items = list(self._ring)
        if limit > 0:
            items = items[-limit:]
        items.reverse()
        return items

    def __len__(self) -> int:
        return len(self._ring)
