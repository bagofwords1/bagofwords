"""dashfc06: a dashboard's old names (phase 8).

`artifact_slug_history` keeps the names a dashboard had: each unique in the
installation, leading to its dashboard until the owner removes the name
(`released_at`). The downgrade drops the table.
"""
import pytest
from sqlalchemy import create_engine, inspect

from tests.fixtures.dashboard_corpus.alembic_at import alembic_pointed_at, current_revisions, script_directory
from tests.fixtures.dashboard_corpus.rows import sync_test_url


def _tables(url) -> set:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _unique_slug(url) -> bool:
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        uniques = [set(u["column_names"]) for u in insp.get_unique_constraints("artifact_slug_history")]
        uniques += [set(i["column_names"]) for i in insp.get_indexes("artifact_slug_history") if i.get("unique")]
        return {"slug"} in uniques
    finally:
        engine.dispose()


@pytest.mark.e2e
def test_the_table_and_its_unique_name():
    url = sync_test_url()
    assert "artifact_slug_history" in _tables(url)
    assert _unique_slug(url)


@pytest.mark.e2e
def test_round_trip(corpus):
    from alembic import command

    url = sync_test_url()
    head = script_directory().get_current_head()
    with alembic_pointed_at(url) as cfg:
        command.downgrade(cfg, "dashfc05")
    assert current_revisions(url) == ("dashfc05",)
    assert "artifact_slug_history" not in _tables(url)
    with alembic_pointed_at(url) as cfg:
        command.upgrade(cfg, "head")
    assert current_revisions(url) == (head,)
    assert "artifact_slug_history" in _tables(url)
