"""A custom table stores every decimal the source returns, exactly, in every batch.

The extract is written in batches and the artifact's columns are created from
the first one. When a decimal's type was inferred from that batch's values,
`numeric(12,2)` holding nothing over 99,999.99 became DECIMAL(7,2) and the
first later batch with a larger value failed the whole refresh; a column that
was all NULL in the first batch became INTEGER and later values lost their
fraction silently.

The driver is the boundary: a DB-API cursor is faked (the Sybase source reads
one directly), everything from the source through the real encrypted artifact
runs real. One test also runs the SQLAlchemy source against the Postgres the
suite already has, when it has one, to pin what psycopg2 actually declares.
"""

import os
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.data_sources.fast import artifacts, extractor
from app.data_sources.fast.sybase_source import SybaseSource

BATCH = extractor.BATCH_ROWS


class _Cursor:
    def __init__(self, rows, description):
        self._rows = list(rows)
        self.description = description

    def execute(self, sql):
        return self

    def fetchmany(self, n):
        out, self._rows = self._rows[:n], self._rows[n:]
        return out

    def cancel(self):
        pass

    def close(self):
        pass


class _Conn:
    timeout = 60

    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class _Client:
    """A client whose driver hands back DB-API rows and a cursor description."""

    def __init__(self, rows, description):
        self._conn = _Conn(_Cursor(rows, description))
        self.EXTRACTION_SOURCE = lambda client: SybaseSource(client)

    @contextmanager
    def connect(self):
        yield self._conn


def _desc(name, precision=None, scale=None):
    # DB-API 7-tuple: name, type_code, display_size, internal_size, precision, scale, null_ok
    return (name, Decimal, None, precision, precision, scale, True)


@pytest.fixture
def extract(tmp_path):
    def run(client, rel="t"):
        with patch.object(artifacts, "_ARTIFACT_ROOT", tmp_path / "fast"):
            res = extractor.extract_to_artifact(client, "SELECT id, amount FROM t", rel, "conn")
            con = artifacts.connect_encrypted(res.artifact_path, res.artifact_key, read_only=True)
            try:
                rows = con.execute(f'SELECT id, amount FROM "{rel}" ORDER BY id').fetchall()
                col_type = con.execute(
                    f"SELECT data_type FROM information_schema.columns "
                    f"WHERE table_name = '{rel}' AND column_name = 'amount'"
                ).fetchone()[0]
            finally:
                con.close()
        return res, rows, col_type
    return run


def _rows(first, later, first_count=BATCH):
    """`first_count` rows of `first`, then each value of `later` in later batches."""
    out = [(i, first) for i in range(first_count)]
    out += [(first_count + j, v) for j, v in enumerate(later)]
    return out


def _assert_exact(source_rows, stored_rows):
    assert len(stored_rows) == len(source_rows)
    for (sid, sval), (did, dval) in zip(source_rows, stored_rows, strict=True):
        assert did == sid
        if sval is None:
            assert dval is None
        else:
            assert Decimal(str(dval)) == sval, (sid, sval, dval)


# --------------------------------------------------------------------------
# A declared precision is the column's type, whatever the values are
# --------------------------------------------------------------------------

@pytest.mark.parametrize("precision,scale,first,later", [
    (12, 2, Decimal("12.34"), [Decimal("123456.78"), Decimal("-9999999999.99")]),
    (20, 10, Decimal("-0.0000000001"), [Decimal("-1234567890.1234567890"),
                                       Decimal("9999999999.9999999999")]),
    (9, 0, Decimal("7"), [Decimal("987654321")]),
])
def test_a_declared_decimal_holds_later_batches_with_larger_values(extract, precision, scale,
                                                                  first, later):
    source = _rows(first, later)
    _, stored, col_type = extract(_Client(source, [_desc("id"), _desc("amount", precision, scale)]))
    _assert_exact(source, stored)
    assert col_type == f"DECIMAL({precision},{scale})"


def test_a_declared_decimal_keeps_its_type_across_refreshes(extract):
    """The column's type follows the source's declaration, not this refresh's data."""
    desc = [_desc("id"), _desc("amount", 12, 2)]
    _, _, small = extract(_Client([(1, Decimal("5.00"))], desc), rel="a")
    _, _, large = extract(_Client([(1, Decimal("1234567.89"))], desc), rel="b")
    assert small == large == "DECIMAL(12,2)"


# --------------------------------------------------------------------------
# Undeclared decimals (bare NUMERIC, AVG()) widen instead of failing or rounding
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first,later", [
    (Decimal("1.5"), [Decimal("98765.4321"), Decimal("0.000123")]),   # more digits, more scale
    (Decimal("-3"), [Decimal("-2.71828")]),                          # scale only
    (Decimal("0.25"), [Decimal("12345678901234567890.5")]),          # many integer digits
])
def test_an_undeclared_decimal_stores_every_later_batch_exactly(extract, first, later):
    source = _rows(first, later)
    # psycopg2 reports a bare NUMERIC as precision/scale 65535; others send None.
    for desc in ([_desc("id"), _desc("amount", 65535, 65535)], [("id", int), ("amount", Decimal)]):
        _, stored, _ = extract(_Client(source, desc))
        _assert_exact(source, stored)


def test_a_column_null_for_the_whole_first_batch_keeps_later_values(extract):
    source = _rows(None, [Decimal("4321.09"), Decimal("-0.5")])
    for desc in ([_desc("id"), _desc("amount", 12, 2)], [("id", int), ("amount", Decimal)]):
        res, stored, _ = extract(_Client(source, desc))
        _assert_exact(source, stored)
        assert "decimal" in next(c["dtype"] for c in res.columns if c["name"] == "amount")


@pytest.mark.parametrize("first_count", [2, BATCH])
def test_a_value_wider_than_its_declaration_is_still_stored_exactly(extract, first_count):
    """A driver that under-reports a declaration must not cost precision."""
    source = _rows(Decimal("1.00"), [Decimal("1.005"), Decimal("123456789012.5")],
                   first_count=first_count)
    _, stored, _ = extract(_Client(source, [_desc("id"), _desc("amount", 12, 2)]))
    _assert_exact(source, stored)


# --------------------------------------------------------------------------
# The SQLAlchemy source on real Postgres, when the suite runs against one
# --------------------------------------------------------------------------

def test_postgres_numeric_is_extracted_exactly_across_batches(tmp_path):
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("needs the suite's Postgres (--db=postgres or --db=external)")
    from sqlalchemy.engine import make_url

    from app.data_sources.clients.postgresql_client import PostgresqlClient

    u = make_url(url)
    client = PostgresqlClient(host=u.host, port=u.port or 5432, database=u.database,
                              user=u.username, password=u.password or "", sslmode="disable")
    n = BATCH + 1
    # Every value but the last fits DECIMAL(7,2); the last one is in batch two.
    sql = (f"SELECT g AS id, (CASE WHEN g = {n} THEN 123456.78 ELSE 12.34 END)::numeric(12,2) "
           f"AS amount FROM generate_series(1, {n}) g ORDER BY g")
    with patch.object(artifacts, "_ARTIFACT_ROOT", tmp_path / "fast"):
        res = extractor.extract_to_artifact(client, sql, "t", "conn")
        con = artifacts.connect_encrypted(res.artifact_path, res.artifact_key, read_only=True)
        try:
            count, total, top, col_type = con.execute(
                "SELECT count(*), sum(amount)::VARCHAR, max(amount)::VARCHAR, "
                "any_value(typeof(amount)) FROM t"
            ).fetchone()
        finally:
            con.close()
    assert count == n
    assert Decimal(total) == Decimal("12.34") * (n - 1) + Decimal("123456.78")
    assert Decimal(top) == Decimal("123456.78")
    assert col_type == "DECIMAL(12,2)"
