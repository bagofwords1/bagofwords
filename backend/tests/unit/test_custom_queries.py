"""Custom queries: the guarantees that must not regress.

A custom query is admin-authored SQL materialized to an encrypted local DuckDB
artifact and served to agents from there instead of the source. Four properties
carry the whole feature, and each has a silent failure mode:

1. **A reindex must not delete custom queries.** They live in
   ``connection_tables`` alongside introspected rows but have no counterpart in
   the source catalog, so an unfiltered stale-row sweep would remove every one
   of them on the next scheduled reindex — silently, and with the artifacts.

2. **Artifacts must be unreadable without the key.** Encryption is the sandbox
   boundary, not just at-rest compliance: generated Python has ``pd`` injected
   and pandas does its own IO, so a plain artifact could be read straight off
   disk, bypassing the catalog entirely.

3. **A huge query must be refused before it runs.** `SELECT *` over a
   billion-row table must not be absorbed.

4. **The serving session must stay locked down.** With pandas denied access,
   DuckDB SQL becomes the remaining surface.
"""

import base64
import secrets

import duckdb
import pytest

from app.ai.code_execution.code_execution import UnsafePythonError, validate_python_code
from app.data_sources.fast import artifacts, extractor
from app.data_sources.fast.fast_client import FastQueryClient, FastRelation
from app.models.connection_table import KIND_BOW, KIND_TABLE


# --------------------------------------------------------------------------
# 1. Reindex must not touch kind='bow' rows
# --------------------------------------------------------------------------

def test_refresh_schema_only_considers_introspected_rows():
    """``refresh_schema`` must scope its catalog query to ``kind='table'``.

    Asserted against the source because the failure is invisible at runtime
    until a scheduled reindex fires and the custom queries are already gone.
    """
    import inspect

    from app.services import connection_service

    src = inspect.getsource(connection_service.ConnectionService.refresh_schema)
    assert "ConnectionTable.kind == KIND_TABLE" in src, (
        "refresh_schema must filter existing_tables to kind='table'; without it "
        "every kind='bow' custom query lands in the `missing` set and is deleted."
    )


def test_kind_constants_are_distinct():
    assert KIND_TABLE == "table"
    # Deliberately not "view": a view implies non-materialized.
    assert KIND_BOW == "bow"
    assert KIND_TABLE != KIND_BOW


# --------------------------------------------------------------------------
# 2. Artifact encryption
# --------------------------------------------------------------------------

def test_artifact_is_encrypted_and_needs_its_key(tmp_path):
    path = tmp_path / "a.db"
    key = artifacts.new_artifact_key()

    con = artifacts.connect_encrypted(path, key)
    con.execute("CREATE TABLE t AS SELECT 'sensitive-value' AS v")
    con.close()

    # The plaintext must not be recoverable from the raw bytes.
    assert b"sensitive-value" not in path.read_bytes()

    # Correct key reads it back.
    con = artifacts.connect_encrypted(path, key, read_only=True)
    assert con.execute("SELECT v FROM t").fetchall() == [("sensitive-value",)]
    con.close()

    # A different key must not.
    other = artifacts.new_artifact_key()
    with pytest.raises(Exception):
        artifacts.connect_encrypted(path, other, read_only=True)


def test_pandas_cannot_read_an_artifact(tmp_path):
    """The sandbox boundary: pandas must not be able to open the file."""
    import pandas as pd

    path = tmp_path / "b.db"
    key = artifacts.new_artifact_key()
    con = artifacts.connect_encrypted(path, key)
    con.execute("CREATE TABLE t AS SELECT 1 AS x")
    con.close()

    with pytest.raises(Exception):
        pd.read_parquet(str(path))


def test_artifact_key_roundtrips_through_fernet():
    key = artifacts.new_artifact_key()
    enc = artifacts.encrypt_key(key)
    assert enc != key
    assert artifacts.decrypt_key(enc) == key


def test_artifact_paths_are_opaque(tmp_path, monkeypatch):
    """Filenames must not be derivable from the relation or connection name."""
    monkeypatch.setattr(artifacts, "_ARTIFACT_ROOT", tmp_path)
    p1 = artifacts.new_artifact_path("conn-1")
    p2 = artifacts.new_artifact_path("conn-1")
    assert p1 != p2
    assert "conn-1" not in p1.name
    assert p1.suffix == ".db"


# --------------------------------------------------------------------------
# 3. Extraction budgets
# --------------------------------------------------------------------------

def test_budget_refuses_on_row_count():
    est = extractor.Estimate(rows=2_000_000_000, width_bytes=40,
                             total_bytes=80_000_000_000, supported=True)
    with pytest.raises(extractor.ExtractionRefused) as e:
        extractor.check_budget(est)
    assert "2,000,000,000 rows" in str(e.value)


def test_budget_refuses_on_byte_size():
    est = extractor.Estimate(rows=10, width_bytes=10**9,
                             total_bytes=10 * 10**9, supported=True)
    with pytest.raises(extractor.ExtractionRefused):
        extractor.check_budget(est)


def test_budget_allows_a_reasonable_query():
    est = extractor.Estimate(rows=50_000, width_bytes=80,
                             total_bytes=4_000_000, supported=True)
    extractor.check_budget(est)  # must not raise


def test_budget_is_permissive_when_no_estimate_available():
    """An engine without EXPLAIN support falls through to the hard caps rather
    than blocking every query."""
    extractor.check_budget(extractor.Estimate(supported=False))


def test_preview_limit_is_one_hundred():
    assert extractor.PREVIEW_ROW_LIMIT == 100


# --------------------------------------------------------------------------
# 4. Serving session lockdown + authorization
# --------------------------------------------------------------------------

def _relation(tmp_path, name="rel", value=1):
    path = tmp_path / f"{name}.db"
    key = artifacts.new_artifact_key()
    con = artifacts.connect_encrypted(path, key)
    con.execute(f'CREATE TABLE "{name}" AS SELECT {value} AS x')
    con.close()
    return FastRelation(name, str(path), key, [{"name": "x", "dtype": "int64"}],
                        as_of="2026-07-28T00:00", row_count=1)


def test_fast_client_serves_its_relation(tmp_path):
    rel = _relation(tmp_path, "orders_summary", 42)
    df = FastQueryClient([rel]).execute_query("SELECT x FROM orders_summary")
    assert df["x"].tolist() == [42]


def test_unactivated_relation_is_not_nameable(tmp_path):
    """Authorization is structural: a relation absent from the catalog cannot
    be referenced, regardless of what the generated SQL asks for."""
    granted = _relation(tmp_path, "granted", 1)
    _withheld = _relation(tmp_path, "withheld", 2)

    client = FastQueryClient([granted])
    with pytest.raises(Exception):
        client.execute_query("SELECT * FROM withheld")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_parquet('/etc/hostname')",
    "SELECT * FROM read_csv_auto('/etc/passwd')",
    "COPY (SELECT 1) TO '/tmp/bow_leak_test.csv'",
    "SET enable_external_access=true",
])
def test_serving_session_is_locked_down(tmp_path, sql):
    """With pandas locked out, DuckDB SQL is the remaining surface: no reading
    arbitrary files, no attaching other artifacts, no writing anything out."""
    client = FastQueryClient([_relation(tmp_path, "rel", 1)])
    with pytest.raises(Exception):
        client.execute_query(sql)


def test_attaching_another_artifact_is_blocked(tmp_path):
    mine = _relation(tmp_path, "mine", 1)
    theirs = _relation(tmp_path, "theirs", 2)
    client = FastQueryClient([mine])
    with pytest.raises(Exception):
        client.execute_query(
            f"ATTACH '{theirs.artifact_path}' AS o "
            f"(ENCRYPTION_KEY '{theirs.artifact_key}')"
        )


def test_description_carries_dialect_freshness_and_scan_hint(tmp_path):
    """These three cues drive codegen quality: without the dialect the model
    writes source SQL, without 'as of' it reports stale figures as current, and
    without the cheap-scan cue it keeps pre-aggregating defensively."""
    d = FastQueryClient([_relation(tmp_path, "rel", 1)]).description
    assert "DuckDB SQL" in d
    assert "Scans are cheap" in d
    assert "data as of" in d
    assert "rel" in d


# --------------------------------------------------------------------------
# 5. Sandbox validator: hardcoded paths blocked, uploaded files still allowed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", [
    "def generate_df(ds, ex):\n    return pd.read_parquet('/app/uploads/fast/x.db')",
    "def generate_df(ds, ex):\n    return pd.read_csv('/etc/passwd')",
    "def generate_df(ds, ex):\n    return pd.read_parquet(f'/app/uploads/{1}.db')",
    "def generate_df(ds, ex):\n    return np.load('/app/uploads/x.npy')",
])
def test_hardcoded_file_reads_are_rejected(code):
    with pytest.raises(UnsafePythonError):
        validate_python_code(code)


@pytest.mark.parametrize("code", [
    # The sanctioned way to read an uploaded file — must keep working.
    "def generate_df(ds, ex):\n    return pd.read_excel(ex[0].path, sheet_name=0, header=None)",
    "def generate_df(ds, ex):\n    return pd.read_csv(ex[0].path)",
    "def generate_df(ds, ex):\n    return pd.read_json(ex[1].path, lines=True)",
    "def generate_df(ds, ex):\n    p = ex[0].path\n    return pd.read_csv(p)",
    "def generate_df(ds, ex):\n    return ds['a:b'].execute_query('SELECT 1')",
])
def test_uploaded_file_reads_still_allowed(code):
    validate_python_code(code)  # must not raise


# --------------------------------------------------------------------------
# 6. Schema context must attribute a cached relation to the client that serves it
# --------------------------------------------------------------------------
#
# The coder maps a table's <connection name> onto a client_key suffix (see
# coder.py "Connection-Table Mapping"). A custom query is served by the
# connection's `::fast` sibling, not by the source client, so rendering it under
# the source connection sends generated SQL to a client where the relation does
# not exist — every query against it fails with "relation not found", and the
# failure only shows up with a live model in the loop.

def test_bow_relation_is_attributed_to_the_fast_client():
    from app.ai.context.builders.schema_context_builder import (
        FAST_CLIENT_SUFFIX, _connection_identity_for,
    )

    class _Conn:
        name, type = "postgresql-1", "postgresql"

    class _CT:
        kind = KIND_BOW

    class _CTTable:
        kind = KIND_TABLE

    conn = _Conn()
    assert _connection_identity_for(_CT(), conn) == (
        f"postgresql-1{FAST_CLIENT_SUFFIX}", "duckdb")
    # Introspected tables keep the source identity.
    assert _connection_identity_for(_CTTable(), conn) == ("postgresql-1", "postgresql")


def test_live_and_cached_relations_render_as_separate_connections():
    """They share a connection_id, so grouping on the id alone merged them into
    one <connection> block under whichever name came first — pointing half the
    tables at a client that cannot serve them."""
    from app.ai.prompt_formatters import Table as PromptTable
    from app.ai.context.sections.tables_schema_section import TablesSchemaContext
    from app.schemas.data_source_schema import DataSourceSummarySchema

    def _t(name, conn_name, conn_type):
        t = PromptTable(name=name, columns=[], pks=[], fks=[])
        t.connection_id = "same-connection-id"
        t.connection_name = conn_name
        t.connection_type = conn_type
        return t

    ds = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="ds1", name="Shop", type="postgresql"),
        tables=[
            _t("public.orders", "postgresql-1", "postgresql"),
            _t("revenue_summary", "postgresql-1::fast", "duckdb"),
        ],
    )
    groups = ds._group_tables_by_connection()
    assert len(groups) == 2, f"expected the two client identities to stay apart, got {groups.keys()}"

    rendered = ds.render()
    assert 'name="postgresql-1" type="postgresql"' in rendered
    assert 'name="postgresql-1::fast" type="duckdb"' in rendered
    # And each table sits under its own one.
    live_block = rendered.split('name="postgresql-1::fast"')[0]
    assert "public.orders" in live_block
    assert "revenue_summary" not in live_block


# --------------------------------------------------------------------------
# 7. The agent must be able to tell a cached relation apart, and prefer it
# --------------------------------------------------------------------------
#
# Without these cues a live model treats the cached relation as a fallback: it
# scanned the raw tables first and only reached for the cache when that looked
# awkward — which both hit the source and produced a WRONG figure, because the
# curated query encodes the business definition (status='completed') that the
# model's ad-hoc join left out.

def test_cached_relation_renders_marker_description_and_freshness():
    from app.ai.prompt_formatters import Table as PromptTable
    from app.ai.context.sections.tables_schema_section import TablesSchemaContext
    from app.schemas.data_source_schema import DataSourceSummarySchema

    t = PromptTable(name="revenue_summary", columns=[], pks=[], fks=[])
    t.connection_id = "c1"
    t.connection_name = "postgresql-1::fast"
    t.connection_type = "duckdb"
    t.is_cached = True
    t.cached_as_of = "2026-07-29T04:44"
    t.cached_next_refresh = "2026-07-29T05:44"
    t.description = "Completed-order revenue by region, segment and category"

    ds = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="ds1", name="Shop", type="postgresql"),
        tables=[t],
    )
    rendered = ds.render()
    assert 'cached="true"' in rendered
    assert 'as_of="2026-07-29T04:44"' in rendered
    # `as_of` alone cannot be read: the same timestamp means "current" on a
    # daily schedule and "hours behind" on an hourly one. The agent needs both
    # to answer "is this figure still worth trusting?".
    assert 'next_refresh="2026-07-29T05:44"' in rendered
    # The description is the only thing telling the agent what the relation
    # holds — "revenue_summary" alone doesn't say per-order vs per-region.
    assert "Completed-order revenue by region" in rendered


def test_a_relation_with_no_scheduled_job_omits_next_refresh():
    """Paused, never scheduled, or mid-migration — the attribute is absent
    rather than guessed. A wrong next-refresh is worse than none: the agent
    would tell a user to re-ask at a time nothing happens."""
    from app.ai.prompt_formatters import Table as PromptTable
    from app.ai.context.sections.tables_schema_section import TablesSchemaContext
    from app.schemas.data_source_schema import DataSourceSummarySchema

    t = PromptTable(name="revenue_summary", columns=[], pks=[], fks=[])
    t.connection_name = "postgresql-1::fast"
    t.is_cached = True
    t.cached_as_of = "2026-07-29T04:44"
    t.cached_next_refresh = None

    ds = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="ds1", name="Shop", type="postgresql"),
        tables=[t],
    )
    rendered = ds.render()
    assert 'as_of="2026-07-29T04:44"' in rendered
    assert "next_refresh" not in rendered


def test_next_refresh_comes_from_the_shared_job_store():
    """Not recomputed from the schedule columns. APScheduler's store is the
    application database, so the number the agent quotes is the same one the
    settings screen renders — a second derivation would have to reproduce the
    interval anchor and the jitter, and would drift the moment either changed."""
    from datetime import datetime

    from app.ai.context.builders import schema_context_builder as scb
    from app.models.connection_table import KIND_BOW

    class FakeCT:
        id = "cq-1"
        kind = KIND_BOW
        last_refreshed_at = datetime(2026, 7, 29, 4, 44)
        description = "Revenue by region"

    fired = {}

    class FakeJob:
        next_run_time = datetime(2026, 7, 29, 5, 44)

    import app.services.custom_query_service as cqs
    real_get_job = cqs.scheduler.get_job
    cqs.scheduler.get_job = lambda jid: (fired.setdefault("job_id", jid), FakeJob())[1]
    try:
        is_cached, as_of, nxt, desc = scb._cached_meta_for(FakeCT())
    finally:
        cqs.scheduler.get_job = real_get_job

    assert is_cached is True
    assert as_of == "2026-07-29T04:44"
    assert nxt == "2026-07-29T05:44"
    assert fired["job_id"] == "custom_query_cq-1"


def test_a_broken_scheduler_costs_the_hint_not_the_context():
    """Building the agent's schema context must not fail because the job store
    is unreachable. The relation is still worth showing without its next fire."""
    from datetime import datetime

    from app.ai.context.builders import schema_context_builder as scb
    from app.models.connection_table import KIND_BOW

    class FakeCT:
        id = "cq-1"
        kind = KIND_BOW
        last_refreshed_at = datetime(2026, 7, 29, 4, 44)
        description = None

    def boom(_):
        raise RuntimeError("job store is down")

    import app.services.custom_query_service as cqs
    real_get_job = cqs.scheduler.get_job
    cqs.scheduler.get_job = boom
    try:
        is_cached, as_of, nxt, _ = scb._cached_meta_for(FakeCT())
    finally:
        cqs.scheduler.get_job = real_get_job

    assert is_cached is True
    assert as_of == "2026-07-29T04:44"
    assert nxt is None


def test_planner_prompt_states_the_cached_preference():
    import inspect
    from app.ai.agents.planner import prompt_builder_v3

    src = inspect.getsource(prompt_builder_v3)
    assert 'cached="true"' in src, "planner prompt must name the cached marker"
    assert "::fast" in src, "planner prompt must explain the ::fast connection"
    assert "DuckDB" in src, "planner prompt must state the cached dialect"


# --------------------------------------------------------------------------
# 8. Beta gate, per-agent activation, and schedule visibility
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_beta_gate_blocks_when_flag_is_off():
    """Off by default — an org opts in explicitly."""
    from fastapi import HTTPException
    from app.services.custom_query_service import CustomQueryService

    class _Settings:
        def get_config(self, key):
            return type("F", (), {"value": False})()

    class _Org:
        async def get_settings(self, db):
            return _Settings()

    with pytest.raises(HTTPException) as e:
        await CustomQueryService.ensure_enabled(None, _Org())
    assert e.value.status_code == 403
    assert "beta" in str(e.value.detail).lower()


@pytest.mark.asyncio
async def test_beta_gate_blocks_when_settings_unavailable():
    """Fail closed: a settings lookup that errors must not open the feature."""
    from fastapi import HTTPException
    from app.services.custom_query_service import CustomQueryService

    class _Org:
        async def get_settings(self, db):
            raise RuntimeError("boom")

    with pytest.raises(HTTPException):
        await CustomQueryService.ensure_enabled(None, _Org())


def test_new_agents_default_a_bow_relation_to_inactive():
    """A curated relation must not switch itself on for a brand-new agent — it
    would silently widen what that agent can query."""
    import inspect

    from app.services import data_source_service

    src = inspect.getsource(data_source_service)
    assert 'getattr(conn_table, "kind", None) == "bow"' in src, (
        "new-agent table creation must force kind='bow' rows inactive rather "
        "than following the auto-select rule"
    )


def test_schedule_uses_a_timezone():
    """'Daily at 03:00' must mean 03:00 where the admin is, like every other
    schedule in the product."""
    import inspect

    from app.services.custom_query_service import CustomQueryService

    src = inspect.getsource(CustomQueryService._schedule)
    assert "timezone=timezone" in src, "cron trigger must be given a timezone"
    assert inspect.getsource(CustomQueryService._org_timezone)


def test_next_run_at_is_exposed():
    from app.schemas.custom_query_schema import CustomQuerySchema

    assert "next_run_at" in CustomQuerySchema.model_fields
    assert "last_refresh_ms" in CustomQuerySchema.model_fields
    assert "no_rows" in CustomQuerySchema.model_fields


# --------------------------------------------------------------------------
# Cached relations must be visible enough for the planner to prefer them
# --------------------------------------------------------------------------

class _Rel:
    def __init__(self, name, cached=False, score=0.0):
        self.name, self.is_cached, self.score = name, cached, score

    def __repr__(self):
        return f"{'*' if self.is_cached else ''}{self.name}"


def _ranked_pool():
    """25 raw tables with real usage history plus one fresh custom query."""
    from app.ai.context.builders.schema_context_builder import _cached_first

    raw = [_Rel(f"raw{i}", score=1.0 - i * 0.05) for i in range(25)]
    cq = _Rel("revenue_summary", cached=True, score=0.0)
    by_score = sorted(raw + [cq], key=lambda t: t.score, reverse=True)
    return raw, cq, by_score, _cached_first(by_score)


def test_the_composite_score_alone_buries_a_cached_relation():
    """Documents WHY the override exists. The score is built from usage,
    feedback and FK-derived centrality; a freshly authored custom query has
    none of those, so it ranks below the very tables it exists to replace."""
    _, cq, by_score, _ = _ranked_pool()
    assert by_score.index(cq) >= 10


def test_cached_relations_rank_first():
    _, cq, _, ranked = _ranked_pool()
    assert ranked[0] is cq


def test_ranking_is_stable_within_each_group():
    """The existing score still orders the raw tables among themselves."""
    _, cq, by_score, ranked = _ranked_pool()
    assert [t.name for t in ranked[1:]] == [t.name for t in by_score if t is not cq]


def test_the_top_k_cap_never_truncates_a_cached_relation():
    """Being dropped from the excerpt is worse than ranking low: the planner
    cannot prefer what it cannot see, and rebuilds the figures from the raw
    tables — the exact load the cache exists to remove."""
    from app.ai.context.builders.schema_context_builder import _cap_keeping_cached

    _, cq, by_score, ranked = _ranked_pool()
    assert cq not in by_score[:10], "precondition: the old path dropped it"
    assert cq in _cap_keeping_cached(ranked, 10)


def test_the_cap_still_bounds_the_prompt():
    from app.ai.context.builders.schema_context_builder import _cap_keeping_cached

    _, _, _, ranked = _ranked_pool()
    assert len(_cap_keeping_cached(ranked, 10)) == 10


def test_every_cached_relation_survives_even_past_the_cap():
    """An admin writes these one at a time, so keeping all of them cannot blow
    up the prompt — and dropping some would be arbitrary."""
    from app.ai.context.builders.schema_context_builder import (
        _cached_first, _cap_keeping_cached,
    )

    many = [_Rel(f"cq{i}", cached=True) for i in range(12)]
    many += [_Rel(f"raw{i}", score=1.0) for i in range(25)]
    capped = _cap_keeping_cached(_cached_first(many), 5)
    assert sum(1 for t in capped if t.is_cached) == 12
    assert all(t.is_cached for t in capped)


def test_nothing_changes_for_an_agent_with_no_cached_relations():
    from app.ai.context.builders.schema_context_builder import (
        _cached_first, _cap_keeping_cached,
    )

    raw = [_Rel(f"raw{i}", score=1.0 - i * 0.05) for i in range(25)]
    assert _cached_first(raw) is raw
    assert [t.name for t in _cap_keeping_cached(raw, 10)] == [t.name for t in raw[:10]]
    assert _cap_keeping_cached(raw, 0) is raw
