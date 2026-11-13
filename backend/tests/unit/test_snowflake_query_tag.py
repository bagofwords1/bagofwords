"""Every query BOW sends to Snowflake can be traced back to its BOW origin.

The promise (BOW-115): a Snowflake admin reading QUERY_HISTORY finds, on each
BOW query, a JSON QUERY_TAG naming where it came from (report, conversation
turn, run, user, kind of run), and BOW keeps Snowflake's query id for each
query it ran, so the link works in both directions.

These tests drive the real SnowflakeClient, SQLAlchemy pool and pandas against
a fake Snowflake at the driver boundary (tests/fixtures/fake_snowflake.py), and
assert on what Snowflake itself would record.
"""

import json
import uuid

import pytest
import sqlalchemy.exc

from app.ai.code_execution.code_execution import wrap_clients_for_capture
from app.data_sources.clients.snowflake_client import SnowflakeClient
from app.data_sources.query_tag import (
    MAX_TAG_CHARS,
    current_query_tag,
    query_tag_defaults,
    query_tag_scope,
    render_query_tag,
)
from app.services.usage_policy_service import UsageLimitContext
from tests.fixtures.fake_snowflake import FakeSnowflakeServer


@pytest.fixture
def server(tmp_path, monkeypatch):
    srv = FakeSnowflakeServer(tmp_path / "warehouse.db", pool_size=1)
    srv.install(monkeypatch)
    import sqlite3

    with sqlite3.connect(srv.db_path) as c:
        c.execute("CREATE TABLE orders (id INTEGER, amount REAL)")
        c.executemany("INSERT INTO orders VALUES (?, ?)", [(1, 10.0), (2, 32.5), (3, 7.25)])
    return srv


def _client(**kwargs) -> SnowflakeClient:
    return SnowflakeClient(account="acct", warehouse="WH", database="DB", user="svc", password="pw", **kwargs)


def _tag(row) -> dict:
    assert row["query_tag"], f"query ran untagged: {row['query_text']!r}"
    return json.loads(row["query_tag"])


def _ids(n=1):
    return [str(uuid.uuid4()) for _ in range(n)]


# --- what lands in Snowflake's query history ------------------------------

def test_query_history_row_names_the_report_and_turn_that_ran_it(server):
    report_id, completion_id, org_id = _ids(3)
    client = _client()
    with query_tag_scope(source="agent", report_id=report_id, completion_id=completion_id, org_id=org_id):
        df = client.execute_query("SELECT id, amount FROM orders ORDER BY id")

    assert len(df) == 3
    row = server.rows()[-1]
    assert "FROM orders" in row["query_text"]
    tag = _tag(row)
    assert tag["app"] == "bow"
    assert tag["source"] == "agent"
    assert tag["report_id"] == report_id
    assert tag["completion_id"] == completion_id
    assert tag["org_id"] == org_id


def test_query_without_any_bow_context_is_still_identifiable_as_bow(server):
    _client().execute_query("SELECT COUNT(*) AS n FROM orders")
    tag = _tag(server.rows()[-1])
    assert tag["app"] == "bow"
    assert tag["source"] == "system"
    assert "report_id" not in tag


def test_pooled_session_never_carries_a_previous_runs_tag(server):
    """One pooled Snowflake session serves run A, then run B, then a run
    with no report: each statement is attributed to its own run only."""
    report_a, report_b = _ids(2)
    client = _client()
    with query_tag_scope(source="agent", report_id=report_a):
        client.execute_query("SELECT 1 AS a")
    with query_tag_scope(source="report_refresh.schedule", report_id=report_b):
        client.execute_query("SELECT 2 AS b")
    client.execute_query("SELECT 3 AS c")

    rows = server.rows()[-3:]
    assert len({r["session_id"] for r in rows}) == 1, "fixture should reuse one pooled session"
    tags = [_tag(r) for r in rows]
    assert tags[0]["report_id"] == report_a
    assert tags[1]["report_id"] == report_b and tags[1]["source"] == "report_refresh.schedule"
    assert "report_id" not in tags[2] and tags[2]["source"] == "system"


def test_a_runs_queries_do_not_retag_the_session_per_query(server):
    client = _client()
    with query_tag_scope(source="agent", report_id=_ids()[0]):
        for _ in range(4):
            client.execute_query("SELECT id FROM orders")
    alters = [r for r in server.rows(include_session_statements=True)
              if r["query_text"].lstrip().upper().startswith("ALTER SESSION")]
    assert len(alters) <= 1


def test_a_new_sessions_setup_statements_are_tagged_too(server):
    """The driver/dialect runs its own statements on a fresh session before
    BOW's code sees it; those count against the warehouse too."""
    report_id = _ids()[0]
    with query_tag_scope(source="agent", report_id=report_id):
        _client().execute_query("SELECT id FROM orders")
    statements = server.rows(include_session_statements=True)
    assert len(statements) >= 2, "fixture should show the session's setup statement"
    assert all(_tag(r)["report_id"] == report_id for r in statements)


def test_tagging_off_leaves_the_customers_session_tag_alone(server):
    client = _client(query_tag_enabled=False)
    with query_tag_scope(source="agent", report_id=_ids()[0]):
        client.execute_query("SELECT id FROM orders")
    assert not [r for r in server.rows(include_session_statements=True) if "QUERY_TAG" in r["query_text"]]
    assert server.rows()[-1]["query_tag"] == ""


def test_connection_label_and_ids_ride_along(server):
    client = _client(query_tag_label="finance team")
    client._bow_connection_id = "conn-1"
    client._bow_data_source_id = "ds-9"
    client.execute_query("SELECT 1 AS x")
    tag = _tag(server.rows()[-1])
    assert tag["label"] == "finance team"
    assert tag["connection_id"] == "conn-1"
    assert tag["data_source_id"] == "ds-9"


def test_unsafe_values_never_reach_snowflake_and_the_tag_stays_valid_json(server):
    client = _client()
    hostile = "x'); DROP TABLE orders; --"
    with query_tag_scope(source="agent", report_id=hostile, completion_id="ok-1\nsecond line"):
        client.execute_query("SELECT id FROM orders")
    tag = _tag(server.rows()[-1])
    assert "report_id" not in tag and "completion_id" not in tag
    # the table is still there: nothing executed from the tag
    assert len(client.execute_query("SELECT id FROM orders")) == 3


# --- the link back: BOW keeps Snowflake's query id ------------------------

def test_result_carries_snowflake_query_id_of_its_own_statement(server):
    df = _client().execute_query("SELECT id FROM orders")
    assert df.attrs["bow_source_query_id"] == server.rows()[-1]["query_id"]


def test_failed_statement_carries_its_snowflake_query_id(server):
    with pytest.raises(sqlalchemy.exc.DBAPIError) as info:
        _client().execute_query("SELECT nope FROM missing_table")
    failed = server.rows()[-1]
    assert failed["error"]
    assert getattr(info.value, "bow_source_query_id", None) == failed["query_id"]


# --- through the agent's query wrapper (timeout thread, metering context) -

def _wrapped(client, usage_context=None):
    timings: list = []
    wrapped = wrap_clients_for_capture({"wh": client}, [], timings, usage_context=usage_context)
    return wrapped["wh"], timings


def test_agent_wrapper_carries_the_runs_tag_onto_its_query_thread(server):
    """The wrapper runs each query on a fresh thread, which starts with no
    context; the run's tag must still reach Snowflake."""
    report_id, completion_id = _ids(2)
    with query_tag_scope(source="agent", report_id=report_id, completion_id=completion_id):
        wrapped, timings = _wrapped(_client())
    # Called outside the scope, as the sandbox's RPC thread would.
    wrapped.execute_query("SELECT id FROM orders")

    row = server.rows()[-1]
    tag = _tag(row)
    assert tag["report_id"] == report_id and tag["completion_id"] == completion_id
    assert timings[-1]["source_query_id"] == row["query_id"]


def test_wrapper_records_query_id_on_failed_query_timing(server):
    wrapped, timings = _wrapped(_client())
    with pytest.raises(sqlalchemy.exc.DBAPIError):
        wrapped.execute_query("SELECT nope FROM missing_table")
    assert timings[-1]["error"]
    assert timings[-1]["source_query_id"] == server.rows()[-1]["query_id"]


@pytest.mark.parametrize("source", ["query_run", "dashboard_filter_run", "mcp.create_data"])
def test_run_kind_comes_from_metering_context_when_nothing_more_specific_is_set(server, source):
    org_id, user_id, ref_id = _ids(3)
    usage = UsageLimitContext(organization_id=org_id, user_id=user_id, source=source, source_ref_id=ref_id)
    wrapped, _ = _wrapped(_client(), usage_context=usage)
    wrapped.execute_query("SELECT 1 AS x")
    tag = _tag(server.rows()[-1])
    assert (tag["source"], tag["org_id"], tag["user_id"], tag["ref_id"]) == (source, org_id, user_id, ref_id)


def test_specific_entry_point_wins_over_generic_metering_source(server):
    usage = UsageLimitContext(organization_id="o", user_id="u", source="query_run")
    with query_tag_scope(source="report_refresh.schedule"):
        wrapped, _ = _wrapped(_client(), usage_context=usage)
    wrapped.execute_query("SELECT 1 AS x")
    assert _tag(server.rows()[-1])["source"] == "report_refresh.schedule"


# --- tag assembly ----------------------------------------------------------

def test_inner_defaults_do_not_override_an_outer_source():
    # Outer entry point first, then the generic step runner inside it.
    with (
        query_tag_defaults(source="report_refresh.schedule", report_id="r1"),
        query_tag_defaults(source="step_run", step_id="s1"),
        query_tag_scope(step_id="s2"),
    ):
        tag = current_query_tag()
    assert tag["source"] == "report_refresh.schedule"
    assert tag["report_id"] == "r1"
    assert tag["step_id"] == "s2"


def test_rendered_tag_fits_snowflakes_limit_keeping_the_most_useful_keys():
    long = "a" * 100
    tag = {"app": "bow", "source": "agent", "report_id": "r-1",
           **dict.fromkeys(("completion_id", "agent_execution_id", "step_id", "query_id", "entity_id", "release_id", "connection_id", "data_source_id", "user_id", "ref_id", "label"), long)}
    small = render_query_tag(tag, max_chars=200)
    assert len(small) <= 200
    parsed = json.loads(small)
    assert parsed["app"] == "bow" and parsed["source"] == "agent" and parsed["report_id"] == "r-1"
    assert len(render_query_tag(tag)) <= MAX_TAG_CHARS
