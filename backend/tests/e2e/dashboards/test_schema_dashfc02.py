"""dashfc02: a query no longer needs a widget (phase 2, part B).

A published dashboard's copies have no conversation, and a widget always
belongs to one, so `queries.widget_id` becomes nullable (`steps.widget_id`
already is in the database; the model now says so too). The downgrade
restores NOT NULL and refuses, before any DDL, while a query without a widget
exists.
"""
import pytest
from sqlalchemy import create_engine, inspect, text

from tests.fixtures.dashboard_corpus.alembic_at import alembic_pointed_at, current_revisions, script_directory
from tests.fixtures.dashboard_corpus.rows import sync_test_url


def _nullable(url, table, column):
    engine = create_engine(url)
    try:
        return {c["name"]: c["nullable"] for c in inspect(engine).get_columns(table)}[column]
    finally:
        engine.dispose()


def _count(url, table):
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
    finally:
        engine.dispose()


@pytest.mark.e2e
def test_widgets_are_optional_in_the_database_and_the_models():
    from app.models.query import Query
    from app.models.step import Step

    url = sync_test_url()
    assert _nullable(url, "queries", "widget_id") is True
    assert _nullable(url, "steps", "widget_id") is True
    assert Query.__table__.c.widget_id.nullable is True
    assert Step.__table__.c.widget_id.nullable is True


@pytest.mark.e2e
def test_round_trip_restores_required_widgets_and_keeps_every_row(corpus):
    from alembic import command

    url = sync_test_url()
    head = script_directory().get_current_head()
    queries, steps = _count(url, "queries"), _count(url, "steps")
    with alembic_pointed_at(url) as cfg:
        command.downgrade(cfg, "dashfc01")
    assert current_revisions(url) == ("dashfc01",)
    assert _nullable(url, "queries", "widget_id") is False
    with alembic_pointed_at(url) as cfg:
        command.upgrade(cfg, "head")
    assert current_revisions(url) == (head,)
    assert _nullable(url, "queries", "widget_id") is True
    assert (_count(url, "queries"), _count(url, "steps")) == (queries, steps)


@pytest.mark.e2e
def test_downgrade_refuses_while_a_query_has_no_widget(corpus, corpus_rows):
    from alembic import command

    url = sync_test_url()
    release = corpus_rows.insert("artifact_releases", artifact_id=corpus.ids["conv_one_dash/dash"],
                                 organization_id=corpus.cast.org_id, content={})
    corpus_rows.insert("queries", title="copy", report_id=None, widget_id=None, artifact_release_id=release,
                       organization_id=corpus.cast.org_id)
    head = script_directory().get_current_head()
    with alembic_pointed_at(url) as cfg, pytest.raises(RuntimeError, match="dashfc02 downgrade refused"):
        command.downgrade(cfg, "dashfc01")
    assert current_revisions(url) == (head,)
    assert _nullable(url, "queries", "widget_id") is True
