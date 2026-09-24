"""Every foreign key to an agent is either dropped by the database or handled
by the agent delete.

Deleting an agent removes its instructions and saved queries first and the
agent row last. A table referencing the agent that nobody clears makes that
last DELETE fail on Postgres — with the agent's content already gone. Such a
table must either delete/null itself (ON DELETE CASCADE / SET NULL), or be
listed in AGENT_DELETE_CLEARS (the delete clears it) or AGENT_DELETE_KEEPS
(deliberately left, with no database-level constraint). A new model that
references data_sources fails here until it picks one.
"""
import main  # noqa: F401 — registers all mappers

from app.models.base import metadata
from app.services.data_source_service import AGENT_DELETE_CLEARS, AGENT_DELETE_KEEPS


def _references_to_agents():
    for table in metadata.tables.values():
        for fk in table.foreign_keys:
            if fk.column.table.name == "data_sources":
                yield table.name, fk.parent.name, (fk.ondelete or "").upper()


def test_every_reference_to_an_agent_is_dropped_or_handled_by_the_delete():
    unhandled = sorted(
        f"{table}.{column}"
        for table, column, ondelete in _references_to_agents()
        if ondelete not in ("CASCADE", "SET NULL")
        and table not in AGENT_DELETE_CLEARS
        and table not in AGENT_DELETE_KEEPS
    )
    assert not unhandled, (
        "These reference data_sources with no ON DELETE rule and are not cleared by "
        f"DataSourceService.delete_data_source: {unhandled}. Clear them there and add them to "
        "AGENT_DELETE_CLEARS (or give the key ON DELETE CASCADE / SET NULL)."
    )


def test_the_handled_lists_name_only_real_references():
    """A stale entry would hide a table that stopped being cleared."""
    referencing = {table for table, _, _ in _references_to_agents()}
    assert AGENT_DELETE_CLEARS <= referencing, sorted(AGENT_DELETE_CLEARS - referencing)
    assert AGENT_DELETE_KEEPS <= referencing, sorted(AGENT_DELETE_KEEPS - referencing)
    assert not AGENT_DELETE_CLEARS & AGENT_DELETE_KEEPS
