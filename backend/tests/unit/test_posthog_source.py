"""PostHog native extraction source — paging over the HogQL query API.

The HTTP boundary is faked; everything above it — the client's request/retry
handling, page composition, batch shaping and the DuckDB artifact it all ends
up in — runs real. What a fake cannot prove (the server's actual row ceiling,
how a live project rate-limits a long extraction) is exactly why the type sits
in UNVERIFIED_TYPES.

PostHog is the first accelerated source with no cursor, so the properties that
matter here are different from every source before it: pages must advance by
what the server *sent*, a paged relation must keep one column type across
pages, and a rate-limited page must be waited out rather than lost.
"""

import json

import pytest
import requests

from app.data_sources.clients import posthog_client as posthog_module
from app.data_sources.clients.posthog_client import PostHogClient
from app.data_sources.fast import artifacts, extractor
from app.data_sources.fast.posthog_source import PostHogSource


# --------------------------------------------------------------------------
# Fakes at the HTTP boundary
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def json(self):
        if isinstance(self._payload, dict):
            return self._payload
        raise ValueError("not json")


class FakeSession:
    """One requests.Session, recording the HogQL it was asked to run."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.queries = []
        self.headers = {}
        self.closed = False

    def post(self, url, json=None, timeout=None):
        self.queries.append(json["query"]["query"])
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]

    def close(self):
        self.closed = True


def _page(columns, rows, types=None):
    return FakeResponse({"columns": columns, "types": types, "results": rows})


def _client(responses, monkeypatch, no_sleep=True):
    """A real PostHogClient whose sessions are the fake above."""
    session = FakeSession(responses)
    monkeypatch.setattr(requests, "Session", lambda: session)
    if no_sleep:
        monkeypatch.setattr(posthog_module.time, "sleep", lambda _s: None)
    client = PostHogClient(api_key="k", host="https://us.posthog.com", project_id="42")
    return client, session


def _source(responses, monkeypatch, **kw):
    client, session = _client(responses, monkeypatch, **kw)
    return PostHogSource(client), session


# --------------------------------------------------------------------------
# Resolution & gating
# --------------------------------------------------------------------------

def test_the_posthog_client_resolves_to_its_native_source():
    from app.data_sources.fast.sources import source_for

    client = PostHogClient(api_key="k", project_id="42")
    assert isinstance(source_for(client), PostHogSource)


def test_posthog_is_an_accelerable_but_unverified_type():
    from app.services.custom_query_service import (
        ACCELERABLE_TYPES,
        UNVERIFIED_TYPES,
        is_accelerable_type,
    )

    assert "posthog" in ACCELERABLE_TYPES
    assert "posthog" in UNVERIFIED_TYPES
    assert is_accelerable_type("posthog")


# --------------------------------------------------------------------------
# Estimating — PostHog has nothing to offer, and says so
# --------------------------------------------------------------------------

def test_estimate_reports_that_posthog_has_no_pre_flight_answer():
    """A silent failure here downgrades to "no pre-flight check" without
    anyone noticing; the note is what makes that visible in the modal."""
    src = PostHogSource(PostHogClient(api_key="k", project_id="42"))
    est = src.estimate("SELECT 1")
    assert est.supported is False
    assert est.rows is None and est.total_bytes is None and est.scan_bytes is None
    assert "no row estimate" in est.note


def test_estimate_does_not_touch_the_api(monkeypatch):
    """`estimate` promises not to run the query — on a metered, rate-limited
    API that promise is worth asserting rather than assuming."""
    src, session = _source([_page(["a"], [[1]])], monkeypatch)
    src.estimate("SELECT * FROM events")
    assert session.queries == []


def test_an_unestimatable_query_is_not_refused():
    """No estimate must mean "fall back to the hard caps", never "reject"."""
    src = PostHogSource(PostHogClient(api_key="k", project_id="42"))
    extractor.check_budget(src.estimate("SELECT * FROM events"))


# --------------------------------------------------------------------------
# Preview — one bounded request, admin SQL intact inside it
# --------------------------------------------------------------------------

def test_preview_asks_for_exactly_one_bounded_page(monkeypatch):
    src, session = _source(
        [_page(["event", "n"], [["$pageview", 3], ["click", 1]], types=["String", "UInt64"])],
        monkeypatch,
    )
    cols, rows = src.preview("SELECT event, count() AS n FROM events GROUP BY event", 5)

    assert len(session.queries) == 1
    assert "LIMIT 5" in session.queries[0]
    assert [c["name"] for c in cols] == ["event", "n"]
    assert [c["dtype"] for c in cols] == ["String", "UInt64"]
    assert rows == [["$pageview", 3], ["click", 1]]


def test_preview_bounds_the_query_the_server_would_otherwise_truncate(monkeypatch):
    """PostHog truncates an unbounded HogQL query to its own default. The
    admin's statement therefore has to be wrapped, not sent as written."""
    src, session = _source([_page(["a"], [])], monkeypatch)
    src.preview("SELECT a FROM events", 100)
    assert "LIMIT 100" in session.queries[0]


def test_the_admin_sql_reaches_the_server_untouched_inside_the_wrapper(monkeypatch):
    """Wrapping is unavoidable; rewriting the statement is not. An ORDER BY,
    a LIMIT of the admin's own, and duplicate-looking projections all have to
    survive verbatim."""
    src, session = _source([_page(["id"], [])], monkeypatch)
    sql = "SELECT uuid, timestamp FROM events ORDER BY timestamp DESC LIMIT 10;"
    src.preview(sql, 100)

    sent = session.queries[0]
    assert "SELECT uuid, timestamp FROM events ORDER BY timestamp DESC LIMIT 10" in sent
    assert ";" not in sent


def test_preview_survives_a_response_with_no_types(monkeypatch):
    """`types` is a nicety — the column names are the contract."""
    src, _ = _source([_page(["a"], [[1]], types=None)], monkeypatch)
    cols, rows = src.preview("SELECT a FROM events", 10)
    assert [c["name"] for c in cols] == ["a"]
    assert cols[0]["dtype"] is None
    assert rows == [[1]]


# --------------------------------------------------------------------------
# Paging — the property a cursor gives for free and HTTP does not
# --------------------------------------------------------------------------

def _offsets(session):
    return [int(q.rsplit("OFFSET", 1)[1]) for q in session.queries]


def test_streaming_pages_until_a_page_comes_back_empty(monkeypatch):
    src, session = _source(
        [
            _page(["i"], [[0], [1]]),
            _page(["i"], [[2], [3]]),
            _page(["i"], [[4]]),
            _page(["i"], []),
        ],
        monkeypatch,
    )
    batches = list(src.stream_batches("SELECT i FROM events", 2))

    assert sum(b.num_rows for b in batches) == 5
    assert [v for b in batches for v in b.column("i").to_pylist()] == [0, 1, 2, 3, 4]
    assert _offsets(session) == [0, 2, 4, 5]


def test_paging_advances_by_the_rows_returned_not_the_rows_requested(monkeypatch):
    """PostHog clamps an over-large LIMIT to its own ceiling, and that ceiling
    has differed between versions. Advancing by the request would skip every
    row the server declined to send — silently, into a materialized relation."""
    src, session = _source(
        [
            _page(["i"], [[0], [1], [2]]),   # asked for 10, server sent 3
            _page(["i"], []),
        ],
        monkeypatch,
    )
    list(src.stream_batches("SELECT i FROM events", 10))
    assert _offsets(session) == [0, 3]


def test_a_page_size_is_never_larger_than_the_api_can_serve(monkeypatch):
    """The extractor's batch size is tuned for cursors (tens of thousands of
    rows); one HTTP request that size would be clamped or time out."""
    from app.data_sources.fast.posthog_source import PAGE_ROWS

    src, session = _source([_page(["i"], [])], monkeypatch)
    list(src.stream_batches("SELECT i FROM events", 500_000))
    assert f"LIMIT {PAGE_ROWS} " in session.queries[0]


def test_zero_rows_still_emits_the_shape(monkeypatch):
    """An empty result must still create the relation, or the agent sees a
    missing table rather than an empty one."""
    src, _ = _source([_page(["event", "n"], [])], monkeypatch)
    batches = list(src.stream_batches("SELECT event, count() AS n FROM events", 100))

    assert len(batches) == 1
    assert batches[0].num_rows == 0
    assert batches[0].column_names == ["event", "n"]


# --------------------------------------------------------------------------
# Column types across pages — a paged source's own failure mode
# --------------------------------------------------------------------------

def test_a_column_all_null_on_the_first_page_still_accepts_later_values(monkeypatch):
    """The first page fixes the artifact's column types. A column that happens
    to be empty in it would otherwise be typed as "holds nothing", and every
    later page would fail to append — halfway through a refresh."""
    src, _ = _source(
        [
            _page(["id", "email"], [[1, None], [2, None]]),
            _page(["id", "email"], [[3, "a@example.com"]]),
            _page(["id", "email"], []),
        ],
        monkeypatch,
    )
    batches = list(src.stream_batches("SELECT id, email FROM persons", 2))

    assert [b.schema for b in batches].count(batches[0].schema) == len(batches)
    assert [v for b in batches for v in b.column("email").to_pylist()] == [
        None, None, "a@example.com",
    ]


def test_json_columns_are_stored_as_text(monkeypatch):
    """PostHog returns `properties` as a nested structure whose keys differ per
    row. Inferring a struct per page would give two pages different types."""
    src, _ = _source(
        [
            _page(["properties"], [[{"$browser": "Chrome"}], [{"utm_source": "x"}]]),
            _page(["properties"], []),
        ],
        monkeypatch,
    )
    (batch,) = list(src.stream_batches("SELECT properties FROM events", 10))

    values = [json.loads(v) for v in batch.column("properties").to_pylist()]
    assert values == [{"$browser": "Chrome"}, {"utm_source": "x"}]


def test_a_column_whose_type_changes_mid_extraction_fails_loudly(monkeypatch):
    """Better an aborted refresh — the previous artifact keeps serving — than
    a relation whose later pages were quietly coerced into something else."""
    src, _ = _source(
        [
            _page(["n"], [[1], [2]]),
            _page(["n"], [["not-a-number"]]),
            _page(["n"], []),
        ],
        monkeypatch,
    )
    with pytest.raises(RuntimeError):
        list(src.stream_batches("SELECT n FROM events", 2))


# --------------------------------------------------------------------------
# Rate limiting — the source this feature exists to protect
# --------------------------------------------------------------------------

def test_a_rate_limited_page_is_waited_out_and_retried(monkeypatch):
    waits = []
    monkeypatch.setattr(posthog_module.time, "sleep", waits.append)
    src, session = _source(
        [
            FakeResponse({"detail": "throttled"}, status_code=429,
                         headers={"Retry-After": "2"}),
            _page(["i"], [[1]]),
        ],
        monkeypatch,
        no_sleep=False,
    )
    cols, rows = src.preview("SELECT i FROM events", 10)

    assert waits == [2]
    assert rows == [[1]]
    assert len(session.queries) == 2, "the throttled page must be re-sent, not skipped"


def test_a_rate_limit_with_no_usable_retry_after_still_waits(monkeypatch):
    """Retrying immediately would burn the remaining attempts inside a
    second and turn a wait into a failure."""
    waits = []
    monkeypatch.setattr(posthog_module.time, "sleep", waits.append)
    src, _ = _source(
        [
            FakeResponse({}, status_code=429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            _page(["i"], [[1]]),
        ],
        monkeypatch,
        no_sleep=False,
    )
    src.preview("SELECT i FROM events", 10)
    assert waits and all(w >= 1 for w in waits)


def test_a_persistently_throttled_project_fails_rather_than_hanging(monkeypatch):
    src, session = _source(
        [FakeResponse({"detail": "throttled"}, status_code=429)], monkeypatch
    )
    with pytest.raises(RuntimeError, match="429"):
        src.preview("SELECT i FROM events", 10)
    assert len(session.queries) <= posthog_module.RATE_LIMIT_RETRIES + 1


def test_a_query_error_is_surfaced_with_the_servers_reason(monkeypatch):
    src, _ = _source(
        [FakeResponse({"detail": "Unable to resolve field: nope"}, status_code=400)],
        monkeypatch,
    )
    with pytest.raises(RuntimeError, match="Unable to resolve field"):
        src.preview("SELECT nope FROM events", 10)


# --------------------------------------------------------------------------
# The client contract the rest of the app depends on
# --------------------------------------------------------------------------

def test_execute_query_still_returns_a_dataframe(monkeypatch):
    """Extraction shares the HTTP call with the agent path; that path returns a
    DataFrame by contract and every existing caller depends on it."""
    client, _ = _client([_page(["event", "n"], [["$pageview", 3]])], monkeypatch)
    df = client.execute_query("SELECT event, count() AS n FROM events GROUP BY event")

    assert list(df.columns) == ["event", "n"]
    assert df.iloc[0]["n"] == 3


# --------------------------------------------------------------------------
# End to end — pages land in a real encrypted artifact
# --------------------------------------------------------------------------

def test_pages_materialize_into_a_queryable_artifact(monkeypatch, tmp_path):
    """The whole point: many bounded API pages become one local relation an
    agent can run unrestricted SQL over."""
    monkeypatch.setattr(artifacts, "_ARTIFACT_ROOT", tmp_path)
    client, _ = _client(
        [
            _page(["event", "n"], [["$pageview", 10], ["click", 4]]),
            _page(["event", "n"], [["$identify", 1]]),
            _page(["event", "n"], []),
        ],
        monkeypatch,
    )

    result = extractor.extract_to_artifact(
        client,
        "SELECT event, count() AS n FROM events GROUP BY event ORDER BY event",
        "top_events",
        "conn-1",
    )

    assert result.row_count == 3
    assert [c["name"] for c in result.columns] == ["event", "n"]

    con = artifacts.connect_encrypted(
        result.artifact_path, result.artifact_key, read_only=True
    )
    try:
        assert con.execute(
            'SELECT sum(n) FROM "top_events"'
        ).fetchone()[0] == 15
    finally:
        con.close()
