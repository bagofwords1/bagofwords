"""Extraction is a protocol, and the sources that implement it natively.

The extractor used to assume every source was a SQLAlchemy connection with a
dialect-specific EXPLAIN. BigQuery is not: it has no engine, its estimator is a
free dry run that returns *exact* scan bytes rather than a planner guess, and
its results arrive as Arrow batches — the very thing the extractor builds. Three
sources in a row (SQLite, Fabric, BigQuery) needed something other than
"connect() gives you SQLAlchemy", so it became `ExtractionSource`.

What these pin: that resolution is by declaration rather than guesswork, that
scan cost and result size never get confused (they diverge hardest on exactly
the queries worth caching), and that a source which bills per byte stops its
job when the extractor gives up.
"""

import pytest

from app.data_sources.fast import sources
from app.data_sources.fast.extractor import Estimate


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

class _Declared:
    EXTRACTION_DIALECT = "postgresql"


class _Native:
    class _Src:
        def __init__(self, client):
            self.client = client

    EXTRACTION_SOURCE = staticmethod(lambda client: _Native._Src(client))


class _Nothing:
    pass


def test_a_client_declaring_a_dialect_gets_the_sqlalchemy_source():
    src = sources.source_for(_Declared())
    assert isinstance(src, sources.SqlAlchemySource)
    assert src.dialect == "postgresql"


def test_a_client_with_a_native_source_gets_that_instead():
    """BigQuery must not be driven through SQLAlchemy just because it could
    theoretically be — its own API is better at all three questions."""
    src = sources.source_for(_Native())
    assert type(src).__name__ == "_Src"


def test_a_client_with_neither_cannot_be_materialized():
    assert sources.source_for(_Nothing()) is None


def test_resolution_prefers_the_native_source_over_a_declared_dialect():
    class Both(_Native):
        EXTRACTION_DIALECT = "postgresql"

    assert type(sources.source_for(Both())).__name__ == "_Src"


def test_the_sqlalchemy_source_prefers_extraction_connect_when_present():
    """SQLite and Fabric cannot yield a SQLAlchemy connection from connect() —
    one opens raw sqlite3 for its PRAGMA reads, the other raw pyodbc because an
    Entra token cannot live in a URL. Both offer extraction_connect instead."""
    used = []

    class Client:
        EXTRACTION_DIALECT = "sqlite"

        def connect(self):
            used.append("connect")
            raise AssertionError("should have preferred extraction_connect")

        def extraction_connect(self):
            used.append("extraction_connect")
            from contextlib import contextmanager

            @contextmanager
            def cm():
                yield object()

            return cm()

    with sources.source_for(Client())._open():
        pass
    assert used == ["extraction_connect"]


# --------------------------------------------------------------------------
# Scan cost is not result size
# --------------------------------------------------------------------------

def test_an_estimate_can_report_cost_without_reporting_size():
    """Snowflake and BigQuery report bytes SCANNED and nothing about the
    result. That is a supported estimate, not a failed one."""
    est = Estimate(scan_bytes=5 * 1024**3, note="scan only")
    assert est.rows is None and est.total_bytes is None
    assert est.scan_bytes


def test_a_rollup_that_scans_a_lot_and_returns_little_is_not_refused():
    """The single most important budget behaviour. An aggregate over a billion
    rows scans gigabytes and returns twelve rows — refusing it would reject
    exactly the query this feature exists to serve."""
    from app.data_sources.fast.extractor import check_budget

    check_budget(Estimate(scan_bytes=50 * 1024**3))     # no rows, no result bytes


def test_the_scan_ceiling_still_refuses_a_genuinely_expensive_refresh():
    from app.data_sources.fast.extractor import ExtractionRefused, check_budget

    with pytest.raises(ExtractionRefused) as e:
        check_budget(Estimate(scan_bytes=500 * 1024**3))
    msg = str(e.value)
    assert "would scan" in msg
    # The admin needs to know it recurs, not just that it is big.
    assert "every refresh" in msg


def test_result_size_and_scan_size_are_checked_independently():
    from app.data_sources.fast.extractor import ExtractionRefused, check_budget

    # Small scan, huge result → refused on rows.
    with pytest.raises(ExtractionRefused) as e:
        check_budget(Estimate(rows=10**9, width_bytes=10, total_bytes=10**10,
                              scan_bytes=1024))
    assert "rows exceeds" in str(e.value)


def test_an_unsupported_estimate_never_refuses():
    """A source that cannot estimate must not block the save; the mid-flight
    caps still bound the extraction."""
    from app.data_sources.fast.extractor import check_budget

    check_budget(Estimate(supported=False, note="no estimator"))


# --------------------------------------------------------------------------
# BigQuery's native source
# --------------------------------------------------------------------------

class _FakeJob:
    def __init__(self, scanned=0, batches=(), schema=()):
        self.total_bytes_processed = scanned
        self.job_id = "job-1"
        self.cancelled = 0
        self._batches = list(batches)
        self._schema = list(schema)

    def cancel(self):
        self.cancelled += 1

    def result(self, max_results=None, page_size=None):
        return self

    @property
    def schema(self):
        return self._schema

    def to_arrow_iterable(self):
        yield from self._batches

    def __iter__(self):
        return iter([])


class _FakeBq:
    def __init__(self, job):
        self.job = job
        self.configs = []

    def query(self, sql, job_config=None):
        self.configs.append(job_config)
        return self.job


def _bq_source(job):
    from app.data_sources.fast.bigquery_source import BigQuerySource

    class Client:
        maximum_bytes_billed = None

    c = Client()
    c.client = _FakeBq(job)
    return BigQuerySource(c), c.client


def test_bigquery_estimates_with_a_dry_run():
    src, bq = _bq_source(_FakeJob(scanned=3_600_000_000))
    est = src.estimate("SELECT * FROM t")
    assert est.scan_bytes == 3_600_000_000
    assert bq.configs[0].dry_run is True
    # A dry run that used cached results would report 0 and mislead.
    assert bq.configs[0].use_query_cache is False


def test_bigquery_never_reports_scan_bytes_as_a_result_size():
    src, _ = _bq_source(_FakeJob(scanned=3_600_000_000))
    est = src.estimate("SELECT * FROM t")
    assert est.rows is None and est.total_bytes is None


def test_bigquery_zero_bytes_is_a_real_answer_not_a_failure():
    """COUNT(*) is answered from table metadata and scans nothing."""
    src, _ = _bq_source(_FakeJob(scanned=0))
    est = src.estimate("SELECT COUNT(*) FROM t")
    assert est.supported is True
    assert est.scan_bytes == 0


def test_bigquery_caps_bytes_billed_on_every_job():
    """A runaway query here spends money, not just CPU. BigQuery rejects a job
    over the cap before running it, so this is a real ceiling."""
    from app.data_sources.fast.bigquery_source import DEFAULT_MAX_BYTES_BILLED

    src, bq = _bq_source(_FakeJob())
    src.estimate("SELECT 1")
    assert bq.configs[0].maximum_bytes_billed == DEFAULT_MAX_BYTES_BILLED


def test_bigquery_honours_a_connection_level_byte_cap():
    from app.data_sources.fast.bigquery_source import BigQuerySource

    class Client:
        maximum_bytes_billed = 1234

    c = Client()
    c.client = _FakeBq(_FakeJob())
    BigQuerySource(c).estimate("SELECT 1")
    assert c.client.configs[0].maximum_bytes_billed == 1234


def test_bigquery_cancels_the_job_when_extraction_stops_early():
    """Abandoning the iterator leaves the job running — and billing. The
    extractor closes the generator on a cap breach, which must reach cancel()."""
    import pyarrow as pa

    batches = [pa.record_batch([pa.array([1, 2])], names=["a"]) for _ in range(50)]
    job = _FakeJob(batches=batches)
    src, _ = _bq_source(job)

    gen = src.stream_batches("SELECT a FROM t", 2)
    next(gen)
    gen.close()
    assert job.cancelled == 1


def test_bigquery_does_not_cancel_a_job_it_read_to_completion():
    import pyarrow as pa

    job = _FakeJob(batches=[pa.record_batch([pa.array([1])], names=["a"])])
    src, _ = _bq_source(job)
    assert len(list(src.stream_batches("SELECT a FROM t", 10))) == 1
    assert job.cancelled == 0


def test_bigquery_estimate_failure_degrades_rather_than_raising():
    from app.data_sources.fast.bigquery_source import BigQuerySource

    class Broken:
        def query(self, sql, job_config=None):
            raise RuntimeError("quota exceeded")

    class Client:
        maximum_bytes_billed = None

    c = Client()
    c.client = Broken()
    est = BigQuerySource(c).estimate("SELECT 1")
    assert est.supported is False
    assert "dry run failed" in est.note


# --------------------------------------------------------------------------
# Snowflake's explainer
# --------------------------------------------------------------------------

def test_snowflake_reports_scan_bytes_and_no_result_size():
    """Verified against a live account: EXPLAIN USING JSON gives GlobalStats
    with bytesAssigned and no row estimate at all."""
    import json

    from app.data_sources.fast import sql_dialect

    class Conn:
        def execute(self, stmt, params=None):
            class R:
                def scalar(self_inner):
                    return json.dumps({
                        "GlobalStats": {"partitionsTotal": 116,
                                        "partitionsAssigned": 116,
                                        "bytesAssigned": 1717059584},
                        "Operations": [],
                    })
            return R()

    est = sql_dialect.explain_snowflake(Conn(), "SELECT 1")
    assert est.scan_bytes == 1717059584
    assert est.rows is None and est.total_bytes is None
    assert "scan size only" in est.note


def test_snowflake_without_globalstats_reports_no_estimate():
    import json

    from app.data_sources.fast import sql_dialect

    class Conn:
        def execute(self, stmt, params=None):
            class R:
                def scalar(self_inner):
                    return json.dumps({"Operations": []})
            return R()

    est = sql_dialect.explain_snowflake(Conn(), "SELECT 1")
    assert est.scan_bytes is None
    assert "bytesAssigned" in est.note


# --------------------------------------------------------------------------
# The admin has to be able to see the cost before scheduling it
# --------------------------------------------------------------------------

def test_the_preview_response_carries_scan_cost_separately():
    """On Snowflake and BigQuery `estimated_rows` is None — they report scan
    cost, not result size. Without a separate field the modal would show
    'unknown rows' and the number that actually costs money would never reach
    the person choosing the refresh interval."""
    from app.schemas.custom_query_schema import CustomQueryPreviewResponse

    r = CustomQueryPreviewResponse(
        row_limit=100, estimated_rows=None, estimated_bytes=None,
        scan_bytes=3_600_000_000,
    )
    assert r.scan_bytes == 3_600_000_000
    assert r.estimated_rows is None


def test_the_preview_response_defaults_scan_cost_to_none():
    """Sources that report result size and not scan cost (Postgres, MySQL)
    must not gain a bogus zero."""
    from app.schemas.custom_query_schema import CustomQueryPreviewResponse

    r = CustomQueryPreviewResponse(row_limit=100, estimated_rows=1000)
    assert r.scan_bytes is None


# --------------------------------------------------------------------------
# Decimals the artifact cannot store
# --------------------------------------------------------------------------

def test_a_decimal_too_wide_for_duckdb_is_widened_to_float():
    """Oracle returns NUMBER-without-precision — what AVG() and most arithmetic
    produce — as a decimal of precision 39+, and DuckDB tops out at 38.
    Extraction died on `SELECT AVG(amount) ... GROUP BY ...`, which is close to
    the most ordinary custom query anyone would write. Verified against Oracle
    Free 23ai."""
    import decimal

    import pyarrow as pa

    from app.data_sources.fast.sources import arrow_table

    wide = [decimal.Decimal("1.234567890123456789012345678901234567890")]
    tbl = arrow_table({"avg_amt": wide})
    assert pa.types.is_floating(tbl.schema.field("avg_amt").type)


def test_a_decimal_duckdb_can_store_keeps_its_exact_type():
    """A column declared NUMBER(12,2) must not silently become a float — the
    widening is a last resort for values with no representable decimal form."""
    import decimal

    import pyarrow as pa

    from app.data_sources.fast.sources import arrow_table

    tbl = arrow_table({"amount": [decimal.Decimal("1234.56")]})
    assert pa.types.is_decimal(tbl.schema.field("amount").type)


def test_widening_leaves_other_columns_alone():
    import decimal

    import pyarrow as pa

    from app.data_sources.fast.sources import arrow_table

    tbl = arrow_table({
        "status": ["completed"],
        "n": [42],
        "avg_amt": [decimal.Decimal("1." + "1" * 38)],
    })
    assert pa.types.is_string(tbl.schema.field("status").type)
    assert pa.types.is_integer(tbl.schema.field("n").type)
    assert pa.types.is_floating(tbl.schema.field("avg_amt").type)


def test_the_widened_value_survives_a_round_trip_into_duckdb():
    """The point is not the cast, it is that register() stops failing."""
    import decimal

    import duckdb

    from app.data_sources.fast.sources import arrow_table

    tbl = arrow_table({"avg_amt": [decimal.Decimal("2." + "3" * 38)]})
    con = duckdb.connect(database=":memory:")
    con.register("t", tbl)
    assert round(con.execute("SELECT avg_amt FROM t").fetchone()[0], 3) == 2.333
    con.close()
