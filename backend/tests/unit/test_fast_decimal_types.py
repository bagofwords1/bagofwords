"""A custom table stores every number the source returns, exactly, in every batch.

The extract is written in batches and the artifact's columns are created from
the first one. When a decimal's type was inferred from that batch's values,
`numeric(12,2)` holding nothing over 99,999.99 became DECIMAL(7,2) and the
first later batch with a larger value failed the whole refresh; a column that
was all NULL in the first batch became INTEGER and later values lost their
fraction silently.

The driver is the boundary. Two fakes stand in for it:

* a DB-API cursor, which the Sybase source reads directly;
* a DB-API connection wrapped around real `sqlite3`, driven by a real
  SQLAlchemy engine through `SqlAlchemySource`. It reports the declared
  precision/scale a typed driver would and returns `Decimal`s for those
  columns, and the dialect is switched to name normalization — the code path
  SQLAlchemy takes on Oracle and Snowflake, where `AMOUNT` becomes `amount`.

Everything from the source through the real encrypted artifact runs real. One
test also runs against the Postgres the suite has, when it has one, to pin
what psycopg2 actually declares.

The cursor fakes return the batches a test lists, whatever size the extractor
asks for (DB-API allows a short `fetchmany`), so a "later batch" costs one row
instead of 50,000. One test keeps the extractor's real batch size.
"""

import logging
import math
import os
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.data_sources.fast import artifacts, extractor, loader
from app.data_sources.fast.sybase_source import SybaseSource

BATCH = extractor.BATCH_ROWS


# --------------------------------------------------------------------------
# Driver fakes
# --------------------------------------------------------------------------

class _Cursor:
    """A DB-API cursor. `batches` are returned one per fetchmany; with `rows`
    instead, fetchmany honours the size the extractor asks for."""

    def __init__(self, description, rows=None, batches=None):
        self.description = description
        self._rows = list(rows or [])
        self._batches = [list(b) for b in batches] if batches is not None else None

    def execute(self, sql):
        return self

    def fetchmany(self, n):
        if self._batches is not None:
            return self._batches.pop(0) if self._batches else []
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
    """A Sybase-style client whose driver hands back DB-API rows and a description."""

    def __init__(self, description, rows=None, batches=None):
        self._conn = _Conn(_Cursor(description, rows, batches))
        self.EXTRACTION_SOURCE = lambda client: SybaseSource(client)

    @contextmanager
    def connect(self):
        yield self._conn


def _desc(name, precision=None, scale=None):
    # DB-API 7-tuple: name, type_code, display_size, internal_size, precision, scale, null_ok
    return (name, Decimal, None, precision, precision, scale, True)


ORACLE_BARE_NUMBER = (0, -127)   # what oracledb declares for NUMBER without precision


class _TypedDriverCursor:
    """sqlite3's cursor, reporting declarations the way a typed driver does.

    Values for a declared column are stored as TEXT in SQLite and returned as
    `Decimal`, exactly what oracledb/psycopg2 hand back for NUMERIC.
    """

    def __init__(self, raw, declared, keep_ints=()):
        self._raw = raw
        self._declared = declared
        self._keep_ints = set(keep_ints)

    @property
    def description(self):
        if self._raw.description is None:
            return None
        return [(d[0], None, None, None, *self._declared.get(d[0], (None, None)), None)
                for d in self._raw.description]

    def _row(self, row):
        names = [d[0] for d in self._raw.description]
        return tuple(Decimal(v) if v is not None and n in self._declared
                     and n not in self._keep_ints else v
                     for n, v in zip(names, row, strict=True))

    def fetchone(self):
        r = self._raw.fetchone()
        return None if r is None else self._row(r)

    def fetchmany(self, n=None):
        return [self._row(r) for r in self._raw.fetchmany(n or self._raw.arraysize)]

    def fetchall(self):
        return [self._row(r) for r in self._raw.fetchall()]

    def __getattr__(self, name):
        return getattr(self._raw, name)


class _TypedDriverConnection:
    def __init__(self, raw, declared, keep_ints=()):
        self._raw = raw
        self._declared = declared
        self._keep_ints = keep_ints

    def cursor(self, *a, **kw):
        return _TypedDriverCursor(self._raw.cursor(*a, **kw), self._declared, self._keep_ints)

    def __getattr__(self, name):
        return getattr(self._raw, name)


class _SqlAlchemyClient:
    """A client that speaks SQLAlchemy, as most production clients do."""

    EXTRACTION_DIALECT = "sqlite"

    def __init__(self, engine):
        self._engine = engine

    def connect(self):
        return self._engine.connect()


def _sqlalchemy_client(rows, declared, *, normalize_names=True, keep_ints=()):
    """`rows` go into table t(id, amount TEXT); `declared` is {DRIVER_NAME: (p, s)}.

    A declared column comes back as `Decimal`, except those in `keep_ints`,
    which keep SQLite's int, as Oracle's NUMBER(p,0) does through SQLAlchemy."""
    raw = sqlite3.connect(":memory:", check_same_thread=False)
    raw.execute("CREATE TABLE t (id INTEGER, amount TEXT, other TEXT)")
    raw.executemany("INSERT INTO t VALUES (?, ?, ?)",
                    [(i, None if v is None else str(v), None if o is None else str(o))
                     for i, v, o in rows])
    raw.commit()
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           creator=lambda: _TypedDriverConnection(raw, declared, keep_ints))
    # Oracle and Snowflake report unquoted names in upper case and SQLAlchemy
    # folds them (AMOUNT -> amount) in result.keys(); the description keeps
    # the driver's spelling. This is that same dialect switch.
    engine.dialect.requires_name_normalize = normalize_names
    return _SqlAlchemyClient(engine)


# --------------------------------------------------------------------------
# Running an extraction and reading back what was stored
# --------------------------------------------------------------------------

@pytest.fixture
def extract(tmp_path):
    """Returns (result, rows ordered by the first column, [(stored name, type)])."""
    def run(client, rel="t", sql="SELECT * FROM t"):
        with patch.object(artifacts, "_ARTIFACT_ROOT", tmp_path / "fast"):
            res = extractor.extract_to_artifact(client, sql, rel, "conn")
            con = artifacts.connect_encrypted(res.artifact_path, res.artifact_key, read_only=True)
            try:
                # The data columns only: the loader also stores its own
                # `_bow_run_id` / `_bow_loaded_at` on every row.
                types = con.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    f"WHERE table_name = '{rel}' AND column_name NOT LIKE '\\_bow\\_%' ESCAPE '\\' "
                    "ORDER BY ordinal_position"
                ).fetchall()
                cols = ", ".join('"' + n.replace('"', '""') + '"' for n, _ in types)
                rows = con.execute(f'SELECT {cols} FROM "{rel}" ORDER BY 1').fetchall()
            finally:
                con.close()
        return res, rows, types
    return run


def _one_column(batches):
    """Rows (id, value) for a single value column split into the given batches."""
    out, i = [], 0
    for b in batches:
        out.append([(i + j, v) for j, v in enumerate(b)])
        i += len(b)
    return out


def _flat(batches):
    return [r for b in batches for r in b]


def _assert_exact(source_rows, stored_rows):
    assert len(stored_rows) == len(source_rows)
    for src, dst in zip(source_rows, stored_rows, strict=True):
        assert len(dst) == len(src)
        for s, d in zip(src, dst, strict=True):
            if s is None:
                assert d is None, (src, dst)
            else:
                assert d is not None and Decimal(str(d)) == Decimal(str(s)), (src, dst)


def _matches(dtype: str, stored: str) -> bool:
    """Does the type reported in res.columns describe the stored column?"""
    if dtype.startswith("decimal128("):
        p, s = (int(x) for x in dtype[len("decimal128("):-1].split(","))
        return stored == f"DECIMAL({p},{s})"
    # A column NULL in every row is stored INTEGER and reported as such (int32).
    return {"double": "DOUBLE", "int64": "BIGINT", "string": "VARCHAR",
            "null": "INTEGER", "int32": "INTEGER"}.get(dtype) == stored


def _assert_columns_match(res, types):
    assert len(res.columns) == len(types)
    for col, (_, stored) in zip(res.columns, types, strict=True):
        assert _matches(col["dtype"], stored), (col, stored)


# --------------------------------------------------------------------------
# A declared precision is the column's type, whatever the values are
# --------------------------------------------------------------------------

@pytest.mark.parametrize("precision,scale,first,later", [
    (12, 2, Decimal("12.34"), [Decimal("123456.78"), Decimal("-9999999999.99")]),
    (20, 10, Decimal("-0.0000000001"), [Decimal("-1234567890.1234567890"),
                                       Decimal("9999999999.9999999999")]),
    (9, 0, Decimal("7"), [Decimal("987654321")]),
    (38, 10, Decimal("1.5"), [Decimal("1234567890123456789012345678.0123456789")]),
    (38, 0, Decimal("1"), [Decimal("12345678901234567890123456789012345678")]),
])
def test_a_declared_decimal_holds_later_batches_with_larger_values(extract, precision, scale,
                                                                  first, later):
    batches = _one_column([[first] * 3, later])
    res, stored, types = extract(_Client([_desc("id"), _desc("amount", precision, scale)],
                                         batches=batches))
    _assert_exact(_flat(batches), stored)
    assert types[1] == ("amount", f"DECIMAL({precision},{scale})")
    _assert_columns_match(res, types)


def test_a_declared_decimal_holds_a_later_batch_at_the_real_batch_size(extract):
    rows = [(i, Decimal("12.34")) for i in range(BATCH)] + [(BATCH, Decimal("1234567.89"))]
    _, stored, types = extract(_Client([_desc("id"), _desc("amount", 12, 2)], rows=rows))
    _assert_exact(rows, stored)
    assert types[1] == ("amount", "DECIMAL(12,2)")


def test_a_declared_decimal_keeps_its_type_across_refreshes(extract):
    """The column's type follows the source's declaration, not this refresh's data."""
    desc = [_desc("id"), _desc("amount", 12, 2)]
    refreshes = {
        "small": [[(1, Decimal("5.00"))]],
        "large": [[(1, Decimal("1234567.89"))]],
        "all_null": [[(1, None), (2, None)]],
        "empty": [],
    }
    for rel, batches in refreshes.items():
        res, stored, types = extract(_Client(desc, batches=batches), rel=rel)
        _assert_exact(_flat(batches), stored)
        assert types[1] == ("amount", "DECIMAL(12,2)"), rel
        _assert_columns_match(res, types)


@pytest.mark.parametrize("precision,scale", [
    (39, 2), (5, 7), (5, -2), ORACLE_BARE_NUMBER, (0, 0), (None, None), (12, None),
    (65535, 65535),
])
def test_an_unusable_declaration_is_ignored_and_values_stay_exact(extract, precision, scale):
    batches = _one_column([[Decimal("1500")], [Decimal("2.5"), Decimal("-98765.4321")]])
    res, stored, types = extract(_Client([_desc("id"), _desc("amount", precision, scale)],
                                         batches=batches))
    _assert_exact(_flat(batches), stored)
    _assert_columns_match(res, types)


# --------------------------------------------------------------------------
# Undeclared decimals (bare NUMERIC, AVG()) widen instead of failing or rounding
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first,later", [
    (Decimal("1.5"), [Decimal("98765.4321"), Decimal("0.000123")]),   # more digits, more scale
    (Decimal("-3"), [Decimal("-2.71828")]),                          # scale only
    (Decimal("0.25"), [Decimal("12345678901234567890.5")]),          # many integer digits
])
def test_an_undeclared_decimal_stores_every_later_batch_exactly(extract, first, later):
    batches = _one_column([[first] * 3, later])
    # psycopg2 reports a bare NUMERIC as precision/scale 65535; others send None.
    for desc in ([_desc("id"), _desc("amount", 65535, 65535)], [("id", int), ("amount", Decimal)]):
        res, stored, types = extract(_Client(desc, batches=batches))
        _assert_exact(_flat(batches), stored)
        _assert_columns_match(res, types)


def test_a_column_null_for_the_whole_first_batch_keeps_later_values(extract):
    batches = _one_column([[None] * 3, [Decimal("4321.09"), Decimal("-0.5")]])
    for desc in ([_desc("id"), _desc("amount", 12, 2)], [("id", int), ("amount", Decimal)]):
        res, stored, types = extract(_Client(desc, batches=batches))
        _assert_exact(_flat(batches), stored)
        assert "decimal" in res.columns[1]["dtype"]
        _assert_columns_match(res, types)


@pytest.mark.parametrize("batches", [
    [[Decimal("1.00")] * 3, [Decimal("1.005"), Decimal("123456789012.5")]],
    [[Decimal("1.005")], [Decimal("1.00")]],
])
def test_a_value_wider_than_its_declaration_is_still_stored_exactly(extract, batches):
    """A driver that under-reports a declaration must not cost precision."""
    batches = _one_column(batches)
    res, stored, types = extract(_Client([_desc("id"), _desc("amount", 12, 2)], batches=batches))
    _assert_exact(_flat(batches), stored)
    _assert_columns_match(res, types)


# --------------------------------------------------------------------------
# Integers and decimals in one column (Oracle's bare NUMBER: int when whole)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("values", [
    [[1, 2], [Decimal("2.71828")]],
    [[Decimal("1.5")], [100]],
    [[Decimal("1.5"), 100]],
    [[100, Decimal("1.5"), 123456]],
    [[None], [5], [Decimal("2.5")]],
    [[7], [Decimal("0.12345678901234567")]],
    [[-9223372036854775808, 9223372036854775807], [Decimal("0.5")]],
])
def test_integers_and_decimals_in_one_column_are_stored_exactly(extract, values):
    batches = _one_column(values)
    for desc in ([_desc("id"), _desc("amount", *ORACLE_BARE_NUMBER)],
                 [("id", int), ("amount", Decimal)]):
        res, stored, types = extract(_Client(desc, batches=batches))
        _assert_exact(_flat(batches), stored)
        _assert_columns_match(res, types)


def test_integers_in_a_declared_decimal_column_take_the_declared_type(extract):
    batches = _one_column([[1, 2], [Decimal("2.71828"), 3]])
    res, stored, types = extract(_Client([_desc("id"), _desc("amount", 12, 5)], batches=batches))
    _assert_exact(_flat(batches), stored)
    assert types[1] == ("amount", "DECIMAL(12,5)")
    _assert_columns_match(res, types)


def test_an_integer_column_that_later_gets_a_float_keeps_the_fraction(extract):
    batches = _one_column([[1, 2], [2.75]])
    res, stored, types = extract(_Client([("id", int), ("amount", float)], batches=batches))
    assert [r[1] for r in stored] == [1, 2, 2.75]
    assert types[1] == ("amount", "DOUBLE")
    _assert_columns_match(res, types)


# --------------------------------------------------------------------------
# Past 38 digits there is no exact type: the column becomes DOUBLE, loudly
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first,later", [
    # Neither value is over 38 digits; holding both needs 23 + 20 = 43.
    (Decimal("0.12345678901234567890"), Decimal("12345678901234567890123.5")),
    (Decimal("0.123456789"), Decimal("1234567890123456789012345678901234567.25")),
])
def test_a_column_that_outgrows_38_digits_becomes_double_with_a_warning(extract, caplog,
                                                                         monkeypatch,
                                                                         first, later):
    # alembic's fileConfig disables every logger that exists when an earlier test
    # in the worker runs migrations, so the warning would reach no handler.
    # The loader reconciles each batch against the stored table, so it logs this.
    monkeypatch.setattr(loader.logger, "disabled", False)
    batches = _one_column([[first], [later]])
    with caplog.at_level(logging.INFO, logger=loader.logger.name):
        res, stored, types = extract(_Client([_desc("id"), _desc("amount", 65535, 65535)],
                                             batches=batches))
    assert types[1] == ("amount", "DOUBLE")
    _assert_columns_match(res, types)
    # Every row is kept, the earlier ones now approximate.
    for (_, src), (_, dst) in zip(_flat(batches), stored, strict=True):
        assert math.isclose(dst, float(src), rel_tol=1e-15)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                and r.getMessage() == "extraction.column_became_approximate"]
    assert len(warnings) == 1
    assert warnings[0].column == "amount"


# --------------------------------------------------------------------------
# Columns whose names differ only in case
# --------------------------------------------------------------------------

def test_case_colliding_columns_are_each_widened_in_place(extract):
    """DuckDB names are case-insensitive: it stores `Amount`, `amount` as
    `Amount`, `amount_1`. Widening must alter the column it means."""
    desc = [_desc("id"), _desc("Amount", 65535, 65535), _desc("amount")]
    batches = [
        [(1, Decimal("1.5"), None)],
        [(2, Decimal("2.5"), 7)],
        [(3, Decimal("123.456"), Decimal("0.25"))],
    ]
    res, stored, types = extract(_Client(desc, batches=batches))
    _assert_exact(_flat(batches), stored)
    _assert_columns_match(res, types)
    # The reported names are the ones a query against the artifact uses.
    assert [c["name"] for c in res.columns] == [name for name, _ in types]


# --------------------------------------------------------------------------
# The SQLAlchemy source, through a real engine (runs in every CI unit job)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("normalize_names", [True, False])
def test_the_sqlalchemy_source_applies_declarations_across_batches(extract, normalize_names):
    rows = [(i, Decimal("12.34"), None) for i in range(5)] + [(5, Decimal("123456.78"), None)]
    client = _sqlalchemy_client(rows, {"AMOUNT": (12, 2)}, normalize_names=normalize_names)
    with patch.object(extractor, "BATCH_ROWS", 2):   # several batches from six rows
        res, stored, types = extract(client, sql="SELECT id AS ID, amount AS AMOUNT FROM t")
    _assert_exact([r[:2] for r in rows], stored)
    assert types[1][1] == "DECIMAL(12,2)"
    _assert_columns_match(res, types)


def test_the_sqlalchemy_source_declares_an_empty_or_all_null_result(extract):
    for rel, rows in (("empty", []), ("nulls", [(1, None, None), (2, None, None)])):
        client = _sqlalchemy_client(rows, {"AMOUNT": (12, 2)})
        res, stored, types = extract(client, rel=rel,
                                     sql="SELECT id AS ID, amount AS AMOUNT FROM t")
        assert len(stored) == len(rows)
        assert types[1][1] == "DECIMAL(12,2)", rel
        assert res.columns[1]["dtype"] == "decimal128(12, 2)"


def test_the_sqlalchemy_source_keeps_columns_whose_normalized_names_collide(extract):
    """`AMOUNT` and `"amount"` are different columns on Oracle; SQLAlchemy's
    keys for both are `amount`. Each keeps its own values and declaration."""
    rows = [(1, Decimal("1.50"), Decimal("0.125")), (2, Decimal("123456.78"), Decimal("9.5"))]
    client = _sqlalchemy_client(rows, {"AMOUNT": (12, 2), "amount": (20, 5)})
    res, stored, types = extract(
        client, sql='SELECT id AS ID, amount AS AMOUNT, other AS "amount" FROM t')
    _assert_exact(rows, stored)
    assert [t for _, t in types[1:]] == ["DECIMAL(12,2)", "DECIMAL(20,5)"]
    _assert_columns_match(res, types)
    assert [c["name"] for c in res.columns] == [name for name, _ in types]


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


# --------------------------------------------------------------------------
# Integer columns stay integers, whatever precision and scale the driver reports
# --------------------------------------------------------------------------

class _OracleNumber:
    """Stands in for oracledb's DB_TYPE_NUMBER: one type code for every NUMBER."""


# Descriptions of an integer column as real drivers report it. Each gives a
# usable (precision, 0) and hands back Python ints.
INTEGER_DECLARATIONS = {
    "mysql_bigint": ("value", 8, None, 20, 20, 0, False),        # PyMySQL: LONGLONG, display length
    "pyodbc_bigint": ("value", int, None, 19, 19, 0, True),      # pyodbc / SQL Server
    "oracle_number_19_0": ("value", _OracleNumber, 20, None, 19, 0, True),
    "snowflake_number_38_0": ("value", 0, None, None, 38, 0, False),   # FIXED, as every NUMBER
}

BEYOND_FLOAT = 9007199254740993   # 2**53 + 1: a float64 holds 9007199254740992


def _read_through_fast_client(res, rel, sql):
    from app.data_sources.fast.fast_client import FastQueryClient, FastRelation

    return FastQueryClient([FastRelation(name=rel, artifact_path=str(res.artifact_path),
                                         artifact_key=res.artifact_key,
                                         columns=res.columns)]).execute_query(sql)


@pytest.mark.parametrize("declaration", INTEGER_DECLARATIONS.values(),
                         ids=INTEGER_DECLARATIONS.keys())
def test_an_integer_column_declared_with_scale_zero_stays_an_exact_integer(extract,
                                                                         declaration):
    batches = _one_column([[BEYOND_FLOAT, -9223372036854775808],
                           [9223372036854775807, 42]])
    res, stored, types = extract(_Client([_desc("id"), declaration], batches=batches))
    assert [r[1] for r in stored] == [v for _, v in _flat(batches)]
    assert types[1] == ("value", "BIGINT")
    _assert_columns_match(res, types)
    # What a query against the custom table returns: still the exact integer.
    df = _read_through_fast_client(res, "t", "SELECT value FROM t ORDER BY id")
    assert str(df["value"].dtype) == "int64"
    assert [int(v) for v in df["value"]] == [v for _, v in _flat(batches)]


@pytest.mark.parametrize("declaration", INTEGER_DECLARATIONS.values(),
                         ids=INTEGER_DECLARATIONS.keys())
def test_an_integer_column_never_becomes_decimal_on_any_refresh(extract, declaration):
    refreshes = {
        "ints": [[(1, 5)], [(2, None), (3, BEYOND_FLOAT)]],
        "null_first": [[(1, None), (2, None)], [(3, BEYOND_FLOAT)]],
        "all_null": [[(1, None), (2, None)]],
        "empty": [],
    }
    for rel, batches in refreshes.items():
        res, stored, types = extract(_Client([_desc("id"), declaration], batches=batches),
                                     rel=rel)
        assert [r[1] for r in stored] == [v for _, v in _flat(batches)], rel
        assert not types[1][1].startswith("DECIMAL"), (rel, types)
        if any(v is not None for _, v in _flat(batches)):
            assert types[1][1] == "BIGINT", (rel, types)
        _assert_columns_match(res, types)


@pytest.mark.parametrize("batches", [
    [[1, 2], [10 ** 30 + 7]],
    [[10 ** 30 + 7], [5]],
    [[-(10 ** 37) - 1, 3]],
    [[1, 2], [Decimal("3"), Decimal("12345678901234567890123")]],
])
def test_integers_past_int64_in_a_scale_zero_column_stay_exact(extract, batches):
    """Oracle's NUMBER(38,0) returns Python ints of any size."""
    batches = _one_column(batches)
    res, stored, types = extract(_Client(
        [_desc("id"), ("value", _OracleNumber, 39, None, 38, 0, True)], batches=batches))
    _assert_exact(_flat(batches), stored)
    assert types[1][1].startswith("DECIMAL(") and types[1][1].endswith(",0)"), types
    _assert_columns_match(res, types)


def test_the_sqlalchemy_source_keeps_a_declared_integer_an_integer(extract):
    """Oracle's NUMBER(19,0) comes back from SQLAlchemy as int and is declared (19, 0)."""
    rows = [(BEYOND_FLOAT + i, Decimal("1.50"), None) for i in range(5)]
    client = _sqlalchemy_client(rows, {"ID": (19, 0), "AMOUNT": (12, 2)}, keep_ints={"ID"})
    with patch.object(extractor, "BATCH_ROWS", 2):
        res, stored, types = extract(client, sql="SELECT id AS ID, amount AS AMOUNT FROM t")
    assert [r[0] for r in stored] == [r[0] for r in rows]
    assert [t for _, t in types] == ["BIGINT", "DECIMAL(12,2)"]
    df = _read_through_fast_client(res, "t", "SELECT * FROM t ORDER BY 1")
    assert str(df.iloc[:, 0].dtype) == "int64"
    assert [int(v) for v in df.iloc[:, 0]] == [r[0] for r in rows]


# --------------------------------------------------------------------------
# Widening between integers and decimals sizes the integer side from its values
# --------------------------------------------------------------------------

@pytest.mark.parametrize("batches,stored_type", [
    # A later integer that fits the decimal already held changes nothing.
    ([[Decimal("1.12345678901234567890")], [1]], "DECIMAL(21,20)"),
    ([[Decimal("123.45")], [-999, 0]], "DECIMAL(5,2)"),
    # One that does not, widens by its own digits, not by int64's 19.
    ([[Decimal("1.5")], [123456789]], "DECIMAL(10,1)"),
    # Integers held first, then a decimal with a long fraction.
    ([[7, -3], [Decimal("0.12345678901234567890")]], "DECIMAL(21,20)"),
    ([[None], [12345], [Decimal("0.123456789012345678901234567890123")]], "DECIMAL(38,33)"),
])
def test_integers_meeting_decimals_widen_only_as_far_as_the_values_need(extract, batches,
                                                                       stored_type):
    batches = _one_column(batches)
    for desc in ([_desc("id"), _desc("value", 65535, 65535)], [("id", int), ("value", Decimal)]):
        res, stored, types = extract(_Client(desc, batches=batches))
        _assert_exact(_flat(batches), stored)
        assert types[1] == ("value", stored_type)
        _assert_columns_match(res, types)
