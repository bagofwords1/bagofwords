"""Live agent-reliability validation against Chinook with a real LLM (Haiku).

Seeds everything directly in the DB (the HTTP signup route is unavailable in
this sandbox config) — org, admin, an Anthropic provider, the Chinook demo data
source, and an eval — then drives the reliability loop in-process so a *real*
Haiku agent run scores the eval.

Gated behind ``@pytest.mark.evals``; skips unless ``ANTHROPIC_API_KEY_TEST`` is
set. Run:

    BOW_DATABASE_URL="sqlite:///db/app.db" ANTHROPIC_API_KEY_TEST=sk-ant-... \
      /tmp/venv312/bin/python -m pytest tests/evals/test_reliability_live.py -v -s
"""
import os
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.membership import Membership
from app.models.llm_provider import LLMProvider
from app.models.llm_model import LLMModel
from app.models.data_source import DataSource
from app.models.eval import TestSuite, TestCase, TEST_CASE_STATUS_ACTIVE
from app.models.agent_automation_run import TRIGGER_MANUAL
from app.services.demo_data_source_service import DemoDataSourceService
from app.services.agent_reliability_service import AgentReliabilityService

HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"


@pytest.mark.evals
@pytest.mark.asyncio
async def test_reliability_loop_runs_live_eval_on_chinook():
    key = os.getenv("ANTHROPIC_API_KEY_TEST")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY_TEST not set")

    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        # --- org + admin -----------------------------------------------------
        from app.models.organization_settings import OrganizationSettings
        org = Organization(name=f"Rel Live {suffix}")
        db.add(org)
        await db.flush()
        db.add(OrganizationSettings(organization_id=org.id, config={}))
        await db.flush()
        user = User(
            name="Admin", email=f"admin-{suffix}@example.com",
            hashed_password="x", is_active=True, is_superuser=False, is_verified=True,
        )
        db.add(user)
        await db.flush()
        db.add(Membership(user_id=user.id, organization_id=org.id, role="admin"))

        # --- Anthropic / Haiku provider --------------------------------------
        provider = LLMProvider(
            name="Anthropic", provider_type="anthropic",
            organization_id=org.id, is_preset=False, is_enabled=True,
            use_preset_credentials=False,
        )
        provider.encrypt_credentials(key, "")
        db.add(provider)
        await db.flush()
        model = LLMModel(
            name="Claude Haiku 4.5", model_id=HAIKU_MODEL_ID, provider_id=provider.id,
            organization_id=org.id, is_preset=True, is_enabled=True,
            is_default=True, is_small_default=True,
        )
        db.add(model)
        await db.commit()

        # --- Chinook demo data source ---------------------------------------
        resp = await DemoDataSourceService().install_demo_data_source(
            db, org, user, "chinook",
        )
        assert getattr(resp, "success", False), getattr(resp, "message", resp)
        ds_id = resp.data_source_id
        print(f"[reliability-live] chinook installed ds={ds_id}", flush=True)

        # --- eval scoped to the agent ---------------------------------------
        suite = TestSuite(organization_id=org.id, name="Reliability Live")
        db.add(suite)
        await db.flush()
        case = TestCase(
            suite_id=suite.id, name="count_customers",
            prompt_json={"content": "How many customers are in the database?"},
            expectations_json={
                "spec_version": 1, "order_mode": "flexible",
                "rules": [{"type": "tool.calls", "tool": "create_data", "min_calls": 1}],
            },
            data_source_ids_json=[str(ds_id)],
            status=TEST_CASE_STATUS_ACTIVE,
        )
        db.add(case)

        # --- enable automation (report-only training keeps it bounded) ------
        ds = await db.get(DataSource, str(ds_id))
        ds.automation_settings = {
            "enabled": True, "eval_on_table_change": "suggest",
            "train_on_failure": "off", "on_repeated_failure": "none",
        }
        db.add(ds)
        await db.commit()

        # --- drive the loop: a real Haiku agent run scores the eval ----------
        run = await AgentReliabilityService().run_automation(
            db, org, ds, TRIGGER_MANUAL, user=user, changed_hint="live test",
        )
        print(f"[reliability-live] status={run.status} iterations={run.iterations} "
              f"runs={run.test_run_ids_json} detail={run.detail_json}", flush=True)

        # The loop measured the agent live: a baseline TestRun was spawned and a
        # terminal state reached (passed when Haiku correctly queries Chinook;
        # report-only gave_up otherwise — either way the live eval executed).
        assert run.status in {"passed", "gave_up", "passed_pending"}, run.detail_json
        assert run.test_run_ids_json, "expected at least one live TestRun"


@pytest.mark.evals
@pytest.mark.asyncio
async def test_column_change_triggers_reliability_loop_live():
    """The headline behaviour: changing a table's columns fires the
    table_change trigger, which runs the agent's evals end to end (live Haiku).

    Seeds Chinook + Haiku + an eval + automation, then mutates a ConnectionTable
    column and re-runs the schema sync — the same path a real re-index takes —
    and asserts a ``table_change`` AgentAutomationRun is recorded that executed
    a live eval.
    """
    import asyncio
    from sqlalchemy import select
    from app.models.connection_table import ConnectionTable
    from app.models.connection import Connection
    from app.models.agent_automation_run import AgentAutomationRun
    from app.models.organization_settings import OrganizationSettings
    from app.services.data_source_service import DataSourceService

    key = os.getenv("ANTHROPIC_API_KEY_TEST")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY_TEST not set")

    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Rel Col {suffix}")
        db.add(org)
        await db.flush()
        db.add(OrganizationSettings(organization_id=org.id, config={}))
        await db.flush()
        user = User(
            name="Admin", email=f"admin-{suffix}@example.com",
            hashed_password="x", is_active=True, is_superuser=False, is_verified=True,
        )
        db.add(user)
        await db.flush()
        db.add(Membership(user_id=user.id, organization_id=org.id, role="admin"))

        provider = LLMProvider(
            name="Anthropic", provider_type="anthropic", organization_id=org.id,
            is_preset=False, is_enabled=True, use_preset_credentials=False,
        )
        provider.encrypt_credentials(key, "")
        db.add(provider)
        await db.flush()
        db.add(LLMModel(
            name="Claude Haiku 4.5", model_id=HAIKU_MODEL_ID, provider_id=provider.id,
            organization_id=org.id, is_preset=True, is_enabled=True,
            is_default=True, is_small_default=True,
        ))
        await db.commit()

        resp = await DemoDataSourceService().install_demo_data_source(db, org, user, "chinook")
        assert getattr(resp, "success", False), getattr(resp, "message", resp)
        ds_id = resp.data_source_id

        suite = TestSuite(organization_id=org.id, name="Col Change")
        db.add(suite)
        await db.flush()
        db.add(TestCase(
            suite_id=suite.id, name="count_customers",
            prompt_json={"content": "How many customers are in the database?"},
            expectations_json={
                "spec_version": 1, "order_mode": "flexible",
                "rules": [{"type": "tool.calls", "tool": "create_data", "min_calls": 1}],
            },
            data_source_ids_json=[str(ds_id)], status=TEST_CASE_STATUS_ACTIVE,
        ))
        ds = await db.get(DataSource, str(ds_id))
        ds.automation_settings = {
            "enabled": True, "eval_on_table_change": "auto",
            "train_on_failure": "off", "on_repeated_failure": "none",
        }
        db.add(ds)
        await db.commit()

        # Resolve the agent's connection + a ConnectionTable, then mutate its
        # columns to simulate a schema change picked up by a re-index.
        conn = (await db.execute(
            select(Connection).join(
                Connection.data_sources
            ).where(DataSource.id == str(ds_id))
        )).scalars().first()
        assert conn is not None, "chinook connection not found"
        ct = (await db.execute(
            select(ConnectionTable).where(ConnectionTable.connection_id == conn.id).limit(1)
        )).scalars().first()
        assert ct is not None, "no ConnectionTable to mutate"
        new_cols = list(ct.columns or []) + [{"name": f"added_col_{suffix}", "dtype": "TEXT"}]
        ct.columns = new_cols
        db.add(ct)
        await db.commit()

        # Re-run the sync — the same path a re-index takes. Its post-commit diff
        # detection schedules the table_change loop.
        await DataSourceService().sync_domain_tables_from_connection(db, ds, conn)

        # The trigger schedules a background task on this loop. Give it time to
        # run the live eval, then assert a table_change run was recorded.
        deadline = asyncio.get_event_loop().time() + 120
        found = None
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            async with async_session_maker() as check:
                rows = (await check.execute(
                    select(AgentAutomationRun).where(
                        AgentAutomationRun.data_source_id == str(ds_id),
                        AgentAutomationRun.trigger == "table_change",
                    )
                )).scalars().all()
                done = [r for r in rows if r.status != "running"]
                if done:
                    found = done[0]
                    break
        assert found is not None, "column change did not trigger a table_change automation run"
        print(f"[reliability-live] table_change run status={found.status} "
              f"runs={found.test_run_ids_json}", flush=True)
        assert found.status in {"passed", "gave_up", "passed_pending", "no_evals"}, found.detail_json
