"""dashfc07: a fork's source dashboard (phase 8).

`reports.forked_from_artifact_id` names the dashboard a fork from /d was
made from (the conversation's own `forked_from_id` stays empty: it would
name the owner's conversation). Optional; the downgrade drops it.
"""
import pytest
from sqlalchemy import create_engine, inspect

from tests.fixtures.dashboard_corpus.alembic_at import alembic_pointed_at, current_revisions, script_directory
from tests.fixtures.dashboard_corpus.rows import sync_test_url


def _columns(url) -> dict:
    engine = create_engine(url)
    try:
        return {c["name"]: c for c in inspect(engine).get_columns("reports")}
    finally:
        engine.dispose()


@pytest.mark.e2e
def test_the_column_is_optional():
    from app.models.report import Report

    assert _columns(sync_test_url())["forked_from_artifact_id"]["nullable"] is True
    assert Report.__table__.c.forked_from_artifact_id.nullable


@pytest.mark.e2e
def test_round_trip(corpus):
    from alembic import command

    url = sync_test_url()
    head = script_directory().get_current_head()
    with alembic_pointed_at(url) as cfg:
        command.downgrade(cfg, "dashfc06")
    assert current_revisions(url) == ("dashfc06",)
    assert "forked_from_artifact_id" not in _columns(url)
    with alembic_pointed_at(url) as cfg:
        command.upgrade(cfg, "head")
    assert current_revisions(url) == (head,)
    assert "forked_from_artifact_id" in _columns(url)
