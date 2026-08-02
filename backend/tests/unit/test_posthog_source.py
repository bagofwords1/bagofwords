"""PostHog native extraction source — windowed extraction over the HogQL API.

The HTTP boundary is faked with a small HogQL server; everything above it — the
client's request/retry handling, the count check, window splitting, batch
shaping and the DuckDB artifact it all ends up in — runs real.

The properties under test are the ones this source has and no cursor-based
source does. PostHog serves at most 50,000 rows per response, truncates without
saying so, and rejects OFFSET for personal API keys, so extraction is split into
half-open windows over a datetime column and verified against an exact count.
What has to hold: every row lands exactly once, a tie on the cursor is not
dropped at a window edge, and anything short of the count fails loudly.
"""

import json
from datetime import datetime, timedelta

import pytest
import requests

from app.data_sources.clients import posthog_client as posthog_module
from app.data_sources.clients.posthog_client import PostHogClient
from app.data_sources.fast import artifacts, extractor
from app.data_sources.fast import posthog_source as ph
from app.data_sources.fast.posthog_source import PostHogSource

BASE = datetime(2026, 1, 1, 12, 0, 0)


# --------------------------------------------------------------------------
# A fake HogQL server at the HTTP boundary
# --------------------------------------------------------------------------

class FakeHogQL:
    """Enough of the query endpoint to answer what the source actually sends.

    Holds one in-memory table and understands the three statement shapes the
    source builds: `count()`, the min/max/null range, and a `SELECT *` with an
    optional cursor window and a LIMIT. The inner query is ignored — what is
    under test is the extraction strategy, not SQL execution.
    """

    def __init__(self, rows, types, ceiling=None, withhold=0):
        self.rows = rows                       # list[dict]
        self.types = types                     # {column: clickhouse type}
        self.columns = list(types.keys())
        self.ceiling = ceiling                 # rows one response will carry
        self.withhold = withhold               # rows silently dropped, as PostHog does
        self.queries = []

    # -- the endpoint --------------------------------------------------

    def respond(self, sql):
        self.queries.append(sql)
        if sql.startswith("SELECT count()") and "min(" not in sql:
            return ["count"], [["count", "UInt64"]], [[len(self.rows)]]
        if "min(" in sql:
            return self._range(sql)
        return self._select(sql)

    def _range(self, sql):
        col = _between(sql, "min(`", "`)")
        vals = [r[col] for r in self.rows if r.get(col) is not None]
        nulls = len(self.rows) - len(vals)
        return (
            ["min", "max", "nulls"],
            [["min", "DateTime64(6)"], ["max", "DateTime64(6)"], ["nulls", "UInt64"]],
            [[min(vals), max(vals), nulls]] if vals else [[None, None, nulls]],
        )

    def _select(self, sql):
        rows = self.rows
        window = _parse_window(sql)
        if window:
            col, lo, hi, closed = window
            rows = [
                r for r in rows
                if r.get(col) is not None
                and _dt(r[col]) >= lo
                and (_dt(r[col]) <= hi if closed else _dt(r[col]) < hi)
            ]
        limit = _parse_limit(sql)
        if self.ceiling is not None:
            limit = min(limit, self.ceiling)
        rows = rows[:limit]
        if self.withhold and rows:
            # A response that comes up short and does not say so — the exact
            # behavior the count check exists to catch.
            rows = rows[: -self.withhold]
        types = [[c, self.types[c]] for c in self.columns]
        return self.columns, types, [[r.get(c) for c in self.columns] for r in rows]


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    """A requests.Session backed either by a FakeHogQL or by canned responses."""

    def __init__(self, server=None, responses=None):
        self.server = server
        self._responses = list(responses or [])
        self.queries = []
        self.headers = {}

    def post(self, url, json=None, timeout=None):
        sql = json["query"]["query"]
        self.queries.append(sql)
        if self.server is not None:
            cols, types, rows = self.server.respond(sql)
            return FakeResponse({"columns": cols, "types": types, "results": rows})
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]

    def close(self):
        pass


def _between(s, start, end):
    return s.split(start, 1)[1].split(end, 1)[0]


def _parse_limit(sql):
    return int(sql.rsplit("LIMIT", 1)[1].strip())


def _parse_window(sql):
    if " WHERE " not in sql:
        return None
    clause = sql.split(" WHERE ", 1)[1].split(" LIMIT")[0]
    col = clause.split("`")[1]
    lo, hi = [p.split("'")[1] for p in clause.split(" AND ")]
    return col, _dt(lo), _dt(hi), "<=" in clause.split(" AND ")[1]


def _dt(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _events(n, *, tie_at_end=0, step_seconds=60):
    """`n` rows on a rising timestamp, optionally with a tie on the last value."""
    rows = []
    for i in range(n - tie_at_end):
        rows.append({"uuid": f"u{i}", "event": "$pageview",
                     "timestamp": _iso(BASE + timedelta(seconds=i * step_seconds))})
    last = rows[-1]["timestamp"] if rows else _iso(BASE)
    for i in range(tie_at_end):
        rows.append({"uuid": f"tie{i}", "event": "$pageview", "timestamp": last})
    return rows


EVENT_TYPES = {"uuid": "UUID", "event": "String", "timestamp": "DateTime64(6, 'UTC')"}


def _source(server, monkeypatch, ceiling=None):
    """A real PostHogClient + PostHogSource over the fake server."""
    session = FakeSession(server=server)
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr(posthog_module.time, "sleep", lambda _s: None)
    if ceiling is not None:
        monkeypatch.setattr(ph, "SINGLE_RESPONSE_ROWS", ceiling)
        server.ceiling = ceiling
    client = PostHogClient(api_key="k", host="https://us.posthog.com", project_id="42")
    return PostHogSource(client), session


def _canned(responses, monkeypatch):
    session = FakeSession(responses=responses)
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr(posthog_module.time, "sleep", lambda _s: None)
    client = PostHogClient(api_key="k", host="https://us.posthog.com", project_id="42")
    return client, session


def _page(columns, rows, types=None):
    return FakeResponse({"columns": columns, "types": types, "results": rows})


def _collect(src, sql, batch_rows=1000):
    """Every row the source yields, flattened, with its column names."""
    batches = list(src.stream_batches(sql, batch_rows))
    names = batches[0].column_names if batches else []
    rows = [r for b in batches for r in zip(*[b.column(c).to_pylist() for c in names])]
    return names, rows


# --------------------------------------------------------------------------
# Resolution & gating
# --------------------------------------------------------------------------

def test_the_posthog_client_resolves_to_its_native_source():
    from app.data_sources.fast.sources import source_for

    client = PostHogClient(api_key="k", project_id="42")
    assert isinstance(source_for(client), PostHogSource)


def test_posthog_is_an_accelerable_verified_type():
    """Verified against a live project end to end — which is also where the
    design came from, since the API rejects OFFSET and truncates silently."""
    from app.services.custom_query_service import (
        ACCELERABLE_TYPES,
        VERIFIED_TYPES,
        is_accelerable_type,
    )

    assert "posthog" in ACCELERABLE_TYPES
    assert "posthog" in VERIFIED_TYPES
    assert is_accelerable_type("posthog")


# --------------------------------------------------------------------------
# OFFSET — rejected by the API, so it must never be sent
# --------------------------------------------------------------------------

def test_no_statement_ever_uses_offset(monkeypatch):
    """PostHog refuses OFFSET for personal API keys, which is the only
    credential this connector has. Every statement the source builds — preview,
    count, range, cursor probe, window fetch — has to live without it."""
    server = FakeHogQL(_events(25), EVENT_TYPES)
    src, session = _source(server, monkeypatch, ceiling=10)

    src.preview("SELECT * FROM events", 5)
    src.estimate("SELECT * FROM events")
    _collect(src, "SELECT * FROM events")

    assert session.queries, "the source must actually have issued statements"
    assert not any("OFFSET" in q.upper() for q in session.queries)


# --------------------------------------------------------------------------
# Estimating — an exact count, and whether the result can be materialized
# --------------------------------------------------------------------------

def test_estimate_reports_the_exact_row_count(monkeypatch):
    src, _ = _source(FakeHogQL(_events(137), EVENT_TYPES), monkeypatch)
    est = src.estimate("SELECT * FROM events")

    assert est.supported is True
    assert est.rows == 137


def test_estimate_degrades_without_raising(monkeypatch):
    """A source must never break the save path, however it fails."""
    client, _ = _canned([FakeResponse({"detail": "boom"}, status_code=500)], monkeypatch)
    est = PostHogSource(client).estimate("SELECT * FROM events")

    assert est.supported is False
    assert est.note


def test_a_result_too_large_to_page_is_refused_while_saving(monkeypatch):
    """No datetime column means no way to window, and PostHog cannot page. The
    admin should hear that on the Test click, not on the first refresh."""
    rows = [{"event": f"e{i}", "n": i} for i in range(25)]
    server = FakeHogQL(rows, {"event": "String", "n": "UInt64"})
    src, _ = _source(server, monkeypatch, ceiling=10)

    est = src.estimate("SELECT event, n FROM events")
    assert est.rows == 25
    assert est.source_max_rows == 10

    with pytest.raises(extractor.ExtractionRefused) as e:
        extractor.check_budget(est)
    assert "batch export" in str(e.value).lower() or "timestamp" in str(e.value).lower()


def test_a_large_result_with_a_timestamp_is_not_refused(monkeypatch):
    """The same size is fine when it can be windowed — the ceiling is a
    property of the query's shape, not of its row count."""
    server = FakeHogQL(_events(25), EVENT_TYPES)
    src, _ = _source(server, monkeypatch, ceiling=10)

    est = src.estimate("SELECT * FROM events")
    assert est.rows == 25
    assert est.source_max_rows is None
    extractor.check_budget(est)


def test_a_source_ceiling_is_only_hit_when_the_estimate_exceeds_it():
    """The new check must not fire on sources that never set it."""
    extractor.check_budget(extractor.Estimate(rows=10_000))
    extractor.check_budget(
        extractor.Estimate(rows=10, source_max_rows=50, source_max_rows_note="n/a")
    )


# --------------------------------------------------------------------------
# Preview — one bounded request, admin SQL intact inside it
# --------------------------------------------------------------------------

def test_preview_asks_for_exactly_one_bounded_page(monkeypatch):
    src, session = _source(FakeHogQL(_events(50), EVENT_TYPES), monkeypatch)
    cols, rows = src.preview("SELECT * FROM events", 5)

    assert len(session.queries) == 1
    assert len(rows) == 5
    assert [c["name"] for c in cols] == ["uuid", "event", "timestamp"]
    assert [c["dtype"] for c in cols] == ["UUID", "String", "DateTime64(6, 'UTC')"]


def test_the_admin_sql_reaches_the_server_untouched_inside_the_wrapper(monkeypatch):
    """Wrapping is unavoidable — an unbounded HogQL query is truncated by the
    server. Rewriting the statement is not: an ORDER BY, a LIMIT of the admin's
    own, and duplicate-looking projections all survive verbatim."""
    src, session = _source(FakeHogQL(_events(2), EVENT_TYPES), monkeypatch)
    sql = "SELECT uuid, timestamp FROM events ORDER BY timestamp DESC LIMIT 10;"
    src.preview(sql, 100)

    sent = session.queries[0]
    assert "SELECT uuid, timestamp FROM events ORDER BY timestamp DESC LIMIT 10" in sent
    assert ";" not in sent


def test_preview_survives_a_response_with_no_types(monkeypatch):
    """`types` is a nicety — the column names are the contract."""
    client, _ = _canned([_page(["a"], [[1]], types=None)], monkeypatch)
    cols, rows = PostHogSource(client).preview("SELECT a FROM events", 10)

    assert [c["name"] for c in cols] == ["a"]
    assert cols[0]["dtype"] is None
    assert rows == [[1]]


# --------------------------------------------------------------------------
# Extraction — the invariant everything else exists to protect
# --------------------------------------------------------------------------

def test_a_small_result_is_one_request_after_the_count(monkeypatch):
    """Under the ceiling there is nothing to window; a rollup — the query this
    feature is actually for — must not pay for machinery it doesn't need."""
    server = FakeHogQL(_events(8), EVENT_TYPES)
    src, session = _source(server, monkeypatch, ceiling=10)

    names, rows = _collect(src, "SELECT * FROM events")
    assert len(rows) == 8
    assert len(session.queries) == 2, session.queries


def test_every_row_is_materialized_exactly_once_across_windows(monkeypatch):
    """The whole point. Windows partition the cursor range, so a row can be
    neither skipped at an edge nor counted twice by an overlap."""
    server = FakeHogQL(_events(25), EVENT_TYPES)
    src, _ = _source(server, monkeypatch, ceiling=10)

    names, rows = _collect(src, "SELECT * FROM events")
    uuids = [r[names.index("uuid")] for r in rows]

    assert sorted(uuids) == sorted(f"u{i}" for i in range(25))
    assert len(uuids) == len(set(uuids)), "a row was materialized twice"


def test_rows_sharing_the_cursor_value_are_not_lost_at_a_window_edge(monkeypatch):
    """Keyset `>` — what PostHog's own error message suggests — silently drops
    ties. Half-open windows are used precisely because they cannot, and real
    projects do have ties: the max timestamp is a common one."""
    server = FakeHogQL(_events(24, tie_at_end=6), EVENT_TYPES)
    src, _ = _source(server, monkeypatch, ceiling=8)

    names, rows = _collect(src, "SELECT * FROM events")
    uuids = [r[names.index("uuid")] for r in rows]

    assert len(uuids) == 24
    assert sorted(u for u in uuids if u.startswith("tie")) == [f"tie{i}" for i in range(6)]


def test_a_window_that_reaches_the_ceiling_is_split_not_trusted(monkeypatch):
    """A response at the ceiling cannot say whether more was waiting, so it is
    re-asked in halves. Without this the extraction silently truncates."""
    server = FakeHogQL(_events(40), EVENT_TYPES)
    src, session = _source(server, monkeypatch, ceiling=10)

    _names, rows = _collect(src, "SELECT * FROM events")
    windowed = [q for q in session.queries if " WHERE " in q]

    assert len(rows) == 40
    assert len(windowed) > 4, "a 40-row result over a 10-row ceiling must split"


def test_windows_are_sized_from_the_count_rather_than_discovered(monkeypatch):
    """Starting from the whole range guarantees the first request hits the
    ceiling and is thrown away, and so does the first half. The count is
    already known, so the range is sliced up front — a wasted request on a
    rate-limited API is the cost this avoids."""
    server = FakeHogQL(_events(40), EVENT_TYPES)
    src, session = _source(server, monkeypatch, ceiling=10)

    _names, rows = _collect(src, "SELECT * FROM events")
    fetches = [q for q in session.queries if q.startswith("SELECT *") and "LIMIT 0" not in q]
    discarded = sum(1 for q in fetches if " WHERE " in q)

    assert len(rows) == 40
    # 40 rows over a 10-row ceiling needs 4 windows at minimum; allow slack for
    # burstiness but not for rediscovering the shape of the data every time.
    assert len(fetches) <= 12, session.queries
    assert discarded >= 4


def test_a_truncated_extraction_is_refused_rather_than_stored(monkeypatch):
    """The count check is the safety net under every strategy above it. A
    refresh that comes up short must fail so the previous artifact keeps
    serving — a relation quietly missing rows is the worst outcome here."""
    server = FakeHogQL(_events(20), EVENT_TYPES, withhold=1)
    src, _ = _source(server, monkeypatch, ceiling=10)

    with pytest.raises(RuntimeError, match="of 20 rows"):
        _collect(src, "SELECT * FROM events")


def test_a_short_response_is_caught_on_the_single_request_path_too(monkeypatch):
    """Truncation is not only a windowing concern — a result that fits in one
    response can come back short as well, and must not be stored either."""
    server = FakeHogQL(_events(8), EVENT_TYPES, withhold=1)
    src, _ = _source(server, monkeypatch, ceiling=10)

    with pytest.raises(RuntimeError, match="of 8 rows"):
        _collect(src, "SELECT * FROM events")


def test_extraction_refuses_a_cursor_with_missing_values(monkeypatch):
    """A row whose cursor is NULL falls outside every window. Refusing beats
    materializing a relation that is missing exactly those rows."""
    rows = _events(20)
    rows[3]["timestamp"] = None
    server = FakeHogQL(rows, EVENT_TYPES)
    src, _ = _source(server, monkeypatch, ceiling=10)

    with pytest.raises(RuntimeError, match="no 'timestamp' value"):
        _collect(src, "SELECT * FROM events")


def test_extraction_refuses_when_there_is_nothing_to_window_on(monkeypatch):
    rows = [{"event": f"e{i}", "n": i} for i in range(25)]
    server = FakeHogQL(rows, {"event": "String", "n": "UInt64"})
    src, _ = _source(server, monkeypatch, ceiling=10)

    with pytest.raises(RuntimeError, match="date or timestamp"):
        _collect(src, "SELECT event, n FROM events")


def test_a_cursor_with_no_spread_fails_with_a_reason(monkeypatch):
    """Every row on one timestamp cannot be split further. The refresh has to
    say why rather than loop until the wall-clock cap kills it."""
    server = FakeHogQL(_events(20, tie_at_end=20), EVENT_TYPES)
    src, _ = _source(server, monkeypatch, ceiling=10)

    with pytest.raises(RuntimeError, match="share a single"):
        _collect(src, "SELECT * FROM events")


def test_zero_rows_still_emits_the_shape(monkeypatch):
    """An empty result must still create the relation, or the agent sees a
    missing table rather than an empty one."""
    server = FakeHogQL([], EVENT_TYPES)
    src, _ = _source(server, monkeypatch)

    batches = list(src.stream_batches("SELECT * FROM events", 100))
    assert len(batches) == 1
    assert batches[0].num_rows == 0
    assert batches[0].column_names == ["uuid", "event", "timestamp"]


def test_a_date_column_works_as_a_cursor_when_there_is_no_timestamp(monkeypatch):
    """The cursor is chosen from the reported types, not from a hardcoded name —
    an admin's query need not project `timestamp` to be extractable."""
    rows = [{"day": _iso(BASE + timedelta(days=i)), "n": i} for i in range(25)]
    server = FakeHogQL(rows, {"day": "DateTime64(6, 'UTC')", "n": "UInt64"})
    src, _ = _source(server, monkeypatch, ceiling=10)

    _names, out = _collect(src, "SELECT day, n FROM events")
    assert len(out) == 25


# --------------------------------------------------------------------------
# Column types across batches — a windowed source's own failure mode
# --------------------------------------------------------------------------

def test_a_column_all_null_in_the_first_window_still_accepts_later_values(monkeypatch):
    """The first batch fixes the artifact's column types. A column that happens
    to be empty in it would otherwise be typed as "holds nothing", and a later
    window would fail to append — halfway through a refresh."""
    rows = _events(20)
    for i, r in enumerate(rows):
        r["email"] = None if i < 12 else f"u{i}@example.com"
    server = FakeHogQL(rows, {**EVENT_TYPES, "email": "String"})
    src, _ = _source(server, monkeypatch, ceiling=10)

    names, out = _collect(src, "SELECT * FROM events")
    emails = [r[names.index("email")] for r in out]

    assert len(out) == 20
    assert sum(1 for e in emails if e) == 8


def test_date_columns_are_stored_as_dates_not_text(monkeypatch):
    """JSON has no date type, so a DateTime64 arrives as a string and Arrow
    would infer text. Stored that way, every agent query has to CAST before it
    can use date_trunc or an interval — and has to know to. The API reports the
    real type, so it is used."""
    import pyarrow as pa

    server = FakeHogQL(_events(6), EVENT_TYPES)
    src, _ = _source(server, monkeypatch)

    (batch,) = list(src.stream_batches("SELECT * FROM events", 100))
    field = batch.schema.field("timestamp")

    assert pa.types.is_timestamp(field.type), f"timestamp came through as {field.type}"
    assert pa.types.is_string(batch.schema.field("event").type)


def test_an_unparseable_date_keeps_the_column_rather_than_nulling_it(monkeypatch):
    """A column that is mostly dates is not worth silently emptying; text keeps
    the data, and the agent can still cast it."""
    import pyarrow as pa

    rows = _events(6)
    rows[2]["timestamp"] = "not-a-date"
    server = FakeHogQL(rows, EVENT_TYPES)
    src, _ = _source(server, monkeypatch)

    names, out = _collect(src, "SELECT * FROM events")
    values = [r[names.index("timestamp")] for r in out]

    assert len(out) == 6
    assert all(v is not None for v in values), "no value may be dropped"


def test_dates_survive_into_the_artifact_as_a_queryable_type(monkeypatch, tmp_path):
    """The end that matters: DuckDB gets a real timestamp, so date_trunc works
    without the agent casting anything."""
    monkeypatch.setattr(artifacts, "_ARTIFACT_ROOT", tmp_path)
    server = FakeHogQL(_events(25, step_seconds=3600), EVENT_TYPES)
    src, _ = _source(server, monkeypatch, ceiling=10)

    result = extractor.extract_to_artifact(
        src.client, "SELECT * FROM events", "raw_events", "conn-dates"
    )
    con = artifacts.connect_encrypted(
        result.artifact_path, result.artifact_key, read_only=True
    )
    try:
        days = con.execute(
            'SELECT count(DISTINCT date_trunc(\'day\', timestamp)) FROM "raw_events"'
        ).fetchone()[0]
    finally:
        con.close()
    assert days >= 1


def test_json_columns_are_stored_as_text(monkeypatch):
    """ClickHouse arrays and tuples arrive as nested structures whose shape
    differs per row; inferring a type per batch would give two batches of the
    same column different types."""
    rows = [{"uuid": "u0", "event": "e", "timestamp": _iso(BASE),
             "props": {"$browser": "Chrome"}},
            {"uuid": "u1", "event": "e", "timestamp": _iso(BASE + timedelta(minutes=1)),
             "props": {"utm_source": "x"}}]
    server = FakeHogQL(rows, {**EVENT_TYPES, "props": "String"})
    src, _ = _source(server, monkeypatch)

    names, out = _collect(src, "SELECT * FROM events")
    values = [json.loads(r[names.index("props")]) for r in out]
    assert values == [{"$browser": "Chrome"}, {"utm_source": "x"}]


def test_a_column_whose_type_changes_mid_extraction_fails_loudly(monkeypatch):
    """Better an aborted refresh — the previous artifact keeps serving — than
    a relation whose later batches were quietly coerced into something else."""
    rows = _events(20)
    for i, r in enumerate(rows):
        r["n"] = i if i < 12 else "not-a-number"
    server = FakeHogQL(rows, {**EVENT_TYPES, "n": "UInt64"})
    src, _ = _source(server, monkeypatch, ceiling=10)

    with pytest.raises(RuntimeError):
        _collect(src, "SELECT * FROM events")


# --------------------------------------------------------------------------
# Rate limiting — the source this feature exists to protect
# --------------------------------------------------------------------------

def test_a_rate_limited_request_is_waited_out_and_retried(monkeypatch):
    waits = []
    monkeypatch.setattr(posthog_module.time, "sleep", waits.append)
    client, session = _canned(
        [
            FakeResponse({"detail": "throttled"}, status_code=429,
                         headers={"Retry-After": "2"}),
            _page(["i"], [[1]]),
        ],
        monkeypatch,
    )
    monkeypatch.setattr(posthog_module.time, "sleep", waits.append)
    cols, rows = PostHogSource(client).preview("SELECT i FROM events", 10)

    assert waits == [2]
    assert rows == [[1]]
    assert len(session.queries) == 2, "the throttled request must be re-sent, not skipped"


def test_a_rate_limit_with_no_usable_retry_after_still_waits(monkeypatch):
    """Retrying immediately would burn the remaining attempts inside a second
    and turn a wait into a failure."""
    waits = []
    client, _ = _canned(
        [
            FakeResponse({}, status_code=429,
                         headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            _page(["i"], [[1]]),
        ],
        monkeypatch,
    )
    monkeypatch.setattr(posthog_module.time, "sleep", waits.append)
    PostHogSource(client).preview("SELECT i FROM events", 10)
    assert waits and all(w >= 1 for w in waits)


def test_a_persistently_throttled_project_fails_rather_than_hanging(monkeypatch):
    client, session = _canned(
        [FakeResponse({"detail": "throttled"}, status_code=429)], monkeypatch
    )
    with pytest.raises(RuntimeError, match="429"):
        PostHogSource(client).preview("SELECT i FROM events", 10)
    assert len(session.queries) <= posthog_module.RATE_LIMIT_RETRIES + 1


def test_a_query_error_is_surfaced_with_the_servers_reason(monkeypatch):
    client, _ = _canned(
        [FakeResponse({"detail": "Unable to resolve field: nope"}, status_code=400)],
        monkeypatch,
    )
    with pytest.raises(RuntimeError, match="Unable to resolve field"):
        PostHogSource(client).preview("SELECT nope FROM events", 10)


# --------------------------------------------------------------------------
# The client contract the rest of the app depends on
# --------------------------------------------------------------------------

def test_execute_query_still_returns_a_dataframe(monkeypatch):
    """Extraction shares the HTTP call with the agent path; that path returns a
    DataFrame by contract and every existing caller depends on it."""
    client, _ = _canned([_page(["event", "n"], [["$pageview", 3]])], monkeypatch)
    df = client.execute_query("SELECT event, count() AS n FROM events GROUP BY event")

    assert list(df.columns) == ["event", "n"]
    assert df.iloc[0]["n"] == 3


# --------------------------------------------------------------------------
# End to end — windows land in a real encrypted artifact
# --------------------------------------------------------------------------

def test_windows_materialize_into_a_queryable_artifact(monkeypatch, tmp_path):
    """The whole point: many bounded API requests become one local relation an
    agent can run unrestricted SQL over."""
    monkeypatch.setattr(artifacts, "_ARTIFACT_ROOT", tmp_path)
    server = FakeHogQL(_events(25), EVENT_TYPES)
    src, _ = _source(server, monkeypatch, ceiling=10)

    result = extractor.extract_to_artifact(
        src.client, "SELECT * FROM events", "raw_events", "conn-1"
    )

    assert result.row_count == 25
    assert [c["name"] for c in result.columns] == ["uuid", "event", "timestamp"]

    con = artifacts.connect_encrypted(
        result.artifact_path, result.artifact_key, read_only=True
    )
    try:
        assert con.execute('SELECT count(DISTINCT uuid) FROM "raw_events"').fetchone()[0] == 25
    finally:
        con.close()
