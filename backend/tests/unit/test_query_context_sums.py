"""The ``<queries>`` section shows a ``column_sums`` block per query.

The section is rebuilt every iteration from each default step's persisted
summary: the stored ``info`` (all rows) plus at most 5 ``preview_rows``. For a
longer result those rows are incomplete, so a total row the section cannot see
is caught by the ``k × max`` check only. Hidden previews get no block.

Seam: ``QueryContextBuilder.build`` on the migration-backed test database. The
report, its owner and org and the query come from the API; only the step
holding the result and its ``default_step_id`` link are seeded directly (see
``seed_default_step``). The step's summary is written by the existing Step
write-through.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import select

from app.ai import data_preview
from app.ai.context.builders.query_context_builder import QueryContextBuilder
from app.ai.data_preview import clamp_stats, gate_stats_for_privacy
from app.dependencies import async_session_maker
from app.models.step import Step
from tests.unit.test_observation_sums import (
    EXPECTED_SUMS,
    _departments_df,
    api_query_creator,
    seed_default_step,
)

pytestmark = pytest.mark.db  # opens real sessions — see tests/unit/conftest.py

NOTE_12 = "sum = sum of all 12 rows"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def sums_report(test_client, create_report, create_user, login_user, whoami):
    """A report owned by a fresh user and org, created through the API."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Budget", user_token=token, org_id=org_id)
    return SimpleNamespace(
        report_id=report["id"],
        create_query=api_query_creator(test_client, token, org_id, report["id"]),
    )


def _seed(report, df):
    return seed_default_step(report, df)


def _section(scenario, *, include_data_preview=True):
    async def build():
        async with async_session_maker() as db:
            return await QueryContextBuilder(
                db,
                organization=None,
                report=SimpleNamespace(id=scenario.report_id),
            ).build(max_queries=5, include_data_preview=include_data_preview)

    return _run(build())


def _persisted_summary(step_id):
    async def load():
        async with async_session_maker() as db:
            return (
                await db.execute(select(Step.context_summary_json).where(Step.id == step_id))
            ).scalar_one()

    return _run(load())


def _with_unlabelled_total_row(df):
    """A 13th row with a NULL label holding each column's total (UNION ALL)."""
    total = {
        "department": None,
        "budget_eur": int(df["budget_eur"].sum()),
        "actual_eur": int(df["actual_eur"].sum()),
        "variance_pct": round(float(df["variance_pct"].sum()), 2),
        "dept_id": 99,
    }
    return pd.concat([df, pd.DataFrame([total])], ignore_index=True)


def test_queries_section_shows_sums_when_previews_visible(sums_report):
    scenario = _seed(sums_report, _departments_df())
    persisted = _persisted_summary(scenario.step_id)
    assert len(persisted["preview_rows"]) == 5  # 5 of 12 rows: incomplete

    section = _section(scenario)

    [item] = section.items
    assert item.row_count == 12
    assert item.column_sums == {"note": NOTE_12, "sum": EXPECTED_SUMS}
    rendered = section.render()
    assert "<column_sums>" in rendered
    assert NOTE_12 in rendered
    assert "&quot;budget_eur&quot;: 2140" in rendered  # xml_escape, as every tag


def test_queries_section_drops_sums_for_a_total_row_it_cannot_see(sums_report):
    df = _with_unlabelled_total_row(_departments_df())
    assert df["budget_eur"].sum() == 2 * df["budget_eur"].max()  # the k = 2 signature
    scenario = _seed(sums_report, df)

    section = _section(scenario)

    [item] = section.items
    assert item.row_count == 13
    assert item.column_sums is None
    assert "<column_sums>" not in section.render()
    # Control: test_queries_section_shows_sums_when_previews_visible (same
    # step without the total row) gets the block.


def test_queries_section_has_no_sums_when_previews_hidden(sums_report):
    scenario = _seed(sums_report, _departments_df())
    info = dict(_persisted_summary(scenario.step_id)["info"])

    hidden = _section(scenario, include_data_preview=False)

    [item] = hidden.items
    assert item.column_sums is None
    assert item.data_preview is None
    assert "<column_sums>" not in hidden.render()
    # The privacy stats are rendered exactly as before this change.
    assert item.stats == clamp_stats(gate_stats_for_privacy(info))

    # Control: the same step with previews visible shows the block.
    visible = _section(scenario)
    assert visible.items[0].column_sums == {"note": NOTE_12, "sum": EXPECTED_SUMS}
    assert "<column_sums>" in visible.render()


def test_single_row_query_has_no_sums(sums_report):
    scenario = _seed(sums_report, _departments_df().head(1))

    section = _section(scenario)

    [item] = section.items
    assert item.row_count == 1
    assert item.column_sums is None
    assert "<column_sums>" not in section.render()
    # Control: the 12-row step of test_queries_section_shows_sums_when_previews_visible.


def test_queries_section_survives_a_column_sums_failure(sums_report, monkeypatch, caplog):
    scenario = _seed(sums_report, _departments_df())
    monkeypatch.setattr(logging.getLogger("app.ai.data_preview"), "disabled", False)
    caplog.set_level(logging.INFO, logger="app.ai.data_preview")

    def boom(*args, **kwargs):
        raise ValueError("budget 2140")

    monkeypatch.setattr(data_preview, "column_sums", boom)

    section = _section(scenario)

    [item] = section.items
    assert item.row_count == 12
    assert item.data_preview is not None  # set before the sums call; kept
    assert item.column_sums is None
    assert "<column_sums>" not in section.render()
    records = [r for r in caplog.records if r.name == "app.ai.data_preview"]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.WARNING, f"column sums skipped: step={scenario.step_id} error=ValueError"),
    ]
    # Control: test_queries_section_shows_sums_when_previews_visible (same
    # step, real column_sums) gets the block.
