"""dashfc04: one publish at a time per dashboard; step references indexed (phase 5).

A partial unique index holds at most one release in 'publishing' per
dashboard; releases in any other status are not limited. The four columns
that referenced steps.id without an index get one (publishing deletes
steps). The downgrade drops the indexes and changes no data.
"""
import pytest
from sqlalchemy import create_engine, inspect

from tests.fixtures.dashboard_corpus.alembic_at import alembic_pointed_at, current_revisions, script_directory
from tests.fixtures.dashboard_corpus.rows import sync_test_url

INDEX = "uq_artifact_releases_one_publishing"
STEP_REFERENCES = {"completions": "step_id", "tool_executions": "created_step_id",
                   "entities": "source_step_id", "table_feedback_events": "step_id"}


def _index(url):
    engine = create_engine(url)
    try:
        return {i["name"]: i for i in inspect(engine).get_indexes("artifact_releases")}.get(INDEX)
    finally:
        engine.dispose()


def _step_reference_indexes(url) -> dict:
    """table -> whether some index starts with its steps.id column."""
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        return {table: any(i["column_names"][:1] == [column] for i in insp.get_indexes(table))
                for table, column in STEP_REFERENCES.items()}
    finally:
        engine.dispose()


@pytest.mark.e2e
def test_the_index_is_unique_on_the_dashboard():
    index = _index(sync_test_url())
    assert index is not None and index["unique"] and index["column_names"] == ["artifact_id"]


@pytest.mark.e2e
def test_every_column_that_references_a_step_is_indexed():
    assert _step_reference_indexes(sync_test_url()) == {t: True for t in STEP_REFERENCES}


@pytest.mark.e2e
def test_round_trip(corpus):
    from alembic import command

    url = sync_test_url()
    head = script_directory().get_current_head()
    with alembic_pointed_at(url) as cfg:
        command.downgrade(cfg, "dashfc03")
    assert current_revisions(url) == ("dashfc03",)
    assert _index(url) is None
    assert _step_reference_indexes(url) == {t: False for t in STEP_REFERENCES}
    with alembic_pointed_at(url) as cfg:
        command.upgrade(cfg, "head")
    assert current_revisions(url) == (head,)
    assert _index(url) is not None
    assert _step_reference_indexes(url) == {t: True for t in STEP_REFERENCES}
