"""dashfc05: how the last refresh went (phase 6).

`artifacts.last_run_status` and `last_run_error` describe the last refresh
attempt of the published release; both are optional. The downgrade drops
them.
"""
import pytest
from sqlalchemy import create_engine, inspect

from tests.fixtures.dashboard_corpus.alembic_at import alembic_pointed_at, current_revisions, script_directory
from tests.fixtures.dashboard_corpus.rows import sync_test_url


def _columns(url) -> dict:
    engine = create_engine(url)
    try:
        return {c["name"]: c for c in inspect(engine).get_columns("artifacts")}
    finally:
        engine.dispose()


@pytest.mark.e2e
def test_the_columns_are_optional():
    from app.models.artifact import Artifact

    columns = _columns(sync_test_url())
    assert columns["last_run_status"]["nullable"] is True and columns["last_run_error"]["nullable"] is True
    assert Artifact.__table__.c.last_run_status.nullable and Artifact.__table__.c.last_run_error.nullable


@pytest.mark.e2e
def test_round_trip(corpus):
    from alembic import command

    url = sync_test_url()
    head = script_directory().get_current_head()
    with alembic_pointed_at(url) as cfg:
        command.downgrade(cfg, "dashfc04")
    assert current_revisions(url) == ("dashfc04",)
    assert "last_run_status" not in _columns(url) and "last_run_error" not in _columns(url)
    with alembic_pointed_at(url) as cfg:
        command.upgrade(cfg, "head")
    assert current_revisions(url) == (head,)
    assert {"last_run_status", "last_run_error"} <= set(_columns(url))
