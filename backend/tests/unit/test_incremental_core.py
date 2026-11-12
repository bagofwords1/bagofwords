"""Incremental refresh: the connector-agnostic core, the normalizer and the
loader, with no application database and no source engine."""
import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
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
    datetime(2026, 3, 1, 2, 0, tzinfo=UTC),
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


@pytest.mark.parametrize("kind,raw,expected", [
    (None, "1000", 1000), (None, "12.5", Decimal("12.5")), (None, "2024-01-01", datetime(2024, 1, 1)),
    (None, "2024-01-01T08:30:00Z", datetime(2024, 1, 1, 8, 30, tzinfo=timezone.utc)), (None, "A-17", "A-17"),
    ("timestamp", "2024-01-01", datetime(2024, 1, 1)), ("date", "2024-01-01", date(2024, 1, 1)),
    ("number", "0", 0), ("text", "2024", "2024"), (None, "", None), ("number", None, None),
])
def test_a_start_value_is_read_as_what_it_looks_like_unless_told_otherwise(kind, raw, expected):
    assert incremental.parse_initial(kind, raw) == expected


@pytest.mark.parametrize("kind,raw", [("number", "soon"), ("timestamp", "41 o'clock"), ("date", "yesterday")])
def test_a_start_value_that_contradicts_the_chosen_type_is_refused(kind, raw):
    with pytest.raises(IncrementalConfigError):
        incremental.parse_initial(kind, raw)


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


def _density_plan(values, target, **kwargs):
    values = sorted(values)
    probes = []

    def count_between(a, b):
        probes.append((a, b))
        return sum(1 for v in values if a <= v < b)

    windows = incremental.plan_by_density(values[0], values[-1], len(values), count_between,
                                          target_rows=target, **kwargs)
    sizes = []
    for w in windows:
        sizes.append(sum(1 for v in values if (w.lo is None or v >= w.lo) and (w.hi is None or v < w.hi)))
    return windows, sizes, probes


@pytest.mark.parametrize("values,target", [
    # Two outliers a century apart, everything else inside two months.
    ([datetime(1970, 1, 1), datetime(2099, 1, 1)]
     + [datetime(2026, 9, 1) + timedelta(minutes=7 * i) for i in range(12_000)], 1_000),
    # Most rows recent: a curve, not a line.
    ([int(1_000_000 * (1 - (1 - i / 20_000) ** 3)) for i in range(20_000)], 1_500),
    # Evenly spread: nothing special to find.
    (list(range(0, 50_000, 5)), 1_000),
    # Dates, with a gap of years in the middle.
    ([date(2015, 1, 1) + timedelta(days=i % 300) for i in range(3_000)]
     + [date(2026, 1, 1) + timedelta(days=i % 200) for i in range(9_000)], 800),
])
def test_chunks_follow_where_the_rows_are_not_how_wide_the_range_is(values, target):
    windows, sizes, probes = _density_plan(values, target)
    assert sum(sizes) == len(values), "every row is in exactly one window"
    assert windows[0].lo is None and windows[-1].hi is None
    for left, right in zip(windows, windows[1:]):
        assert left.hi == right.lo
    assert len(windows) > 1 and max(sizes) <= 2 * target, f"a window holds {max(sizes)} rows"
    # No chunk is wasted on emptiness: almost-empty neighbours were merged.
    assert sum(1 for n in sizes if n < target / 10) <= 2
    assert len(probes) <= 64
    assert [w.include_nulls for w in windows].count(True) == 1 and windows[-1].include_nulls


def test_rows_that_share_one_cursor_value_stay_in_one_window():
    values = [5] * 5_000 + list(range(6, 1_006))
    windows, sizes, _probes = _density_plan(values, 500)
    assert sum(sizes) == len(values) and max(sizes) >= 5_000, "a tie cannot be cut, and is not lost"


@pytest.mark.parametrize("values,target", [([1, 2, 3], 100), ([7] * 50, 10), (["a", "b"] * 50, 10)])
def test_a_small_or_uncuttable_load_is_one_window_and_costs_no_probe(values, target):
    windows, _sizes, probes = _density_plan(values, target)
    assert windows == [Window(None, None, True)] and probes == []


def test_the_probe_budget_bounds_the_planning_cost():
    values = list(range(1_000_000))
    _windows, sizes, probes = _density_plan(values, 10, max_probes=20)
    assert len(probes) == 20 and sum(sizes) == len(values)


def test_a_query_without_an_upper_placeholder_cannot_be_loaded_in_windows():
    assert incremental.can_bound_above("SELECT * FROM t")
    assert incremental.can_bound_above("SELECT * FROM t WHERE c >= {{since}} AND c < {{until}}")
    assert not incremental.can_bound_above("SELECT * FROM t WHERE c >= {{since}}")


def test_counting_a_window_is_one_statement_over_the_windowed_query():
    sql = incremental.count_sql("SELECT * FROM t;", "postgres", "c", None, Window(lo=1, hi=9))
    assert sql == 'SELECT COUNT(*) FROM (SELECT * FROM (SELECT * FROM t) bow_sub WHERE "c" >= 1 AND "c" < 9) bow_cnt'


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
    aware = incremental.cap_checkpoint(datetime(2099, 1, 1, tzinfo=UTC), now)
    assert aware == now.replace(tzinfo=UTC) and aware.tzinfo is not None
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

@pytest.mark.parametrize("dialect", sorted(incremental._FAMILY))
@pytest.mark.parametrize("value", CURSOR_VALUES)
def test_every_dialect_renders_every_cursor_kind(dialect, value):
    sql = incremental.render_sql("SELECT * FROM t;", dialect, "updated at", None, Window(lo=value))
    assert re.match(r"SELECT \* FROM \(SELECT \* FROM t\) (AS )?bow_sub WHERE ", sql)
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


@pytest.mark.parametrize("dialect", sorted(incremental._FAMILY))
def test_an_open_lower_bound_is_older_than_any_real_row(dialect):
    """Year 1 must be written with four digits: an engine reads '1-01-01' as
    2001, and every row older than that silently drops out of the load."""
    import re
    out = incremental.render_sql("SELECT * FROM t WHERE c >= {{since}}", dialect, "c", "timestamp", Window())
    (year,) = re.findall(r"'(\d+)-\d\d-\d\d[ T]", out)
    assert len(year) == 4 and int(year) <= 1970


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
    lo = datetime(2026, 1, 1, tzinfo=UTC)
    now = datetime(2026, 1, 2, tzinfo=UTC)
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


def test_an_increment_may_widen_a_type(volume):
    base = build(volume, pa.table({"id": [1], "amount": [10], "seq": [1]}), config=merge_config())
    outcome = increment(volume, base, merge_config(), 1, pa.table({"id": [2], "amount": [2.5], "seq": [2]}))
    assert read(outcome, "SELECT id, amount FROM t ORDER BY id") == [(1, 10.0), (2, 2.5)]


def test_an_increment_missing_a_stored_column_is_refused_so_the_table_can_be_rebuilt(volume):
    """Keeping the column would leave old rows with values the source no
    longer has and new rows without them."""
    base = build(volume, pa.table({"id": [1], "note": ["n"], "seq": [1]}), config=merge_config())
    with pytest.raises(loader.SchemaChanged) as error:
        increment(volume, base, merge_config(), 1, pa.table({"id": [2], "seq": [2]}))
    assert "note" in str(error.value) and not list(volume.rglob("*.tmp"))


@pytest.mark.parametrize("delta", [
    pa.table({"id": [2], "flag": pa.array([None], type=pa.null()), "small": pa.array([None], type=pa.null()),
              "seq": [2]}),
    pa.table({"id": pa.array([], type=pa.int64()), "flag": pa.array([], type=pa.null()),
              "small": pa.array([], type=pa.null()), "seq": pa.array([], type=pa.int64())}),
])
def test_a_delta_with_no_value_in_a_column_does_not_retype_the_stored_column(volume, delta):
    """A column that is all NULL (or a delta that is empty) has no type of
    its own; it must not turn a stored boolean into a number."""
    base = build(volume, pa.table({"id": [1], "flag": [True], "small": pa.array([7], type=pa.int8()), "seq": [1]}),
                 config=merge_config())
    outcome = increment(volume, base, merge_config(), 1, delta)
    types = {c["name"]: c["dtype"] for c in outcome.columns}
    assert types["flag"] == "bool" and types["small"] == "int8"
    assert read(outcome, "SELECT flag, small FROM t WHERE id = 1") == [(True, 7)]


def test_a_window_already_in_the_file_is_not_loaded_a_second_time(volume):
    """The crash this guards against: the rows were committed, the note that
    the window is done was not."""
    part, key = loader.new_part("conn")
    rows = pa.table({"v": ["a", "b"], "seq": [1, 2]})
    first = loader.load_window(Batches(rows), "q", part, key, "t", "r1", limits(), batch_rows=10, window=0)
    again = loader.load_window(Batches(rows), "q", part, key, "t", "r2", limits(), batch_rows=10, window=0)
    other = loader.load_window(Batches(pa.table({"v": ["c"], "seq": [3]})), "q", part, key, "t", "r2",
                               limits(), batch_rows=10, window=1, rows_before=2)
    outcome = loader.finalize(part, key, "t", append_config(), run_id="r2")
    assert (first, again, other) == (2, 2, 1)
    assert sorted(read(outcome, "SELECT v FROM t")) == [("a",), ("b",), ("c",)]
    # The loader's own bookkeeping does not ship in the published file.
    assert read(outcome, "SELECT table_name FROM duckdb_tables() WHERE database_name = 'artifact'") == [("t",)]


def test_a_renamed_table_keeps_its_rows_through_an_increment_and_a_resumed_load(volume):
    base = build(volume, pa.table({"id": [1], "seq": [1]}), config=merge_config(), relation="old_name")
    outcome = loader.apply_increment(
        Batches(pa.table({"id": [2], "seq": [2]})), "q", base.artifact_path, base.artifact_key, "new_name",
        "conn", "run-2", merge_config(), Window(lo=1, include_nulls=True), limits(), batch_rows=10)
    assert read(outcome, "SELECT id FROM new_name ORDER BY id") == [(1,), (2,)]

    part, key = loader.new_part("conn")
    loader.load_window(Batches(pa.table({"id": [1], "seq": [1]})), "q", part, key, "old_name", "r", limits(),
                       batch_rows=10, window=0)
    loader.load_window(Batches(pa.table({"id": [2], "seq": [2]})), "q", part, key, "new_name", "r", limits(),
                       batch_rows=10, window=1, rows_before=1)
    resumed = loader.finalize(part, key, "new_name", merge_config(), run_id="r")
    assert read(resumed, "SELECT id FROM new_name ORDER BY id") == [(1,), (2,)]


def test_a_file_that_cannot_be_published_is_not_left_behind(volume, monkeypatch):
    from app.services import storage
    part, key = loader.new_part("conn")
    loader.load_window(Batches(pa.table({"id": [1]})), "q", part, key, "t", "r", limits(), batch_rows=10)

    def refuse(path, *a, **k):
        raise storage.StorageError("bucket unreachable")

    monkeypatch.setattr(storage, "persist", refuse)
    with pytest.raises(storage.StorageError):
        loader.finalize(part, key, "t", RefreshConfig(), run_id="r")
    assert not list(volume.rglob("*.db")), "renamed but unpublished"


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


# -- connectors that take a JSON request -------------------------------------------

class FakeMetricsApi:
    """An API holding one point per `step` ms, that returns at most `ceiling`
    rows per request and — like the real ones — truncates without saying so."""

    INCREMENTAL_WINDOW = {"start": "start_time", "end": "end_time", "format": "epoch_ms",
                          "clears": ["duration_in_mins"], "limit": "limit", "row_ceiling": 100}

    def __init__(self, first=0, last=10_000, step=10, burst_at=None):
        self.points = list(range(first, last, step))
        if burst_at is not None:
            self.points += [burst_at] * 150        # more rows at one instant than a response holds
        self.requests = []

    def execute_query(self, query):
        import pandas as pd
        body = json.loads(query)
        self.requests.append(body)
        lo, hi = body.get("start_time", float("-inf")), body.get("end_time", float("inf"))
        rows = sorted(p for p in self.points if lo <= p < hi)[: body.get("limit", 100)]
        return pd.DataFrame({"timestamp": rows, "value": [p / 2 for p in rows]},
                            columns=["timestamp", "value"])


class BucketedApi(FakeMetricsApi):
    """Like the real metrics APIs: the end bound is inclusive, and points are
    rolled up into buckets — one row per bucket, summed."""

    BUCKET = 50
    INCREMENTAL_WINDOW = {**FakeMetricsApi.INCREMENTAL_WINDOW, "end_inclusive": True,
                          "bucket": lambda body: BucketedApi.BUCKET}

    def execute_query(self, query):
        import pandas as pd
        body = json.loads(query)
        self.requests.append(body)
        lo, hi = body.get("start_time", float("-inf")), body.get("end_time", float("inf"))
        buckets = {}
        for p in self.points:
            if lo <= p <= hi:
                buckets[p // self.BUCKET * self.BUCKET] = buckets.get(p // self.BUCKET * self.BUCKET, 0) + 1
        rows = sorted(buckets.items())[: body.get("limit", 100)]
        return pd.DataFrame(rows, columns=["timestamp", "value"])

    def truth(self, lo, hi) -> dict:
        out = {}
        for p in self.points:
            if lo <= p <= hi:
                out[p // self.BUCKET * self.BUCKET] = out.get(p // self.BUCKET * self.BUCKET, 0) + 1
        return out


@pytest.mark.parametrize("window", [(0, 9_999), (0, 30_000), (1_050, 8_849)])
def test_splitting_a_rolled_up_window_never_cuts_a_bucket_or_repeats_one(window):
    api = BucketedApi(last=30_000)
    spec = json.dumps({"start_time": window[0], "end_time": window[1]})
    got = {}
    for batch in _request_source(api).stream_batches(spec, 40):
        for t, v in zip(batch.column("timestamp").to_pylist(), batch.column("value").to_pylist()):
            assert t not in got, f"bucket {t} was returned twice"
            got[t] = v
    assert got == api.truth(*window)
    assert len(api.requests) > 1, "the window was actually split"


def test_an_incremental_read_of_a_rolled_up_api_starts_on_a_whole_bucket():
    api = BucketedApi()
    spec = json.dumps({"table": "metrics"})
    config = RefreshConfig.from_dict({"mode": "append", "cursor_column": "timestamp", "cursor_kind": "number",
                                      "lookback_units": 120})
    window = incremental.align_window(api, spec, incremental.incremental_window(1_000, config), config)
    assert window.lo == 850 and window.lo % BucketedApi.BUCKET == 0
    # With no lookback configured, the newest bucket — still filling when it
    # was first read — is read again all the same.
    none = RefreshConfig.from_dict({**config.to_dict(), "mode": "append", "lookback_units": 0})
    assert incremental.align_window(api, spec, incremental.incremental_window(1_000, none), none).lo == 950
    # A SQL source has no buckets to align to.
    assert incremental.align_window(object(), "SELECT 1", Window(lo=1_003), config).lo == 1_003


def _request_source(api):
    from app.data_sources.fast.sources import source_for
    return source_for(api)


@pytest.mark.parametrize("window", [(0, 10_000), (2_500, 7_777), (0, 500), (9_990, 10_000)])
def test_a_capped_api_is_read_in_windows_that_return_every_row_once(window):
    api = FakeMetricsApi()
    spec = json.dumps({"table": "metrics", "start_time": window[0], "end_time": window[1]})
    got = [t for batch in _request_source(api).stream_batches(spec, 40) for t in batch.column("timestamp").to_pylist()]
    assert sorted(got) == [p for p in api.points if window[0] <= p < window[1]]
    assert len(got) == len(set(got)), "no row arrived twice"
    assert all(r["limit"] == 100 for r in api.requests), "every request asks for a full page"


def test_a_capped_api_that_cannot_be_split_further_fails_instead_of_truncating():
    api = FakeMetricsApi(burst_at=5_000)
    spec = json.dumps({"table": "metrics", "start_time": 0, "end_time": 10_000})
    with pytest.raises(RuntimeError) as error:
        list(_request_source(api).stream_batches(spec, 40))
    assert "at most 100 rows per request" in str(error.value)
    # …and so does a request at the ceiling that names no window at all.
    with pytest.raises(RuntimeError):
        list(_request_source(FakeMetricsApi()).stream_batches(json.dumps({"table": "metrics"}), 40))


def test_an_empty_api_response_still_gives_the_table_its_shape():
    api = FakeMetricsApi()
    (batch,) = _request_source(api).stream_batches(json.dumps({"start_time": 50_000, "end_time": 60_000}), 40)
    assert batch.num_rows == 0 and batch.schema.names == ["timestamp", "value"]


def test_previewing_an_api_request_asks_for_only_a_page():
    api = FakeMetricsApi()
    cols, rows = _request_source(api).preview(json.dumps({"table": "metrics", "limit": 5000}), 7)
    assert [c["name"] for c in cols] == ["timestamp", "value"] and len(rows) == 7
    assert api.requests == [{"table": "metrics", "limit": 7}]


def test_an_api_table_can_be_incremented_from_its_checkpoint(volume):
    api = FakeMetricsApi(last=1_000)
    config = RefreshConfig.from_dict({"mode": "append", "cursor_column": "timestamp", "cursor_kind": "number",
                                      "lookback_units": 30})
    source = _request_source(api)
    first = incremental.render(api, json.dumps({"table": "metrics", "duration_in_mins": 60}),
                               "timestamp", "number", Window(lo=0))
    part, key = loader.new_part("conn")
    loader.load_window(source, first, part, key, "t", "run-1", limits(), batch_rows=40)
    base = loader.finalize(part, key, "t", config, run_id="run-1")
    assert base.row_count == 100 and base.max_cursor == 990

    api.points += list(range(1_000, 1_200, 10))
    window = incremental.incremental_window(base.max_cursor, config)
    rendered = incremental.render(api, json.dumps({"table": "metrics", "duration_in_mins": 60}),
                                  "timestamp", "number", window)
    assert json.loads(rendered)["start_time"] == 960 and "duration_in_mins" not in rendered
    outcome = loader.apply_increment(source, rendered, base.artifact_path, base.artifact_key, "t", "conn",
                                     "run-2", config, window, limits(), batch_rows=40, wrapped=False)
    assert [r[0] for r in read(outcome, "SELECT timestamp FROM t ORDER BY 1")] == api.points
    assert outcome.rows_read == 24, "only the lookback and the new points were fetched"
