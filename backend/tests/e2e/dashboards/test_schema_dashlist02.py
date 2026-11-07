"""dashlist02: an index for the dashboards list's agent filter (2026-11-05).

`report_data_source_association(report_id, data_source_id)`: the list's
agent filter and the agents facet look a draft dashboard's conversation up
by `report_id` (dashboard_list.ReaderScope.has_agent, facet_queries), and
the table had no index at all. Non-unique: the table has no key, and a
conversation may link an agent twice.

Additive. The upgrade and the downgrade are idempotent both ways: the
dashboard corpus applies the upgrade unstamped and undoes it unstamped.
"""
import pytest
from sqlalchemy import create_engine, func, inspect, select

from tests.fixtures.dashboard_corpus.alembic_at import alembic_pointed_at, current_revisions, script_directory
from tests.fixtures.dashboard_corpus.rows import sync_test_url

PREVIOUS = "dashlist01"
TABLE = "report_data_source_association"
INDEX = "ix_report_data_source_association_report_ds"


def _indexes(url) -> dict:
    engine = create_engine(url)
    try:
        return {i["name"]: i for i in inspect(engine).get_indexes(TABLE)}
    finally:
        engine.dispose()


def _run_module(url, *calls: str) -> None:
    """Run dashlist02's upgrade()/downgrade() on their own, unstamped -- as
    the dashboard corpus harness does."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    module = script_directory().get_revision("dashlist02").module
    engine = create_engine(url)
    try:
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            for call in calls:
                getattr(module, call)()
    finally:
        engine.dispose()


def _alembic(url, command_name: str, target: str) -> None:
    from alembic import command

    with alembic_pointed_at(url) as cfg:
        getattr(command, command_name)(cfg, target)


def _links(url) -> int:
    from app.models.report_data_source_association import report_data_source_association

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(report_data_source_association)).scalar()
    finally:
        engine.dispose()


@pytest.mark.e2e
def test_the_index_matches_the_model():
    from app.models.report_data_source_association import report_data_source_association

    found = _indexes(sync_test_url())
    assert INDEX in found, sorted(found)
    assert found[INDEX]["column_names"] == ["report_id", "data_source_id"]
    assert not found[INDEX]["unique"]
    # Declared on the table too, so autogenerate sees nothing to add or drop.
    declared = {i.name: i for i in report_data_source_association.indexes}
    assert INDEX in declared, sorted(declared)
    assert [c.name for c in declared[INDEX].columns] == ["report_id", "data_source_id"]
    assert not declared[INDEX].unique


def _below_dashlist02(revisions: tuple[str, ...]) -> bool:
    """Back at dashlist01. Sibling heads merged in beside dashlist02 (for
    example a release's merge revision) stay applied, so they may be listed too."""
    return PREVIOUS in revisions and "dashlist02" not in revisions


@pytest.mark.e2e
def test_round_trip_keeps_every_link(corpus, corpus_rows):
    url = sync_test_url()
    head = script_directory().get_current_head()
    _alembic(url, "upgrade", "head")  # the corpus stops before the data migration
    links = _links(url)
    assert links > 0, "the corpus has conversations with agents"
    _alembic(url, "downgrade", PREVIOUS)
    assert _below_dashlist02(current_revisions(url))
    assert INDEX not in _indexes(url)
    # A native DROP INDEX: on SQLite a batch rebuild could lose rows.
    assert _links(url) == links
    _alembic(url, "upgrade", "head")
    assert current_revisions(url) == (head,)
    assert INDEX in _indexes(url)
    assert _links(url) == links


@pytest.mark.e2e
def test_upgrade_and_downgrade_are_idempotent():
    # Lesson 21: each step checks first (the corpus harness runs them
    # unstamped, on databases that may or may not have the index).
    url = sync_test_url()
    _run_module(url, "upgrade", "upgrade")
    assert INDEX in _indexes(url)
    _alembic(url, "downgrade", PREVIOUS)
    _run_module(url, "downgrade", "downgrade")
    assert _below_dashlist02(current_revisions(url))
    assert INDEX not in _indexes(url)
    _alembic(url, "upgrade", "head")
    assert INDEX in _indexes(url)
