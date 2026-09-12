"""The context must name every connection an agent has, and stay flat in cost.

Three properties, all of which failed before:

1. ROSTER. Only the top-K tables render inside a <connection> block and the
   index is capped (agent_v2.INDEX_LIMIT), so on a large agent both are a
   sample. At 100 connections x 100 tables the agent saw 10 connections and
   had no way to learn the other 90 existed — it answered "1 PowerBI
   connection ... 18 tables total" for a three-connection agent, and would
   answer "10" for a hundred. <connections> is the complete list.

2. ROUND-ROBIN. Rank-ordered truncation clusters: the first 1000 rows all came
   from a handful of connections, so ninety connections had every table cut.
   Interleaving costs each connection depth, never existence.

3. ALIAS. Spelling the connection name onto every index item cost 30-52 bytes
   each — at the 1000-item cap, a third of the whole rendered context to
   repeat a handful of strings a thousand times.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/unit/test_connection_roster_and_scale.py -v
"""
import re

import pytest

from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.ai.prompt_formatters import Table as PromptTable, TableColumn
from app.schemas.data_source_schema import DataSourceSummarySchema


def _table(name, conn_name, conn_type="postgresql", conn_id=None):
    return PromptTable(
        name=name,
        columns=[TableColumn(name="a", dtype="text"),
                 TableColumn(name="b", dtype="text")],
        pks=[], fks=[],
        connection_id=conn_id or f"id-{conn_name}",
        connection_name=conn_name,
        connection_type=conn_type,
        is_active=True,
    )


def _ds(n_conns, n_tables_each):
    return TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="ds-1", name="Big Agent", type="postgresql"),
        tables=[
            _table(f"schema_{c:03d}.table_{t:03d}", f"warehouse_{c:03d}")
            for c in range(n_conns) for t in range(n_tables_each)
        ],
    )


def _roster_entries(xml):
    """(id, name) per <connection> element, head and tail alike, in order."""
    out = []
    for el in re.findall(r'<connection\b[^>]*/>', xml):
        cid = re.search(r'\bid="([^"]*)"', el)
        name = re.search(r'\bname="([^"]*)"', el)
        out.append((cid.group(1) if cid else None, name.group(1) if name else None))
    return out


def _roster_names(xml):
    return [n for _, n in _roster_entries(xml)]


@pytest.mark.parametrize("n_conns,n_each", [(3, 5), (12, 100), (100, 100)])
def test_every_connection_is_named_in_the_roster(n_conns, n_each):
    ds = _ds(n_conns, n_each)
    xml = ds._render_connections_roster_xml()
    assert f'<connections count="{n_conns}">' in xml
    names = _roster_names(xml)
    assert len(names) == n_conns, f"{n_conns - len(names)} connections unnamed"
    assert names[0] == "warehouse_000" and names[-1] == f"warehouse_{n_conns - 1:03d}"
    # The tool that reaches the truncated tail must be named for the model.
    assert "describe_tables" in xml


def test_single_connection_agent_pays_nothing():
    """No roster and no per-item alias when there is nothing to disambiguate."""
    ds = _ds(1, 20)
    assert ds._render_connections_roster_xml() == ""
    index = ds._render_names_index(200)
    assert ' c="' not in index


def test_index_truncation_keeps_every_connection_present():
    """1000-item cap over 100x100: each connection keeps a share rather than
    ninety of them being cut entirely."""
    ds = _ds(100, 100)
    index = ds._render_names_index(1000)
    assert 'truncated="true"' in index and 'count="10000"' in index
    items = re.findall(r"<item [^>]*/>", index)
    assert len(items) == 1000
    aliases = {re.search(r'\sc="([^"]+)"', i).group(1) for i in items}
    assert len(aliases) == 100, f"only {len(aliases)} of 100 connections survived truncation"


def test_alias_is_cheaper_than_the_name_it_replaces():
    ds = _ds(100, 100)
    index = ds._render_names_index(1000)
    alias_bytes = sum(len(m) for m in re.findall(r'\sc="[^"]*"', index))
    # What spelling the name out on every item would have cost.
    name_bytes = sum(
        len(' connection="%s"' % t.connection_name) for t in ds.tables[:1000]
    )
    # Measured 6,920B vs 27,000B here — a 74% cut, and this is the WORST case:
    # these fixture names are 13 characters. A real name like "Production
    # Snowflake EU-West Analytics" widens the gap, because the alias stays
    # 1-3 digits however long the name is.
    assert alias_bytes < name_bytes * 0.35, (
        f"alias {alias_bytes}B vs names {name_bytes}B — saving is only "
        f"{100 * (1 - alias_bytes / name_bytes):.0f}%"
    )
    # And the saving must grow, not shrink, with realistic names.
    long_names = sum(
        len(' connection="Production Snowflake EU-West Analytics"')
        for _ in ds.tables[:1000]
    )
    assert alias_bytes < long_names * 0.15


def test_roster_is_built_from_the_scoped_tables_not_the_agents_connections():
    """A connection the caller cannot see contributes no tables, so it must
    contribute no roster entry — otherwise the roster re-leaks exactly what the
    per-connection scoping withholds."""
    ds = _ds(3, 4)
    # Simulate scoping having dropped connection 001 entirely.
    ds.tables = [t for t in ds.tables if t.connection_name != "warehouse_001"]
    names = _roster_names(ds._render_connections_roster_xml())
    assert names == ["warehouse_000", "warehouse_002"]
    assert "warehouse_001" not in ds._render_connections_roster_xml()


@pytest.mark.parametrize("n_conns", [3, 100, 260])
def test_every_roster_entry_carries_its_uuid(n_conns):
    """describe_tables takes connection_ids, not names or c= aliases. A roster
    entry without an id names a connection the agent can see but cannot scope a
    discovery call to — so the tail past the cap needs the id most of all."""
    ds = _ds(n_conns, 2)
    entries = _roster_entries(ds._render_connections_roster_xml())
    assert len(entries) == n_conns
    assert all(cid for cid, _ in entries), "some connections rendered without an id"
    assert entries[0] == ("id-warehouse_000", "warehouse_000")
    assert entries[-1] == (
        f"id-warehouse_{n_conns - 1:03d}", f"warehouse_{n_conns - 1:03d}"
    )


def test_duplicate_connection_names_stay_separate():
    """Two connections may share a display name. Keyed on the name they merged
    into one roster entry with one table count, and the agent had no way to ask
    for either of them specifically."""
    ds = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="ds-1", name="Dup Agent", type="postgresql"),
        tables=(
            [_table(f"a.t{i}", "Warehouse", conn_id="conn-a") for i in range(3)]
            + [_table(f"b.t{i}", "Warehouse", conn_id="conn-b") for i in range(2)]
        ),
    )
    xml = ds._render_connections_roster_xml()
    assert '<connections count="2">' in xml
    assert _roster_entries(xml) == [("conn-a", "Warehouse"), ("conn-b", "Warehouse")]
    assert 'tables="3"' in xml and 'tables="2"' in xml
