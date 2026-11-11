"""Incremental refresh: the connector-agnostic core, the normalizer and the
loader, with no application database and no source engine."""
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pyarrow as pa
import pytest

from app.data_sources.fast import artifacts, incremental, loader
from app.data_sources.fast.extractor import ExtractionAborted, PublishCheckFailed
from app.data_sources.fast.incremental import IncrementalConfigError, RefreshConfig, Window
from app.data_sources.fast.normalize import clean_names, rows_to_arrow

# -- typed cursor values ------------------------------------------------------

CURSOR_VALUES = [
    datetime(2026, 3, 1, 2, 0, 0, 123456),
    datetime(2026, 3, 1, 2, 0, tzinfo=timezone.utc),
    datetime(2026, 3, 1, 5, 0, tzinfo=timezone(timedelta(hours=3))),
    date(2024, 2, 29),
    0, -7, 2 ** 62, Decimal("12.50"),
    "2026-03-01 02:00:00",
]


@pytest.mark.parametrize("value", CURSOR_VALUES)
def test_a_checkpoint_survives_storage_unchanged(value):
    stored = json.loads(json.dumps(incremental.encode(value)))
    back = incremental.decode(stored)
    assert back == value
    assert incremental.kind_of(back) == incremental.kind_of(value)
    if isinstance(value, datetime):
        # Aware stays aware, naive stays naive: the two are never mixed.
        assert (back.tzinfo is None) == (value.tzinfo is None)


@pytest.mark.parametrize("junk", [None, True, float("nan"), b"x", {"a": 1}, object()])
def test_values_that_cannot_order_rows_are_not_cursors(junk):
    assert incremental.kind_of(junk) is None
    assert incremental.encode(junk) is None


@pytest.mark.parametrize("stored", [None, {}, {"kind": "timestamp"}, {"kind": "timestamp", "value": "soon"},
                                    {"kind": "number", "value": "abc"}, "2026-01-01"])
def test_an_unreadable_checkpoint_reads_as_none(stored):
    assert incremental.decode(stored) is None


# -- windows ------------------------------------------------------------------

@pytest.mark.parametrize("lo,hi,rows", [
    (datetime(2024, 1, 1), datetime(2026, 1, 1), 1_000_000),
    (date(2020, 1, 1), date(2026, 6, 30), 3_000_000),
    (1, 5_000_000, 5_000_000),
    (Decimal("0.5"), Decimal("900.25"), 800_000),
    (10, 13, 10_000_000),          # narrower than the number of chunks asked for
])
def test_chunks_partition_the_range(lo, hi, rows):
    windows = incremental.plan_chunks(lo, hi, rows)
    assert windows[0].lo is None and windows[-1].hi is None, "the ends are open"
    for left, right in zip(windows, windows[1:]):
        assert left.hi == right.lo, "no gap and no overlap between neighbours"
        assert left.hi is not None
    edges = [w.hi for w in windows[:-1]]
    assert edges == sorted(set(edges)), "boundaries strictly increase"
    assert all(lo <= e <= hi for e in edges)
    # Rows with no cursor value ride along exactly once.
    assert [w.include_nulls for w in windows].count(True) == 1 and windows[-1].include_nulls


@pytest.mark.parametrize("lo,hi,rows", [
    (None, None, 10), (5, 5, 10 ** 7), (1, 10 ** 6, 0), ("a", "z", 10 ** 7),
    (1, datetime(2026, 1, 1), 10 ** 7),
])
def test_a_range_that_cannot_be_cut_is_one_open_window(lo, hi, rows):
    assert incremental.plan_chunks(lo, hi, rows) == [Window(None, None, True)]


def test_lookback_is_applied_at_read_time_in_the_cursors_own_unit():
    config = RefreshConfig(mode="merge", cursor_column="c", unique_key=("id",), lookback_seconds=3600,
                           lookback_units=50)
    at = datetime(2026, 5, 5, 12, 0)
    assert incremental.incremental_window(at, config).lo == at - timedelta(hours=1)
    assert incremental.incremental_window(date(2026, 5, 5), config).lo == date(2026, 5, 4)
    assert incremental.incremental_window(1000, config).lo == 950
    assert incremental.incremental_window("2026-05-05", config).lo == "2026-05-05"
    # Open-ended, and rows without a cursor ride along.
    window = incremental.incremental_window(at, config)
    assert window.hi is None and window.include_nulls


def test_default_lookback_is_a_day_for_time_and_nothing_for_numbers():
    config = RefreshConfig(mode="append", cursor_column="c")
    at = datetime(2026, 5, 5, 12, 0)
    assert incremental.incremental_window(at, config).lo == at - timedelta(days=1)
    assert incremental.incremental_window(77, config).lo == 77


@pytest.mark.parametrize("grain,expected", [
    ("hour", datetime(2026, 5, 4, 11, 0)), ("day", datetime(2026, 5, 4)), ("month", datetime(2026, 5, 1)),
])
def test_a_grain_starts_the_window_on_a_whole_period(grain, expected):
    config = RefreshConfig(mode="append", cursor_column="c", grain=grain, lookback_seconds=86400)
    assert incremental.incremental_window(datetime(2026, 5, 5, 11, 47), config).lo == expected


def test_checkpoint_never_runs_ahead_of_now():
    now = datetime(2026, 11, 11, 12, 0)
    assert incremental.cap_checkpoint(datetime(2099, 1, 1), now) == now
    assert incremental.cap_checkpoint(datetime(2026, 1, 1), now) == datetime(2026, 1, 1)
    aware = incremental.cap_checkpoint(datetime(2099, 1, 1, tzinfo=timezone.utc), now)
    assert aware == now.replace(tzinfo=timezone.utc) and aware.tzinfo is not None
    assert incremental.cap_checkpoint(date(2099, 1, 1), now) == date(2026, 11, 12)
    assert incremental.cap_checkpoint(10 ** 12, now) == 10 ** 12
    assert incremental.cap_checkpoint(None, now) is None


# -- settings -----------------------------------------------------------------

@pytest.mark.parametrize("raw,sql,problem", [
    ({"mode": "sideways"}, "", "mode"),
    ({"mode": "merge", "unique_key": ["id"]}, "", "cursor"),
    ({"mode": "merge", "cursor_column": "u"}, "", "key"),
    ({"mode": "append", "cursor_column": "u", "unique_key": ["id"]}, "", "merge"),
    ({"mode": "append", "cursor_column": "u"}, "SELECT * FROM t WHERE u >= {{since}}", "type"),
    ({"mode": "append", "cursor_column": "u", "cursor_kind": "text"},
     "SELECT * FROM t WHERE u >= {{since}} AND u < {{until}}", "until"),
    ({"mode": "append", "cursor_column": "u", "grain": "week"}, "", "grain"),
    ({"mode": "append", "cursor_column": "u", "lookback_seconds": -1}, "", "negative"),
    ({"mode": "full", "row_drop_guard_pct": 0}, "", "guard"),
])
def test_settings_that_cannot_work_are_rejected_with_a_reason(raw, sql, problem):
    with pytest.raises(IncrementalConfigError) as error:
        RefreshConfig.from_dict(raw).validate(sql)
    assert problem.lower() in str(error.value).lower()


def test_settings_roundtrip_through_storage():
    config = RefreshConfig.from_dict({"mode": "merge", "cursor_column": "u", "unique_key": "a, b",
                                      "lookback_seconds": "60", "row_drop_guard_pct": 30})
    config.validate("")
    assert config.unique_key == ("a", "b")
    assert RefreshConfig.from_dict({**config.to_dict(), "mode": config.mode}) == config


def test_definition_hash_changes_with_what_decides_the_rows_and_only_that():
    base = RefreshConfig.from_dict({"mode": "merge", "cursor_column": "u", "unique_key": ["id"]})
    h = incremental.definition_hash("SELECT 1", None, base)
    assert h == incremental.definition_hash("  SELECT 1  ", None, base)
    for changed in ({"cursor_column": "v"}, {"unique_key": ["x"]}, {"mode": "append", "unique_key": []},
                    {"initial_value": "2025-01-01"}):
        other = RefreshConfig.from_dict({**base.to_dict(), "mode": base.mode, **changed})
        assert incremental.definition_hash("SELECT 1", None, other) != h
    assert incremental.definition_hash("SELECT 2", None, base) != h
    assert incremental.definition_hash("SELECT 1", {"datasetId": "x"}, base) != h
    # How often it refreshes, or how far it looks back, changes no row.
    tuned = RefreshConfig.from_dict({**base.to_dict(), "mode": base.mode, "lookback_seconds": 5,
                                     "full_refresh_every_days": 1, "row_drop_guard_pct": 10})
    assert incremental.definition_hash("SELECT 1", None, tuned) == h


# -- writing a window into SQL --------------------------------------------------

FAMILIES = sorted({f for f in incremental._FAMILY.values()})


@pytest.mark.parametrize("dialect", sorted(incremental._FAMILY))
@pytest.mark.parametrize("value", CURSOR_VALUES)
def test_every_dialect_renders_every_cursor_kind(dialect, value):
    sql = incremental.render_sql("SELECT * FROM t;", dialect, "updated at", None, Window(lo=value))
    assert sql.startswith("SELECT * FROM (SELECT * FROM t) bow_sub WHERE ")
    assert ";" not in sql, "the wrapped query is one statement"
    assert ">=" in sql and "<" not in sql.replace(">=", "")


@pytest.mark.parametrize("dialect", ["postgres", "tsql", "sqlite", "mysql", "bigquery"])
def test_wrapped_window_is_half_open_and_carries_null_cursors(dialect):
    lo, hi = datetime(2026, 1, 1), datetime(2026, 2, 1)
    col = incremental.quote_identifier(incremental.family_of(dialect), "u")
    bounded = incremental.render_sql("SELECT * FROM t", dialect, "u", None, Window(lo, hi))
    assert f"{col} >= " in bounded and f"{col} < " in bounded and "IS NULL" not in bounded
    tail = incremental.render_sql("SELECT * FROM t", dialect, "u", None, Window(lo, None, include_nulls=True))
    assert tail.endswith(f"OR {col} IS NULL") and "(" in tail
    # Nothing to restrict: the admin's query runs exactly as written.
    assert incremental.render_sql("SELECT * FROM t ORDER BY 1", dialect, "u", None, Window()) == \
        "SELECT * FROM t ORDER BY 1"


def test_placeholders_are_filled_where_the_admin_put_them():
    sql = "SELECT id FROM orders WHERE updated_at >= {{since}} AND updated_at < {{ UNTIL }}"
    lo, hi = datetime(2026, 1, 1), datetime(2026, 2, 1)
    out = incremental.render_sql(sql, "postgres", "updated_at", "timestamp", Window(lo, hi))
    assert out == ("SELECT id FROM orders WHERE updated_at >= TIMESTAMP '2026-01-01 00:00:00.000000' "
                   "AND updated_at < TIMESTAMP '2026-02-01 00:00:00.000000'")
    assert "bow_sub" not in out


@pytest.mark.parametrize("dialect", sorted(incremental._FAMILY))
@pytest.mark.parametrize("kind", ["timestamp", "date", "number"])
def test_an_open_window_still_fills_both_placeholders(dialect, kind):
    sql = "SELECT * FROM t WHERE c >= {{since}} AND c < {{until}}"
    out = incremental.render_sql(sql, dialect, "c", kind, Window())
    assert "{{" not in out and "None" not in out


def test_sql_server_literals_are_typed_and_never_ansi_timestamp():
    naive = incremental.literal("tsql", datetime(2026, 3, 1, 2, 0, 0, 123456))
    assert naive == "CAST('2026-03-01T02:00:00.123456' AS datetime2)"
    aware = incremental.literal("tsql", datetime(2026, 3, 1, 5, 0, tzinfo=timezone(timedelta(hours=3))))
    assert aware == "CAST('2026-03-01T02:00:00.000000+00:00' AS datetimeoffset)"
    assert incremental.literal("tsql", date(2026, 3, 1)) == "CAST('2026-03-01' AS date)"
    assert incremental.quote_identifier("tsql", "weird]name") == "[weird]]name]"


@pytest.mark.parametrize("hostile", ["x' OR '1'='1", "a';DROP TABLE t;--", "back\\slash", 'q"uote', "new\nline"])
def test_text_that_could_end_a_literal_is_refused_not_escaped(hostile):
    with pytest.raises(IncrementalConfigError):
        incremental.render_sql("SELECT * FROM t", "postgres", "c", None, Window(lo=hostile))


def test_a_connector_without_a_dialect_cannot_be_windowed():
    with pytest.raises(IncrementalConfigError):
        incremental.render_sql("SELECT 1", None, "c", None, Window(lo=1))
    assert incremental.probe_sql("SELECT 1", "klingon", "c", None) is None


def test_probe_measures_the_whole_result_in_one_statement():
    probe = incremental.probe_sql("SELECT * FROM t;", "tsql", "updated_at", None)
    assert probe == ("SELECT MIN([updated_at]), MAX([updated_at]), COUNT(*), COUNT([updated_at]) "
                     "FROM (SELECT * FROM t) bow_sub")


# -- JSON-request connectors ----------------------------------------------------

def test_a_json_request_gets_the_window_in_its_own_fields():
    decl = {"start": "start_time", "end": "end_time", "format": "epoch_ms", "clears": ["duration_in_mins"]}
    spec = json.dumps({"table": "metrics", "stat_key": ["cpu|usage"], "duration_in_mins": 60})
    lo = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    body = json.loads(incremental.render_json(spec, decl, Window(lo=lo), now_utc=now))
    assert body == {"table": "metrics", "stat_key": ["cpu|usage"],
                    "start_time": 1767225600000, "end_time": 1767312000000}
    # A naive value is taken as UTC, the same instant.
    naive = json.loads(incremental.render_json(spec, decl, Window(lo=datetime(2026, 1, 1)), now_utc=now))
    assert naive["start_time"] == body["start_time"]
    # No window: the admin's request is sent as they wrote it.
    assert json.loads(incremental.render_json(spec, decl, Window())) == json.loads(spec)


def test_a_json_connector_rejects_a_query_that_is_not_an_object():
    with pytest.raises(IncrementalConfigError):
        incremental.render_json("SELECT 1", {"start": "from"}, Window(lo=1))


# -- one shape for every source's rows -------------------------------------------

def test_result_column_names_become_table_column_names_without_losing_any():
    names = clean_names(["id", "ID", "", None, "id", "_bow_run_id", " total "])
    assert len(names) == 7 and len({n.lower() for n in names}) == 7
    assert names[0] == "id" and names[6] == "total"
    assert not any(n.lower().startswith("_bow_") for n in names)


def test_two_columns_sharing_a_name_are_both_kept():
    table = rows_to_arrow(["id", "id"], [(1, "a"), (2, "b")])
    assert table.num_columns == 2
    assert table.column(0).to_pylist() == [1, 2] and table.column(1).to_pylist() == ["a", "b"]


def test_values_arrow_cannot_type_are_stored_rather_than_failing_the_load():
    ref = uuid.uuid4()
    table = rows_to_arrow(
        ["obj", "mixed", "ref", "raw", "wide", "plain"],
        [({"a": 1}, 1, ref, memoryview(b"\x01"), Decimal("1" * 45), 1),
         ([1, {"b": 2}], "two", None, None, None, None),
         (None, None, ref, memoryview(b"\x02"), Decimal("2"), 3)],
    )
    assert json.loads(table.column("obj")[0].as_py()) == {"a": 1}
    assert json.loads(table.column("obj")[1].as_py()) == [1, {"b": 2}]
    assert table.column("mixed").to_pylist() == ["1", "two", None]
    assert table.column("ref").to_pylist() == [str(ref), None, str(ref)]
    assert table.column("raw").to_pylist() == [b"\x01", None, b"\x02"]
    assert pa.types.is_floating(table.schema.field("wide").type)
    assert table.column("plain").to_pylist() == [1, None, 3]


# -- the loader -------------------------------------------------------------------

@pytest.fixture
def volume(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "_ARTIFACT_ROOT", tmp_path / "fast")
    return tmp_path / "fast"


class Batches:
    """A source that returns exactly the batches it was given."""

    def __init__(self, *batches):
        self.batches = batches

    def stream_batches(self, sql, batch_rows):
        yield from self.batches


def limits(**over):
    import time
    return loader.Limits(**{"max_rows": 10 ** 6, "max_bytes": 10 ** 9, "deadline": time.monotonic() + 60,
                            "max_seconds": 60, **over})


def read(outcome, sql):
    con = artifacts.connect_encrypted(outcome.artifact_path, outcome.artifact_key, read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def build(volume, *batches, config=None, relation="t"):
    part, key = loader.new_part("conn")
    loader.load_window(Batches(*batches), "q", part, key, relation, "run-1", limits(), batch_rows=10)
    return loader.finalize(part, key, relation, config or RefreshConfig(), run_id="run-1")


def test_a_column_empty_in_the_first_batch_takes_the_type_that_arrives_later(volume):
    """The first rows of a real table are often its oldest — and its sparsest."""
    first = pa.table({"id": [1, 2], "note": pa.array([None, None], type=pa.null())})
    later = pa.table({"id": [3], "note": ["finally"]})
    outcome = build(volume, first, later)
    assert read(outcome, "SELECT id, note FROM t ORDER BY id") == [(1, None), (2, None), (3, "finally")]
    assert {c["name"]: c["dtype"] for c in outcome.columns}["note"] == "string"


def test_a_number_that_outgrows_the_first_batch_is_widened_not_truncated(volume):
    small = rows_to_arrow(["amount", "qty"], [(Decimal("1.50"), 1)])
    large = rows_to_arrow(["amount", "qty"], [(Decimal("987654321098.765"), 2.5)])
    outcome = build(volume, small, large)
    assert read(outcome, "SELECT amount, qty FROM t ORDER BY amount") == [
        (Decimal("1.500"), 1.0), (Decimal("987654321098.765"), 2.5)]


def test_objects_whose_shape_changes_between_batches_are_all_kept(volume):
    a = pa.table({"id": [1], "payload": [{"source": "web"}]})
    b = pa.table({"id": [2], "payload": [{"store": {"id": 7}}]})
    outcome = build(volume, a, b)
    rows = dict(read(outcome, "SELECT id, payload FROM t"))
    assert json.loads(rows[1]) == {"source": "web"} and json.loads(rows[2]) == {"store": {"id": 7}}


def test_every_row_records_the_run_that_wrote_it_and_the_column_list_does_not(volume):
    outcome = build(volume, pa.table({"id": [1, 2]}))
    assert [c["name"] for c in outcome.columns] == ["id"]
    assert read(outcome, "SELECT DISTINCT _bow_run_id, _bow_loaded_at IS NOT NULL FROM t") == [("run-1", True)]
    assert outcome.row_count == 2 and len(outcome.schema_fingerprint) == 16


def test_an_aborted_window_leaves_the_part_file_as_it_was(volume):
    part, key = loader.new_part("conn")
    loader.load_window(Batches(pa.table({"id": [1, 2]})), "q", part, key, "t", "r1", limits(), batch_rows=10)

    def failing():
        yield pa.table({"id": [3]})
        raise RuntimeError("source went away")

    class Failing:
        def stream_batches(self, sql, batch_rows):
            return failing()

    with pytest.raises(RuntimeError):
        loader.load_window(Failing(), "q", part, key, "t", "r2", limits(), batch_rows=10, rows_before=2)
    with pytest.raises(ExtractionAborted):
        loader.load_window(Batches(pa.table({"id": [4]})), "q", part, key, "t", "r3",
                           limits(should_abort=lambda: True), batch_rows=10, rows_before=2)
    with pytest.raises(ExtractionAborted):
        loader.load_window(Batches(pa.table({"id": [5, 6]})), "q", part, key, "t", "r4",
                           limits(max_rows=3), batch_rows=10, rows_before=2)
    outcome = loader.finalize(part, key, "t", RefreshConfig(), run_id="r1")
    assert read(outcome, "SELECT id FROM t ORDER BY id") == [(1,), (2,)]


def merge_config(**over):
    return RefreshConfig.from_dict({"mode": "merge", "cursor_column": "seq", "unique_key": ["id"], **over})


def increment(volume, previous, config, lo, *batches, wrapped=True, previous_rows=None):
    return loader.apply_increment(
        Batches(*batches), "q", previous.artifact_path, previous.artifact_key, "t", "conn", "run-2",
        config, Window(lo=lo, include_nulls=True), limits(), batch_rows=10,
        previous_rows=previous_rows, wrapped=wrapped,
    )


def test_merge_upserts_by_key_and_leaves_unchanged_rows_alone(volume):
    base = build(volume, pa.table({"id": [1, 2, 3], "v": ["a", "b", "c"], "seq": [1, 2, 3]}),
                 config=merge_config())
    delta = pa.table({"id": [3, 2, 4, 4], "v": ["c", "B", "old", "new"], "seq": [3, 9, 5, 6]})
    outcome = increment(volume, base, merge_config(), 3, delta)
    assert read(outcome, "SELECT id, v, seq FROM t ORDER BY id") == [
        (1, "a", 1), (2, "B", 9), (3, "c", 3), (4, "new", 6)]
    assert (outcome.rows_read, outcome.rows_inserted, outcome.rows_updated) == (4, 1, 1)
    assert outcome.collapsed_duplicates == 1 and outcome.max_cursor == 9
    # Row 3 was re-read but did not change: it still belongs to the run that wrote it.
    assert dict(read(outcome, "SELECT id, _bow_run_id FROM t")) == {1: "run-1", 2: "run-2", 3: "run-1", 4: "run-2"}
    # The previous artifact is untouched.
    assert read(base, "SELECT count(*), max(seq) FROM t") == [(3, 3)]


def test_merge_matches_rows_whose_key_is_null(volume):
    base = build(volume, pa.table({"id": [None, 1], "v": ["x", "y"], "seq": [1, 1]}), config=merge_config())
    outcome = increment(volume, base, merge_config(), 1,
                        pa.table({"id": pa.array([None], type=pa.int64()), "v": ["x2"], "seq": [2]}))
    assert sorted(read(outcome, "SELECT id, v FROM t"), key=str) == [(1, "y"), (None, "x2")]


def append_config(**over):
    return RefreshConfig.from_dict({"mode": "append", "cursor_column": "seq", **over})


def test_append_replaces_the_tail_so_reading_a_window_twice_changes_nothing(volume):
    base = build(volume, pa.table({"v": ["a", "b", "b", "n"], "seq": [1, 5, 5, None]}),
                 config=append_config())
    delta = pa.table({"v": ["b", "b", "c", "n"], "seq": [5, 5, 6, None]})
    once = increment(volume, base, append_config(), 5, delta)
    twice = increment(volume, once, append_config(), 5, delta)
    expected = [("a", 1), ("b", 5), ("b", 5), ("c", 6), ("n", None)]
    for outcome in (once, twice):
        assert sorted(read(outcome, "SELECT v, seq FROM t"), key=str) == sorted(expected, key=str)
    assert (once.rows_inserted, once.rows_deleted) == (4, 3)
    assert once.null_cursor_rows == 1 and once.max_cursor == 6


def test_append_keeps_rows_without_a_cursor_when_the_query_cannot_return_them(volume):
    """A query that places its own filter never returns NULL-cursor rows, so
    their absence from a window says nothing about the source."""
    base = build(volume, pa.table({"v": ["a", "n"], "seq": [1, None]}), config=append_config())
    outcome = increment(volume, base, append_config(), 1, pa.table({"v": ["a", "b"], "seq": [1, 2]}),
                        wrapped=False)
    assert sorted(read(outcome, "SELECT v FROM t")) == [("a",), ("b",), ("n",)]


def test_an_increment_with_a_new_column_is_refused_so_the_table_can_be_rebuilt(volume):
    base = build(volume, pa.table({"id": [1], "seq": [1]}), config=merge_config())
    with pytest.raises(loader.SchemaChanged):
        increment(volume, base, merge_config(), 1, pa.table({"id": [2], "seq": [2], "extra": ["x"]}))
    assert not list(volume.rglob("*.tmp")), "the refused file is not left behind"


def test_an_increment_may_widen_a_type_and_omit_a_column(volume):
    base = build(volume, pa.table({"id": [1], "amount": [10], "note": ["n"], "seq": [1]}),
                 config=merge_config())
    outcome = increment(volume, base, merge_config(), 1, pa.table({"id": [2], "amount": [2.5], "seq": [2]}))
    assert read(outcome, "SELECT id, amount, note FROM t ORDER BY id") == [(1, 10.0, "n"), (2, 2.5, None)]


def test_an_artifact_written_before_run_lineage_can_still_be_incremented(volume):
    part, key = loader.new_part("conn")
    con = artifacts.connect_encrypted(part, key)
    con.execute("CREATE TABLE t AS SELECT 1 AS id, 1 AS seq")
    con.close()
    final = part.with_suffix(".db")
    part.replace(final)

    class Old:
        artifact_path, artifact_key = str(final), key

    outcome = increment(volume, Old, merge_config(), 1, pa.table({"id": [2], "seq": [2]}))
    assert read(outcome, "SELECT id, _bow_run_id FROM t ORDER BY id") == [(1, None), (2, "run-2")]


@pytest.mark.parametrize("missing", ["cursor", "key"])
def test_a_cursor_or_key_that_is_not_in_the_result_is_named(volume, missing):
    config = merge_config(**({"cursor_column": "nope"} if missing == "cursor" else {"unique_key": ["nope"]}))
    with pytest.raises(IncrementalConfigError) as error:
        build(volume, pa.table({"id": [1], "seq": [1]}), config=config)
    assert "nope" in str(error.value) and "seq" in str(error.value)
    assert not list(volume.rglob("*.db"))


def test_a_first_load_keeps_one_row_per_key_and_says_how_many_it_collapsed(volume):
    outcome = build(volume, pa.table({"id": [1, 1, 2], "v": ["old", "new", "x"], "seq": [1, 2, 1]}),
                    config=merge_config())
    assert read(outcome, "SELECT id, v FROM t ORDER BY id") == [(1, "new"), (2, "x")]
    assert outcome.collapsed_duplicates == 1 and any("collapsed" in n for n in outcome.notes)


@pytest.mark.parametrize("previous,now,guard,blocked", [
    (1000, 400, 50, True), (1000, 500, 50, False), (1000, 0, 50, True),
    (1000, 10, None, False),     # guard off
    (10, 1, 50, False),          # too small to mean anything
    (None, 0, 50, False),        # nothing to compare with
])
def test_row_drop_guard(volume, previous, now, guard, blocked):
    config = RefreshConfig.from_dict({"mode": "full", "row_drop_guard_pct": guard})
    part, key = loader.new_part("conn")
    loader.load_window(Batches(pa.table({"id": list(range(now))})), "q", part, key, "t", "r", limits(),
                       batch_rows=10)
    if blocked:
        with pytest.raises(PublishCheckFailed) as error:
            loader.finalize(part, key, "t", config, previous_rows=previous, run_id="r")
        assert f"{previous:,}" in str(error.value)
    else:
        assert loader.finalize(part, key, "t", config, previous_rows=previous, run_id="r").row_count == now
