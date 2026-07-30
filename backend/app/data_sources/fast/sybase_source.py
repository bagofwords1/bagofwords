"""Materializing from Sybase SQL Anywhere over raw pyodbc.

SQL Anywhere cannot take the SQLAlchemy path: SQLAlchemy 2.0 removed its
Sybase dialect, and the client speaks to the server through FreeTDS with a
raw pyodbc connection. So this is a native `ExtractionSource`, the same shape
BigQuery uses — the protocol's three questions answered directly against the
driver.

The dialect quirks that matter here:

* **Estimating.** SQL Anywhere has no SHOWPLAN_XML. `GRAPHICAL_PLAN('<sql>')`
  compiles the statement without executing it and returns the optimizer's plan
  as XML, which carries estimated row counts. The XML schema is not publicly
  pinned down, so the parse is deliberately permissive (several known spellings
  of the estimate attribute) and degrades to an unsupported estimate — falling
  back to the hard caps — rather than guessing.
* **Bounding.** The fetch is stopped rather than the SQL rewritten, for the
  same reasons as every other source (a wrapped ORDER BY changes meaning).
* **Cancelling.** pyodbc's `Cursor.cancel()` (SQLCancel) asks the server to
  stop the statement. Best effort: FreeTDS support varies, so failure is
  logged and the cursor is closed regardless.

Sybase sits in UNVERIFIED_TYPES until this file has been run against a live
SQL Anywhere engine — GRAPHICAL_PLAN's XML shape and SQLCancel-over-FreeTDS
are exactly the kind of thing a fake cannot prove (the SQL Server KILL bug is
the cautionary tale).
"""

import logging
import re
from typing import Iterator, List, Tuple

logger = logging.getLogger(__name__)

# The client's default 60s statement timeout protects live agent queries; an
# extraction is a deliberate full read and gets the extractor's wall-clock
# budget instead (see extractor.DEFAULT_MAX_SECONDS).
EXTRACTION_TIMEOUT_SECONDS = 1800

# GRAPHICAL_PLAN's XML schema is not documented as stable. These are the
# spellings of "estimated rows" worth looking for; first hit wins.
_EST_ROWS_PATTERNS = (
    re.compile(r'EstRowCount\s*=\s*"([\d.eE+]+)"'),
    re.compile(r'est_rows\s*=\s*"([\d.eE+]+)"'),
    re.compile(r'Estimated\s+rows[^\d]*([\d.eE+]+)', re.IGNORECASE),
)


class SybaseSource:
    """Extraction over the Sybase client's raw pyodbc connection."""

    def __init__(self, client):
        self.client = client

    # -- the protocol -----------------------------------------------------

    def estimate(self, sql: str):
        """Estimated result rows from GRAPHICAL_PLAN, without executing.

        Width is assumed, not reported — SQL Anywhere's plan does not carry
        one — so the byte ceiling has something to bite on and the note says
        it is a guess.
        """
        from app.data_sources.fast.extractor import Estimate
        from app.data_sources.fast.sql_dialect import (
            ASSUMED_ROW_WIDTH_BYTES,
            strip_trailing_semicolon,
        )

        stmt = strip_trailing_semicolon(sql).replace("'", "''")
        try:
            with self.client.connect() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(f"SELECT GRAPHICAL_PLAN('{stmt}')")
                    row = cursor.fetchone()
                finally:
                    cursor.close()
        except Exception as e:
            return Estimate(supported=False, note=f"sybase GRAPHICAL_PLAN failed: {e}")

        plan = row[0] if row else None
        rows = _estimated_rows(plan)
        if rows is None:
            return Estimate(supported=False,
                            note="sybase plan had no recognizable row estimate")
        return Estimate(rows=rows, width_bytes=ASSUMED_ROW_WIDTH_BYTES,
                        note="row width estimated (GRAPHICAL_PLAN reports none)")

    def preview(self, sql: str, limit: int) -> Tuple[List[dict], List[list]]:
        """First `limit` rows, stopping the fetch rather than wrapping the SQL."""
        from app.data_sources.fast.sql_dialect import strip_trailing_semicolon

        with self.client.connect() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(strip_trailing_semicolon(sql))
                cols = _columns(cursor)
                rows = [list(r) for r in cursor.fetchmany(limit)]
                if len(rows) == limit:
                    # There may be more; ask the server to stop instead of
                    # draining the rest of the result down the wire.
                    _cancel(cursor)
            finally:
                cursor.close()
        return cols, rows

    def stream_batches(self, sql: str, batch_rows: int) -> Iterator["object"]:
        """Yield pyarrow.Table batches covering the whole result."""
        from app.data_sources.fast.sources import arrow_table

        with self.client.connect() as conn:
            # A full extraction may legitimately outlive the client's 60s
            # statement timeout; give it the refresh budget instead.
            try:
                conn.timeout = EXTRACTION_TIMEOUT_SECONDS
            except Exception:
                pass
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                colnames = [c["name"] for c in _columns(cursor)]
                emitted = False
                while True:
                    batch = cursor.fetchmany(batch_rows)
                    if not batch:
                        break
                    emitted = True
                    yield arrow_table(
                        {name: [r[i] for r in batch] for i, name in enumerate(colnames)}
                    )
                if not emitted:
                    # Zero rows: still emit the shape so the relation exists.
                    yield arrow_table({c: [] for c in colnames})
            except GeneratorExit:
                # The extractor stopped early — a cap was breached. The
                # statement is still running on the source; ask it to stop.
                _cancel(cursor)
                raise
            finally:
                cursor.close()


def _columns(cursor) -> List[dict]:
    """(name, dtype) pairs from a pyodbc cursor description."""
    return [
        {"name": d[0], "dtype": getattr(d[1], "__name__", None)}
        for d in (cursor.description or [])
    ]


def _cancel(cursor) -> None:
    """Best-effort SQLCancel; FreeTDS support varies and failure is not fatal."""
    try:
        cursor.cancel()
    except Exception:
        logger.debug("Sybase statement cancel failed", exc_info=True)


def _estimated_rows(plan) -> "int | None":
    if not isinstance(plan, str) or not plan:
        return None
    for pattern in _EST_ROWS_PATTERNS:
        m = pattern.search(plan)
        if m:
            try:
                return int(float(m.group(1)))
            except (TypeError, ValueError):
                continue
    return None
