"""refhist01: a dashboard's refresh history (2026-10-25).

`dashboard_refresh_runs`: one row per refresh that ran, or a scheduled run
that could not. Additive: no existing row changes. The downgrade drops it.
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


def _indexes(url) -> set:
    engine = create_engine(url)
    try:
        return {tuple(i["column_names"]) for i in inspect(engine).get_indexes("dashboard_refresh_runs")}
    finally:
        engine.dispose()


@pytest.mark.e2e
def test_the_table_matches_the_model():
    from app.models.dashboard_refresh_run import DashboardRefreshRun

    url = sync_test_url()
    engine = create_engine(url)
    try:
        columns = {c["name"]: c for c in inspect(engine).get_columns("dashboard_refresh_runs")}
    finally:
        engine.dispose()
    assert set(columns) == {c.name for c in DashboardRefreshRun.__table__.columns}
    for name in ("artifact_id", "organization_id", "trigger", "status", "started_at"):
        assert columns[name]["nullable"] is False, name
    # A dashboard's history is read newest first.
    assert ("artifact_id", "started_at") in _indexes(url)


@pytest.mark.e2e
def test_round_trip(corpus):
    from alembic import command

    url = sync_test_url()
    head = script_directory().get_current_head()
    with alembic_pointed_at(url) as cfg:
        command.upgrade(cfg, "head")  # the corpus stops before the data migration
        command.downgrade(cfg, "collab01")
    assert current_revisions(url) == ("collab01",)
    assert "dashboard_refresh_runs" not in _tables(url)
    with alembic_pointed_at(url) as cfg:
        command.upgrade(cfg, "head")
    assert current_revisions(url) == (head,)
    assert "dashboard_refresh_runs" in _tables(url)
