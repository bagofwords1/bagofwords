"""State-machine tests for the agent-reliability automation loop.

These exercise ``AgentReliabilityService.run_automation`` deterministically by
subclassing it and stubbing the two heavy, LLM-dependent seams (``_evaluate``
and ``_train_iteration``). That keeps the orchestration logic — gating,
baseline, the train/re-eval loop, the regression guard, promotion, and the
on-repeated-failure outcome — under test without a live model or agent run.

Run:
    BOW_DATABASE_URL="sqlite:///db/app.db" \
      /tmp/venv312/bin/python -m pytest tests/e2e/test_agent_reliability_loop.py -v
"""
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.membership import Membership
from app.models.data_source import DataSource
from app.models.eval import TestSuite, TestCase, TEST_CASE_STATUS_ACTIVE
from app.models.agent_automation_run import (
    TRIGGER_TABLE_CHANGE,
    TRIGGER_MANUAL,
    STATUS_PASSED,
    STATUS_GAVE_UP,
    STATUS_NO_EVALS,
    STATUS_SKIPPED,
    STATUS_PASSED_PENDING,
)
from app.services.agent_reliability_service import AgentReliabilityService


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------

class _StubReliability(AgentReliabilityService):
    """Deterministic orchestrator: scripts eval outcomes and training effect.

    ``eval_script`` is a list of (passing_case_ids, failing_case_ids) tuples
    consumed one per ``_evaluate`` call (baseline first, then each re-eval).
    ``promoted`` collects build ids that were submitted/approved/promoted.
    """

    def __init__(self, eval_script):
        super().__init__()
        self._eval_script = list(eval_script)
        self._eval_calls = 0
        self.train_calls = 0
        self.promoted_builds = []

    async def _evaluate(self, db, organization, actor, case_ids, *, build_id, trigger):
        passing, failing = self._eval_script[min(self._eval_calls, len(self._eval_script) - 1)]
        self._eval_calls += 1
        return {
            "run_id": str(uuid.uuid4()),
            "passed": len(passing),
            "failed": len(failing),
            "errored": 0,
            "passing_case_ids": list(passing),
            "failing_case_ids": list(failing),
            "summary": {"total": len(passing) + len(failing), "passed": len(passing), "failed": len(failing)},
        }

    async def _train_iteration(self, db, organization, actor, data_source, *, failing_case_ids, build_id, trigger, iteration, brief=None):
        self.train_calls += 1
        self.last_brief = brief
        # Don't touch the real BuildService; just return a synthetic build id.
        return {"build_id": build_id or f"build-{uuid.uuid4().hex[:8]}", "summary": {"instructions_added": 1}}

    async def _promote_or_pend(self, db, run, organization, actor, policy, *, build_id, iterations, test_run_ids, detail):
        # Record the promotion intent but skip real BuildService calls.
        from app.schemas.agent_automation_schema import AUTONOMY_AUTO
        if build_id and policy.stage("approve_instructions") == AUTONOMY_AUTO:
            self.promoted_builds.append(build_id)
            await self._set_reliability_status(db, run.data_source_id, "ok")
            return await self._finish(
                db, run, STATUS_PASSED, iterations=iterations, build_id=build_id,
                test_run_ids=test_run_ids, detail={**detail, "reason": "auto-promoted (stub)"},
            )
        return await self._finish(
            db, run, STATUS_PASSED_PENDING, iterations=iterations, build_id=build_id,
            test_run_ids=test_run_ids, detail={**detail, "reason": "pending (stub)"},
        )


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------

async def _seed(automation_settings, n_cases=2):
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Rel Org {suffix}")
        db.add(org)
        await db.flush()

        admin = User(
            name="Admin", email=f"admin-{suffix}@example.com",
            hashed_password="x", is_active=True, is_superuser=False, is_verified=True,
        )
        db.add(admin)
        await db.flush()
        db.add(Membership(user_id=admin.id, organization_id=org.id, role="admin"))

        ds = DataSource(
            name=f"Agent {suffix}", organization_id=org.id, is_active=True,
            owner_user_id=admin.id, automation_settings=automation_settings,
        )
        db.add(ds)
        await db.flush()

        suite = TestSuite(organization_id=org.id, name="Suite")
        db.add(suite)
        await db.flush()

        case_ids = []
        for i in range(n_cases):
            c = TestCase(
                suite_id=suite.id, name=f"case-{i}",
                prompt_json={"content": "q"}, expectations_json={"rules": []},
                data_source_ids_json=[str(ds.id)], status=TEST_CASE_STATUS_ACTIVE,
            )
            db.add(c)
            await db.flush()
            case_ids.append(str(c.id))

        await db.commit()
        return str(org.id), str(ds.id), case_ids


async def _run(stub, org_id, ds_id, trigger=TRIGGER_TABLE_CHANGE):
    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        ds = await db.get(DataSource, ds_id)
        run = await stub.run_automation(db, org, ds, trigger)
        await db.refresh(ds)
        return run, ds


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_baseline_green_no_training():
    """All evals pass at baseline → PASSED, no training."""
    settings = {"enabled": True, "eval_on_table_change": "auto", "train_on_failure": "auto"}
    org_id, ds_id, cases = await _seed(settings)
    stub = _StubReliability(eval_script=[(cases, [])])  # all pass
    run, ds = await _run(stub, org_id, ds_id)
    assert run.status == STATUS_PASSED
    assert stub.train_calls == 0
    assert ds.reliability_status == "ok"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_trigger_off_is_skipped():
    """Master switch off → SKIPPED, nothing runs."""
    org_id, ds_id, cases = await _seed({"enabled": False})
    stub = _StubReliability(eval_script=[(cases, [])])
    run, _ = await _run(stub, org_id, ds_id)
    assert run.status == STATUS_SKIPPED
    assert stub._eval_calls == 0


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_no_evals_is_noop():
    """Agent has no evals → NO_EVALS."""
    settings = {"enabled": True, "eval_on_table_change": "auto"}
    org_id, ds_id, _ = await _seed(settings, n_cases=0)
    stub = _StubReliability(eval_script=[([], [])])
    run, _ = await _run(stub, org_id, ds_id)
    assert run.status == STATUS_NO_EVALS


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_train_fixes_and_autopromotes():
    """Baseline fails, training fixes it on iteration 1, auto-approve on →
    PASSED with a promoted build."""
    settings = {
        "enabled": True, "eval_on_table_change": "auto",
        "train_on_failure": "auto", "approve_instructions": "auto",
        "max_iterations": 3,
    }
    org_id, ds_id, cases = await _seed(settings)
    # baseline: case[1] fails; after training: all pass
    stub = _StubReliability(eval_script=[([cases[0]], [cases[1]]), (cases, [])])
    run, ds = await _run(stub, org_id, ds_id)
    assert run.status == STATUS_PASSED
    assert stub.train_calls == 1
    assert len(stub.promoted_builds) == 1
    assert ds.reliability_status == "ok"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_train_fixes_but_suggest_only_pends():
    """Fix works, but approve_instructions=suggest → PASSED_PENDING (awaits
    human), no promotion."""
    settings = {
        "enabled": True, "eval_on_table_change": "auto",
        "train_on_failure": "auto", "approve_instructions": "suggest",
    }
    org_id, ds_id, cases = await _seed(settings)
    stub = _StubReliability(eval_script=[([cases[0]], [cases[1]]), (cases, [])])
    run, _ = await _run(stub, org_id, ds_id)
    assert run.status == STATUS_PASSED_PENDING
    assert stub.promoted_builds == []


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_regression_guard_blocks_promotion():
    """Training makes the failing case pass but regresses a previously-passing
    case → not accepted; loop exhausts and gives up."""
    settings = {
        "enabled": True, "eval_on_table_change": "auto",
        "train_on_failure": "auto", "approve_instructions": "auto",
        "max_iterations": 2, "on_repeated_failure": "training",
    }
    org_id, ds_id, cases = await _seed(settings, n_cases=2)
    # baseline: case0 pass, case1 fail.
    # every retrain: case1 passes but case0 regresses (fails) → never clean.
    stub = _StubReliability(eval_script=[
        ([cases[0]], [cases[1]]),   # baseline
        ([cases[1]], [cases[0]]),   # iter1: regressed case0
        ([cases[1]], [cases[0]]),   # iter2: still regressed
    ])
    run, ds = await _run(stub, org_id, ds_id)
    assert run.status == STATUS_GAVE_UP
    assert ds.reliability_status == "training"
    assert stub.promoted_builds == []


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_gives_up_and_moves_to_development():
    """Training never fixes the failure; on_repeated_failure=development → agent
    is pulled from regular users (reliability_status=development) while
    publish_status is left untouched (admins keep access)."""
    settings = {
        "enabled": True, "eval_on_table_change": "auto",
        "train_on_failure": "auto", "max_iterations": 2,
        "on_repeated_failure": "development",
    }
    org_id, ds_id, cases = await _seed(settings)
    stub = _StubReliability(eval_script=[([cases[0]], [cases[1]])])  # always fails
    run, ds = await _run(stub, org_id, ds_id)
    assert run.status == STATUS_GAVE_UP
    assert ds.publish_status != "disabled"  # publish_status stays human-owned
    assert ds.reliability_status == "development"
    assert stub.train_calls == 2  # used both iterations


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_train_off_reports_only():
    """Evals fail but train_on_failure=off → GAVE_UP (report-only), no training,
    outcome still applied per policy."""
    settings = {
        "enabled": True, "eval_on_table_change": "auto",
        "train_on_failure": "off", "on_repeated_failure": "training",
    }
    org_id, ds_id, cases = await _seed(settings)
    stub = _StubReliability(eval_script=[([cases[0]], [cases[1]])])
    run, ds = await _run(stub, org_id, ds_id)
    assert run.status == STATUS_GAVE_UP
    assert stub.train_calls == 0
    assert ds.reliability_status == "training"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_manual_trigger_runs_even_when_table_change_off():
    """A manual trigger ignores per-stage trigger autonomy (the human asked)."""
    settings = {"enabled": True, "eval_on_table_change": "off", "train_on_failure": "auto"}
    org_id, ds_id, cases = await _seed(settings)
    stub = _StubReliability(eval_script=[(cases, [])])
    run, _ = await _run(stub, org_id, ds_id, trigger=TRIGGER_MANUAL)
    assert run.status == STATUS_PASSED
