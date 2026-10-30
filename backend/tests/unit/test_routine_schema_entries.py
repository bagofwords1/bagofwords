"""Routines as schema entries: grouping, naming and rendering contracts.

app.data_sources.routines turns catalog rows into `Table` entries marked
``metadata_json["type"] == "routine"``; the schema renderer then shows them
as <routine>, never as <table>. Engine catalog SQL is covered against real
databases in tests/integrations/test_routine_discovery.py; this covers the
engine-independent rules.
"""
import pytest

from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients.progress import IndexingCancelled
from app.data_sources.routines import (
    MAX_DEFINITION_CHARS,
    RoutineVariant,
    build_routine_tables,
    is_routine,
    routine_kind,
    routine_meta,
    with_routines,
)
from app.schemas.data_source_schema import DataSourceSummarySchema


def _qualified(schema, name):
    return f"{schema}.{name}"


def _variant(name="fn", kind="function", schema="s", **kw):
    kw.setdefault("definition", f"CREATE {kind.upper()} {schema}.{name} ...")
    return RoutineVariant(schema=schema, name=name, kind=kind, **kw)


def _table(name):
    return Table(name=name, columns=[TableColumn(name="id", dtype="int")], pks=[], fks=[])


# --- grouping & naming ------------------------------------------------------


@pytest.mark.parametrize("n_variants", [2, 3])
def test_variants_sharing_a_name_become_one_entry_with_every_signature(n_variants):
    variants = [_variant(params=[{"name": f"p{i}", "dtype": "int"}], definition=f"BODY {i}")
                for i in range(n_variants)]
    [entry] = build_routine_tables(variants, [], _qualified)
    meta = routine_meta(entry)
    assert len(meta["signatures"]) == n_variants
    assert all(f"BODY {i}" in meta["definition"] for i in range(n_variants))
    assert meta["overloads"] == n_variants


def test_mixed_kinds_under_one_name_read_as_procedure():
    [entry] = build_routine_tables(
        [_variant(kind="function"), _variant(kind="procedure")], [], _qualified)
    assert routine_kind(entry) == "procedure"


def test_a_routine_never_takes_a_tables_name():
    tables = ["s.orders", "s.customers"]
    entries = build_routine_tables([_variant(name="orders"), _variant(name="totals")], tables, _qualified)
    names = {e.name for e in entries}
    assert names.isdisjoint(tables)
    assert "s.totals" in names
    renamed = next(e for e in entries if e.name.startswith("s.orders"))
    # The signature keeps the name the engine knows, so the agent can call it.
    assert routine_meta(renamed)["signatures"][0].startswith("function s.orders(")


def test_long_source_is_capped_and_marked():
    [entry] = build_routine_tables(
        [_variant(definition="x" * (MAX_DEFINITION_CHARS + 500))], [], _qualified)
    meta = routine_meta(entry)
    assert len(meta["definition"]) == MAX_DEFINITION_CHARS
    assert meta["definition_truncated"] is True


def test_unreadable_source_is_flagged_and_not_stored():
    [entry] = build_routine_tables(
        [_variant(definition=None, definition_available=False)], [], _qualified)
    meta = routine_meta(entry)
    assert meta["definition_available"] is False
    assert "definition" not in meta


def test_parameters_become_parameter_columns():
    [entry] = build_routine_tables([_variant(params=[
        {"name": "@from", "dtype": "date", "mode": "IN"},
        {"name": "@rows", "dtype": "int", "mode": "OUT"},
    ])], [], _qualified)
    assert [(c.name, c.metadata["role"], c.metadata["mode"]) for c in entry.columns] == [
        ("@from", "parameter", "IN"), ("@rows", "parameter", "OUT")]
    assert "OUT @rows int" in routine_meta(entry)["signatures"][0]


def test_is_routine_reads_objects_and_raw_metadata():
    [entry] = build_routine_tables([_variant()], [], _qualified)
    assert is_routine(entry) and is_routine(entry.metadata_json)
    assert not is_routine(_table("t")) and not is_routine({"type": "semantic_view"})
    assert routine_kind(_table("t")) is None


# --- discovery is additive ----------------------------------------------------


def test_routines_are_appended_after_the_tables():
    tables = [_table("s.a"), _table("s.b")]
    routines = build_routine_tables([_variant()], [], _qualified)
    out = with_routines(tables, lambda: routines, source="test")
    assert [t.name for t in out[:2]] == ["s.a", "s.b"]
    assert [t.name for t in out[2:]] == [r.name for r in routines]


def test_a_failing_routine_read_returns_the_tables_unchanged():
    tables = [_table("s.a")]

    def boom():
        raise RuntimeError("permission denied for pg_proc")

    assert with_routines(tables, boom, source="test") == tables


def test_routines_are_not_read_when_the_connection_has_not_opted_in():
    called = []
    tables = [_table("s.a")]
    out = with_routines(tables, lambda: called.append(1) or [], source="test", enabled=False)
    assert out == tables and called == []


def test_a_failing_routine_read_marks_routines_unread_on_the_client():
    from app.data_sources.clients.base import DataSourceClient

    class _Client:
        unindexed_entry_types = DataSourceClient.unindexed_entry_types

    client = _Client()
    assert client.unindexed_entry_types() == []

    def boom():
        raise RuntimeError("canceling statement due to statement timeout")

    with_routines([_table("s.a")], boom, source="test", client=client)
    assert client.unindexed_entry_types() == ["routine"]

    # The next successful run clears it, so dropped routines prune again.
    with_routines([_table("s.a")], lambda: [], source="test", client=client)
    assert client.unindexed_entry_types() == []


def test_no_tables_means_no_routines():
    # [] is how table discovery reports failure; routines alone would read as
    # "every table was dropped" and an authoritative reindex would prune them.
    called = []
    assert with_routines([], lambda: called.append(1) or [], source="test") == []
    assert called == []


def test_cancellation_is_not_swallowed():
    def cancelled():
        raise IndexingCancelled("stop")

    with pytest.raises(IndexingCancelled):
        with_routines([_table("s.a")], cancelled, source="test")


# --- rendering ----------------------------------------------------------------


def _context(entries):
    info = DataSourceSummarySchema(id="ds1", name="Sales", type="postgresql", context=None)
    return TablesSchemaContext(data_sources=[TablesSchemaContext.DataSource(info=info, tables=entries)])


def _entries():
    return [_table("s.orders")] + build_routine_tables([
        _variant(name="net_revenue", kind="procedure", definition="SELECT ... WHERE status = 'complete'"),
        _variant(name="hidden_fn", definition=None, definition_available=False),
    ], ["s.orders"], _qualified)


@pytest.mark.parametrize("render", [
    lambda ctx: ctx.render_combined(top_k_per_ds=10),
    lambda ctx: ctx.render(),
])
def test_standing_context_shows_signatures_not_source(render):
    out = render(_context(_entries()))
    assert '<routine name="s.net_revenue" kind="procedure"' in out
    assert '<table name="s.net_revenue"' not in out
    assert '<table name="s.orders"' in out
    assert "<routines_note>" in out
    assert "<definition" not in out and "status = " not in out


def test_describe_mode_adds_source_and_flags_unreadable_routines():
    out = _context(_entries()).render_combined(top_k_per_ds=10, include_routine_definitions=True)
    proc = out.split('<routine name="s.net_revenue"', 1)[1].split("</routine>", 1)[0]
    assert "status = &#x27;complete&#x27;" in proc or "status = 'complete'" in proc
    hidden = out.split('<routine name="s.hidden_fn"', 1)[1].split("</routine>", 1)[0]
    assert 'available="false"' in hidden


def test_agent_without_routines_gets_no_routines_note():
    out = _context([_table("s.orders")]).render_combined(top_k_per_ds=10)
    assert "<routines_note>" not in out and "<routine " not in out


def test_index_marks_routines_by_kind():
    out = _context(_entries()).render_combined(top_k_per_ds=1, index_limit=50)
    index = out.split("<index", 1)[1]
    assert 'name="s.net_revenue" kind="procedure"' in index


def test_text_formatter_never_emits_create_table_for_a_routine():
    routine = build_routine_tables([_variant(name="net_revenue", kind="procedure")], [], _qualified)[0]
    out = TableFormatter([routine]).table_str
    assert "CREATE TABLE" not in out
    assert "procedure s.net_revenue(" in out
