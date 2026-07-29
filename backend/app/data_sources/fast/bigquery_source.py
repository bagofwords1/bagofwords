"""Materializing from BigQuery through its own API rather than SQL.

BigQuery is the source that justified making extraction a protocol. It has no
SQLAlchemy engine, and on all three questions the extractor asks, the native
client does better than a SQL-shaped equivalent could:

**Estimating.** A dry run compiles the query and reports exactly how many bytes
it will scan, without executing and without charge. Every other source guesses:
Postgres reports planner row counts, Snowflake reports partitions it *expects*
to touch. BigQuery reports the number it will bill you for. For a source priced
per byte scanned, a free exact answer to "what will this cost" is the single
most useful thing the API offers.

**Streaming.** `to_arrow_iterable()` yields Arrow record batches directly. The
extractor's next move is to build a `pa.Table` and append it to DuckDB, so this
is the shortest possible path — going through SQLAlchemy would mean Arrow →
Python tuples → Arrow, paying a conversion in each direction for nothing.

**Cancelling.** `query_job.cancel()` stops the job server-side. There is no
connection to invalidate and no thread to abandon; the job has an id and the
API takes it.

The one thing this source must be careful about is cost. Unlike a Postgres box
where a runaway query wastes CPU, a runaway query here spends money, so
`maximum_bytes_billed` is set on every job as a hard server-side ceiling — the
job is rejected before it runs rather than stopped partway.
"""

import logging
from typing import Iterator, List, Tuple

logger = logging.getLogger(__name__)

# Byte ceiling applied to every extraction job. BigQuery rejects a query whose
# dry-run estimate exceeds this rather than running it and stopping partway, so
# it is a genuine cost guarantee and not a best-effort abort.
DEFAULT_MAX_BYTES_BILLED = 100 * 1024 * 1024 * 1024   # 100 GiB


class BigQuerySource:
    """Extraction over the native BigQuery client."""

    def __init__(self, client):
        self.client = client
        self._bq = client.client          # google.cloud.bigquery.Client

    # -- job configuration ------------------------------------------------

    def _job_config(self, **kw):
        from google.cloud import bigquery

        cap = getattr(self.client, "maximum_bytes_billed", None)
        if not isinstance(cap, int) or cap <= 0:
            cap = DEFAULT_MAX_BYTES_BILLED
        cfg = bigquery.QueryJobConfig(maximum_bytes_billed=cap, **kw)
        return cfg

    # -- the protocol -----------------------------------------------------

    def estimate(self, sql: str):
        """Exact scan bytes from a dry run — no execution, no charge.

        Reported as `scan_bytes`, never as a result size. The two diverge most
        on the queries this feature is for: an aggregate over a 200M-row table
        scans gigabytes and returns a dozen rows, and refusing it for the scan
        would be exactly backwards.

        BigQuery answers `COUNT(*)` and similar from table metadata, so a
        legitimate estimate of 0 bytes exists and is not an error.
        """
        from app.data_sources.fast.extractor import Estimate

        try:
            job = self._bq.query(
                sql, job_config=self._job_config(dry_run=True, use_query_cache=False)
            )
            scanned = int(job.total_bytes_processed or 0)
        except Exception as e:
            return Estimate(supported=False, note=f"BigQuery dry run failed: {e}")

        return Estimate(
            scan_bytes=scanned,
            note=(
                "exact scan size from a BigQuery dry run (free, not executed); "
                "result size is bounded by the hard caps instead"
            ),
        )

    def preview(self, sql: str, limit: int) -> Tuple[List[dict], List[list]]:
        """First `limit` rows, without paying for the whole result set.

        `max_results` bounds what the API returns rather than rewriting the
        admin's SQL, so a query carrying its own ORDER BY keeps its meaning —
        the same reason the SQL sources bound the fetch instead of wrapping.
        """
        job = self._bq.query(sql, job_config=self._job_config(use_query_cache=True))
        it = job.result(max_results=limit)
        cols = [{"name": f.name, "dtype": f.field_type} for f in it.schema]
        rows = []
        for row in it:
            rows.append([_jsonable(v) for v in row.values()])
            if len(rows) >= limit:
                break
        return cols, rows

    def stream_batches(self, sql: str, batch_rows: int) -> Iterator["object"]:
        """Arrow batches straight from BigQuery into the artifact."""
        import pyarrow as pa

        job = self._bq.query(sql, job_config=self._job_config(use_query_cache=False))
        it = job.result(page_size=batch_rows)

        emitted = False
        try:
            for record_batch in it.to_arrow_iterable():
                emitted = True
                yield pa.Table.from_batches([record_batch])
        except GeneratorExit:
            # The extractor stopped early — a cap was breached, or the caller
            # gave up. The job is still running and still billing, so stop it.
            self._cancel(job)
            raise
        except Exception:
            self._cancel(job)
            raise

        if not emitted:
            # Zero rows: still emit the shape so the relation exists.
            schema = pa.schema([(f.name, pa.string()) for f in it.schema])
            yield pa.Table.from_pydict({f.name: [] for f in it.schema}, schema=schema)

    # -- helpers ----------------------------------------------------------

    def _cancel(self, job) -> None:
        try:
            job.cancel()
            logger.info("bigquery.extraction.cancelled", extra={"job_id": job.job_id})
        except Exception:
            logger.debug("Could not cancel BigQuery job", exc_info=True)


def _jsonable(v):
    """Preview rows go straight to JSON; BigQuery hands back rich types."""
    import datetime as _dt
    import decimal

    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return str(v)
