"""Sybase SQL Anywhere native extraction source.

The driver (pyodbc over FreeTDS) is the boundary, so it is faked; everything
above it — resolution, fetch-bounding, batch shaping, estimate degradation —
runs real. What a fake cannot prove (GRAPHICAL_PLAN's XML on a live engine,
SQLCancel over FreeTDS) is exactly why the type sits in UNVERIFIED_TYPES.
"""

from contextlib import contextmanager

from app.data_sources.fast.sybase_source import SybaseSource


# --------------------------------------------------------------------------
# Fakes at the pyodbc boundary
# --------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, rows, description):
        self._rows = list(rows)
        self.description = description
        self.executed = []
        self.cancelled = False
        self.closed = False

    def execute(self, sql):
        self.executed.append(sql)
        return self

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchmany(self, n):
        out, self._rows = self._rows[:n], self._rows[n:]
        return out

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.timeout = 60

    def cursor(self):
        return self._cursor


class FakeSybaseClient:
    def __init__(self, cursor):
        self.connection = FakeConnection(cursor)

    @contextmanager
    def connect(self):
        yield self.connection


def _source(rows, description=(("id", int), ("name", str))):
    cursor = FakeCursor(rows, description)
    return SybaseSource(FakeSybaseClient(cursor)), cursor


# --------------------------------------------------------------------------
# Resolution & gating
# --------------------------------------------------------------------------

def test_the_sybase_client_resolves_to_its_native_source():
    from app.data_sources.clients.sybase_client import SybaseClient
    from app.data_sources.fast.sources import source_for

    client = SybaseClient(host="h", port=2638, database="d", user="u", password="p")
    assert isinstance(source_for(client), SybaseSource)


def test_sybase_is_an_accelerable_but_unverified_type():
    from app.services.custom_query_service import (
        ACCELERABLE_TYPES,
        UNVERIFIED_TYPES,
        is_accelerable_type,
    )

    assert "sybase" in ACCELERABLE_TYPES
    assert "sybase" in UNVERIFIED_TYPES
    assert is_accelerable_type("sybase")


# --------------------------------------------------------------------------
# Preview — bound the fetch, never rewrite the SQL
# --------------------------------------------------------------------------

def test_preview_returns_at_most_the_limit_and_stops_the_source():
    src, cursor = _source([(i, f"r{i}") for i in range(10)])
    cols, rows = src.preview("SELECT * FROM t", 3)
    assert [c["name"] for c in cols] == ["id", "name"]
    assert len(rows) == 3
    assert cursor.cancelled, "a partially-read statement must be stopped at the source"
    assert cursor.closed


def test_preview_of_a_small_result_does_not_cancel():
    src, cursor = _source([(1, "a")])
    _, rows = src.preview("SELECT * FROM t", 100)
    assert len(rows) == 1
    assert not cursor.cancelled


def test_preview_runs_the_sql_as_written():
    """Wrapping would break ORDER BY / duplicate-column queries — the bound
    lives in the fetch, so the statement must reach the driver untouched."""
    src, cursor = _source([])
    src.preview("SELECT a.id, b.id FROM a, b ORDER BY a.id DESC;", 5)
    assert cursor.executed == ["SELECT a.id, b.id FROM a, b ORDER BY a.id DESC"]


# --------------------------------------------------------------------------
# Streaming — batches cover the result, zero rows still emit the shape
# --------------------------------------------------------------------------

def test_stream_batches_covers_the_whole_result_in_batch_sized_pieces():
    src, _ = _source([(i, f"r{i}") for i in range(5)])
    batches = list(src.stream_batches("SELECT * FROM t", batch_rows=2))
    assert [b.num_rows for b in batches] == [2, 2, 1]
    assert sum(b.num_rows for b in batches) == 5
    assert batches[0].column_names == ["id", "name"]


def test_a_zero_row_result_still_emits_the_relation_shape():
    src, _ = _source([])
    batches = list(src.stream_batches("SELECT * FROM t WHERE 1=0", batch_rows=10))
    assert len(batches) == 1
    assert batches[0].num_rows == 0
    assert batches[0].column_names == ["id", "name"]


def test_an_abandoned_stream_cancels_the_statement_on_the_source():
    src, cursor = _source([(i, f"r{i}") for i in range(100)])
    gen = src.stream_batches("SELECT * FROM t", batch_rows=10)
    next(gen)
    gen.close()  # extractor breached a cap and stopped consuming
    assert cursor.cancelled
    assert cursor.closed


def test_extraction_lifts_the_statement_timeout_to_the_refresh_budget():
    """The client's 60s timeout protects live agent queries; a deliberate full
    extraction gets the extractor's wall-clock budget instead."""
    from app.data_sources.fast.sybase_source import EXTRACTION_TIMEOUT_SECONDS

    src, _ = _source([(1, "a")])
    list(src.stream_batches("SELECT * FROM t", batch_rows=10))
    assert src.client.connection.timeout == EXTRACTION_TIMEOUT_SECONDS


# --------------------------------------------------------------------------
# Estimation — permissive parse, honest degradation
# --------------------------------------------------------------------------

def test_estimate_reads_rows_from_a_graphical_plan():
    plan = '<plan><node EstRowCount="1234.5" cost="1"/></plan>'
    src, cursor = _source([(plan,)])
    est = src.estimate("SELECT * FROM t;")
    assert est.supported
    assert est.rows == 1234
    assert est.width_bytes is not None, "byte ceiling needs a width to bite on"
    # The statement is compiled inside a string literal, quotes escaped, and
    # never executed as-is.
    assert "GRAPHICAL_PLAN" in cursor.executed[0]


def test_estimate_escapes_quotes_inside_the_admins_sql():
    src, cursor = _source([(None,)])
    src.estimate("SELECT * FROM t WHERE name = 'o''brien'")
    sent = cursor.executed[0]
    assert "GRAPHICAL_PLAN('" in sent
    assert "o''''brien" in sent


def test_an_unrecognizable_plan_degrades_to_unsupported():
    """Falling back to the hard caps is the honest outcome; inventing a row
    count is not."""
    src, _ = _source([("<plan>nothing useful here</plan>",)])
    est = src.estimate("SELECT 1")
    assert est.supported is False
    assert est.note


def test_a_driver_error_during_estimate_degrades_rather_than_raising():
    class BrokenClient:
        @contextmanager
        def connect(self):
            raise RuntimeError("no GRAPHICAL_PLAN permission")
            yield

    est = SybaseSource(BrokenClient()).estimate("SELECT 1")
    assert est.supported is False
    assert "GRAPHICAL_PLAN" in est.note
