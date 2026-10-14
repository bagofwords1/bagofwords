"""dashfc03: where an old /r/{conversation} link leads (phase 4).

`artifacts.legacy_report_id` names the conversation whose old link opens this
dashboard. One dashboard per conversation (unique), and no foreign key to
`reports`: the value must outlive the conversation it names. The data
migration (phase 9) fills it; until then it is empty. The downgrade refuses
-- with a message, never by deleting -- while a value is set.
"""
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.fixtures.dashboard_corpus.alembic_at import alembic_pointed_at, current_revisions, script_directory
from tests.fixtures.dashboard_corpus.rows import sync_test_url


def _inspect(url):
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        columns = {c["name"]: c for c in insp.get_columns("artifacts")}
        unique = {tuple(i["column_names"]) for i in insp.get_indexes("artifacts") if i.get("unique")}
        unique |= {tuple(u["column_names"]) for u in insp.get_unique_constraints("artifacts")}
        fks = [fk for fk in insp.get_foreign_keys("artifacts") if fk["constrained_columns"] == ["legacy_report_id"]]
        return columns, unique, fks
    finally:
        engine.dispose()


@pytest.mark.e2e
def test_the_column_is_unique_nullable_and_independent_of_reports():
    from app.models.artifact import Artifact

    columns, unique, fks = _inspect(sync_test_url())
    assert columns["legacy_report_id"]["nullable"] is True
    assert ("legacy_report_id",) in unique
    assert fks == []
    col = Artifact.__table__.c.legacy_report_id
    assert col.nullable is True and not col.foreign_keys


@pytest.mark.e2e
def test_two_dashboards_cannot_hold_the_same_conversation(corpus, corpus_rows):
    first, second = corpus.ids["conv_three_dash/dash_sales"], corpus.ids["conv_three_dash/dash_stock"]
    report = corpus.reports["conv_three_dash"]
    corpus_rows.update("artifacts", first, legacy_report_id=report)
    with pytest.raises(IntegrityError):
        corpus_rows.update("artifacts", second, legacy_report_id=report)


@pytest.mark.e2e
def test_round_trip_and_refusal(corpus, corpus_rows):
    from alembic import command

    url = sync_test_url()
    head = script_directory().get_current_head()
    with alembic_pointed_at(url) as cfg:
        command.downgrade(cfg, "dashfc02")
    assert current_revisions(url) == ("dashfc02",)
    assert "legacy_report_id" not in _inspect(url)[0]
    with alembic_pointed_at(url) as cfg:
        command.upgrade(cfg, "head")
    assert current_revisions(url) == (head,)

    aid = corpus.ids["conv_one_dash/dash"]
    corpus_rows.update("artifacts", aid, legacy_report_id=corpus.reports["conv_one_dash"])
    with alembic_pointed_at(url) as cfg, pytest.raises(RuntimeError, match="dashfc03 downgrade refused"):
        command.downgrade(cfg, "dashfc02")
    assert current_revisions(url) == (head,)
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            kept = conn.execute(text("SELECT legacy_report_id FROM artifacts WHERE id = :a"), {"a": aid}).scalar()
    finally:
        engine.dispose()
    assert kept == corpus.reports["conv_one_dash"]
