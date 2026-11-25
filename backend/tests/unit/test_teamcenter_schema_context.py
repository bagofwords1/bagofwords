"""A Teamcenter connection must reach the planner as tables AND documents.

Its parts, BOMs and changes are tables; the specifications attached to them are
read live through the file tools. A live run with Haiku answered "this source
has no specification documents" because the planner saw only the tables: the
connector's query guide reaches the code writer, not the planner.
"""
from types import SimpleNamespace

import pytest

from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
from app.ai.context.sections.tables_schema_section import FileScopeItem, TablesSchemaContext
from app.schemas.data_source_schema import DataSourceSummarySchema


def _conn(cid="conn-tc", type_="teamcenter", policy="system_only", **cfg):
    return SimpleNamespace(id=cid, name="Teamcenter", type=type_, auth_policy=policy,
                           allowed_user_auth_modes=["oauth"] if policy == "user_required" else None,
                           config={"base_url": "https://plm.example.com/tc", **cfg})


def _tables(cid="conn-tc"):
    return [SimpleNamespace(name=n, connection_id=cid, metadata_json={}) for n in ("a2_part_revision", "bom")]


def _scopes(*conns, tables=None):
    builder = SchemaContextBuilder.__new__(SchemaContextBuilder)
    return builder._build_file_scopes(SimpleNamespace(connections=list(conns)), tables if tables is not None else _tables())


def test_teamcenter_keeps_its_tables_and_adds_one_document_scope():
    scopes, tables = _scopes(_conn())
    assert [t.name for t in tables] == ["a2_part_revision", "bom"], "tables stay in the schema"
    assert [(s.type, s.connection_id) for s in scopes] == [("teamcenter", "conn-tc")]


def test_documents_switched_off_add_no_scope():
    scopes, tables = _scopes(_conn(include_documents=False))
    assert scopes == [] and len(tables) == 2


def test_a_file_source_beside_it_is_unchanged():
    share = _conn(cid="conn-fs", type_="network_dir", root_path="/specs")
    files = [SimpleNamespace(name="a.pdf", connection_id="conn-fs", metadata_json={})]
    scopes, tables = _scopes(_conn(), share, tables=_tables() + files)
    assert {s.type for s in scopes} == {"teamcenter", "network_dir"}
    assert [t.name for t in tables] == ["a2_part_revision", "bom"], "only the file source's rows leave the table pool"


def _render(per_user: bool, mode: str) -> str:
    ds = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="plm", name="PLM", type="teamcenter"),
        tables=[],
        file_scopes=[FileScopeItem(connection_id="conn-tc", name="Teamcenter", type="teamcenter",
                                   base="https://plm.example.com/tc", index_mode="none", supports_search=True,
                                   per_user=per_user, enforces_scope=False)],
    )
    ctx = TablesSchemaContext(data_sources=[ds])
    out = ctx.render_combined(top_k_per_ds=10, index_limit=200) if mode == "combined" else ctx.render("full")
    block = out[out.index('type="teamcenter"'):]
    return block[: block.index("</connection>")]


@pytest.mark.parametrize("mode", ["combined", "full"])
@pytest.mark.parametrize("per_user", [False, True])
def test_the_planner_is_told_to_find_and_read_the_documents(per_user, mode):
    block = _render(per_user, mode)
    assert 'kind="documents"' in block and 'id="conn-tc"' in block
    for tool in ("search_files", "list_files", "read_file"):
        assert tool in block
    assert "BEFORE concluding the answer is unavailable" in block
    assert "<index " not in block and "<scope>" not in block, "nothing indexed, no path scope"
    assert ("each signed-in user" in block) is per_user
