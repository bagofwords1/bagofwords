"""
Feedback-loop validation for the monitoring user filter + date range work
(branch: claude/monitoring-date-range-user-filter).

Mirrors the "Loop A" style from sandbox-feedback-loop.md: seed directly via the
async session, call ConsoleService, assert. No HTTP / live services needed.

Scenario:
  - One org, two users (Alice, Bob).
  - Alice: 2 agent executions, both recent (within the last week).
  - Bob:   2 agent executions, one recent + one 60 days old.

Validates:
  1. get_agent_execution_users returns the distinct executing users with their
     execution counts (powers the new MonitoringUserFilter dropdown).
  2. get_agent_execution_summaries(user_id=...) scopes the table to that user.
  3. user_id + a custom date range narrows further (the old execution drops out).
  4. get_diagnosis_dashboard_metrics(user_id=...) totals respect the user filter.
  5. get_diagnosis_timeseries(user_id=...) buckets respect the user filter.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      .venv/bin/python -m pytest tests/e2e/test_monitoring_user_filter_repro.py -v -s
"""
import uuid
import asyncio
from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.completion import Completion
from app.models.agent_execution import AgentExecution
from app.services.console_service import ConsoleService
from app.schemas.console_schema import MetricsQueryParams


def _run(coro):
    return asyncio.run(coro)


async def _seed():
    """Seed an org with two users and a mix of recent/old agent executions.

    Returns (organization, alice_id, bob_id).
    """
    suffix = uuid.uuid4().hex[:8]
    now = datetime.utcnow()
    recent = now - timedelta(days=5)
    old = now - timedelta(days=60)

    async with async_session_maker() as db:
        org = Organization(name=f"Monitoring Org {suffix}")
        db.add(org)
        await db.flush()

        alice = User(
            name="Alice", email=f"alice-{suffix}@example.com",
            hashed_password="x", is_active=True, is_superuser=False, is_verified=True,
        )
        bob = User(
            name="Bob", email=f"bob-{suffix}@example.com",
            hashed_password="x", is_active=True, is_superuser=False, is_verified=True,
        )
        db.add_all([alice, bob])
        await db.flush()

        async def make_execution(user, when):
            rep = Report(
                title="Report", slug=f"r-{uuid.uuid4().hex[:8]}",
                user_id=user.id, organization_id=org.id,
            )
            db.add(rep)
            await db.flush()
            comp = Completion(
                prompt="hello", completion="world", role="system",
                report_id=rep.id, user_id=user.id,
            )
            db.add(comp)
            await db.flush()
            ae = AgentExecution(
                completion_id=comp.id, organization_id=org.id, user_id=user.id,
                report_id=rep.id, status="success", created_at=when,
            )
            db.add(ae)
            await db.flush()
            return ae

        # Alice: 2 recent
        await make_execution(alice, recent)
        await make_execution(alice, recent)
        # Bob: 1 recent + 1 old
        await make_execution(bob, recent)
        await make_execution(bob, old)

        await db.commit()
        return org, str(alice.id), str(bob.id)


async def _call(method_name, org, **params_kwargs):
    service = ConsoleService()
    params = MetricsQueryParams(**params_kwargs)
    async with async_session_maker() as db:
        method = getattr(service, method_name)
        return await method(db, org, params)


@pytest.mark.e2e
def test_monitoring_users_endpoint_lists_executing_users():
    org, alice_id, bob_id = _run(_seed())

    resp = _run(_call("get_agent_execution_users", org))
    by_id = {u.id: u for u in resp.items}

    assert alice_id in by_id and bob_id in by_id, "both executing users should appear"
    assert by_id[alice_id].execution_count == 2
    assert by_id[bob_id].execution_count == 2
    assert by_id[alice_id].name == "Alice"
    print(f"[users] {[(u.name, u.execution_count) for u in resp.items]}")


@pytest.mark.e2e
def test_summaries_filtered_by_user():
    org, alice_id, bob_id = _run(_seed())

    wide_start = datetime.utcnow() - timedelta(days=365)

    # No user filter -> all 4 executions
    all_resp = _run(_call("get_agent_execution_summaries", org, start_date=wide_start))
    assert all_resp.total_items == 4, all_resp.total_items

    # Filter to Alice -> her 2 executions only
    alice_resp = _run(_call("get_agent_execution_summaries", org, start_date=wide_start, user_id=alice_id))
    assert alice_resp.total_items == 2
    assert all(item.user_name == "Alice" for item in alice_resp.items)

    # Filter to Bob -> his 2 executions
    bob_resp = _run(_call("get_agent_execution_summaries", org, start_date=wide_start, user_id=bob_id))
    assert bob_resp.total_items == 2

    print(f"[summaries] all={all_resp.total_items} alice={alice_resp.total_items} bob={bob_resp.total_items}")


@pytest.mark.e2e
def test_user_filter_plus_custom_date_range():
    org, alice_id, bob_id = _run(_seed())

    # Bob has 1 recent + 1 old. A last-30-days window should drop the old one.
    start = datetime.utcnow() - timedelta(days=30)
    end = datetime.utcnow()

    bob_recent = _run(_call(
        "get_agent_execution_summaries", org,
        start_date=start, end_date=end, user_id=bob_id,
    ))
    assert bob_recent.total_items == 1, bob_recent.total_items

    metrics = _run(_call(
        "get_diagnosis_dashboard_metrics", org,
        start_date=start, end_date=end, user_id=bob_id,
    ))
    assert metrics["total_items"] == 1, metrics

    print(f"[range] bob last-30d summaries={bob_recent.total_items} metrics_total={metrics['total_items']}")


@pytest.mark.e2e
def test_dashboard_and_timeseries_respect_user_filter():
    org, alice_id, bob_id = _run(_seed())
    wide_start = datetime.utcnow() - timedelta(days=365)

    alice_metrics = _run(_call("get_diagnosis_dashboard_metrics", org, start_date=wide_start, user_id=alice_id))
    assert alice_metrics["total_items"] == 2, alice_metrics

    ts = _run(_call("get_diagnosis_timeseries", org, start_date=wide_start, user_id=bob_id))
    total_points = sum(p.success + p.error for p in ts.points)
    assert total_points == 2, total_points

    print(f"[dashboard] alice_total={alice_metrics['total_items']} | [timeseries] bob_total={total_points}")
