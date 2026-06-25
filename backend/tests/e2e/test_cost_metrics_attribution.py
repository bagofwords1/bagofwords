"""Cost console — attribution & grouping repro/validation (Loop A).

Self-contained: seeds one org with two users (Alice in group G1, Bob in none),
two data sources, two reports, two LLM models, and three usage records — one
attributed to Alice/Report1/DS1, one to Bob/Report2/(DS1+DS2), and one
background record with no user/report (the "unattributed" case). Then it calls
ConsoleService.get_cost_metrics for each group_by and asserts the spend lands in
the right buckets, and that KPI totals never double-count when a record fans out
across multiple data sources / groups.

Run:
    cd backend
    export BOW_DATABASE_URL="sqlite:///db/app.db"
    TESTING=true pytest -s -m e2e --db=sqlite \
      tests/e2e/test_cost_metrics_attribution.py
"""
import uuid
import asyncio
from datetime import datetime

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.group import Group
from app.models.group_membership import GroupMembership
from app.models.data_source import DataSource
from app.models.report import Report
from app.models.report_data_source_association import report_data_source_association
from app.models.llm_provider import LLMProvider
from app.models.llm_model import LLMModel
from app.models.llm_usage_record import LLMUsageRecord
from app.schemas.console_schema import MetricsQueryParams
from app.services.console_service import ConsoleService


def _run(coro):
    return asyncio.run(coro)


async def _seed():
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Cost Org {suffix}")
        db.add(org)
        await db.flush()

        alice = User(name="Alice", email=f"alice-{suffix}@x.com", hashed_password="x",
                     is_active=True, is_superuser=False, is_verified=True)
        bob = User(name="Bob", email=f"bob-{suffix}@x.com", hashed_password="x",
                   is_active=True, is_superuser=False, is_verified=True)
        db.add_all([alice, bob])
        await db.flush()

        g1 = Group(organization_id=org.id, name=f"Analysts {suffix}")
        db.add(g1)
        await db.flush()
        db.add(GroupMembership(group_id=g1.id, user_id=alice.id))

        ds1 = DataSource(name=f"DS1 {suffix}", organization_id=org.id, is_active=True)
        ds2 = DataSource(name=f"DS2 {suffix}", organization_id=org.id, is_active=True)
        db.add_all([ds1, ds2])
        await db.flush()

        r1 = Report(title="R1", slug=f"r1-{suffix}", organization_id=org.id, user_id=alice.id)
        r2 = Report(title="R2", slug=f"r2-{suffix}", organization_id=org.id, user_id=bob.id)
        db.add_all([r1, r2])
        await db.flush()
        # r1 -> DS1; r2 -> DS1 + DS2 (multi-source: tests the fan-out split)
        await db.execute(report_data_source_association.insert().values([
            {"report_id": r1.id, "data_source_id": ds1.id},
            {"report_id": r2.id, "data_source_id": ds1.id},
            {"report_id": r2.id, "data_source_id": ds2.id},
        ]))

        prov_a = LLMProvider(organization_id=org.id, name="Anthropic", provider_type="anthropic")
        prov_o = LLMProvider(organization_id=org.id, name="OpenAI", provider_type="openai")
        db.add_all([prov_a, prov_o])
        await db.flush()

        m_claude = LLMModel(organization_id=org.id, provider_id=prov_a.id,
                            name="Claude 4.6 Sonnet", model_id="claude-sonnet-4-6")
        m_gpt = LLMModel(organization_id=org.id, provider_id=prov_o.id,
                         name="GPT-5.5", model_id="gpt-5.5")
        db.add_all([m_claude, m_gpt])
        await db.flush()

        now = datetime.utcnow()

        def rec(model, provider_type, *, user_id, report_id, scope, total, inp, out):
            return LLMUsageRecord(
                scope=scope, scope_ref_id=None,
                organization_id=org.id, user_id=user_id, report_id=report_id,
                llm_model_id=model.id, model_id=model.model_id, provider_type=provider_type,
                prompt_tokens=1000, completion_tokens=500,
                cache_read_tokens=0, cache_creation_tokens=0,
                input_cost_usd=inp, output_cost_usd=out, total_cost_usd=total,
                created_at=now,
            )

        # rec1: Alice / R1(DS1) / Claude  cost 0.0105
        # rec2: Bob   / R2(DS1+DS2) / GPT cost 0.02
        # rec3: background — no user, no report — Claude  cost 0.001
        db.add_all([
            rec(m_claude, "anthropic", user_id=alice.id, report_id=r1.id, scope="planner",
                total=0.0105, inp=0.003, out=0.0075),
            rec(m_gpt, "openai", user_id=bob.id, report_id=r2.id, scope="answer",
                total=0.02, inp=0.005, out=0.015),
            rec(m_claude, "anthropic", user_id=None, report_id=None, scope="data_source.summary",
                total=0.001, inp=0.001, out=0.0),
        ])
        await db.commit()

        return {
            "org": org, "alice_id": str(alice.id), "bob_id": str(bob.id),
            "g1_id": str(g1.id), "g1_name": g1.name,
            "ds1_id": str(ds1.id), "ds1_name": ds1.name,
            "ds2_id": str(ds2.id), "ds2_name": ds2.name,
            "claude_id": str(m_claude.id),
        }


def _by_key(items):
    return {i.key: i for i in items}


def _approx(a, b, tol=1e-6):
    return abs(a - b) < tol


@pytest.mark.e2e
def test_cost_metrics_grouping_and_attribution():
    seed = _run(_seed())
    org = seed["org"]
    svc = ConsoleService()
    params = MetricsQueryParams()

    async def _go():
        async with async_session_maker() as db:
            out = {}
            for gb in ("model", "provider", "user", "data_source", "group", "scope"):
                out[gb] = await svc.get_cost_metrics(db, org, params, group_by=gb)
            return out

    res = _run(_go())

    TOTAL = 0.0105 + 0.02 + 0.001  # 0.0315

    # --- KPI totals are exact and identical across every grouping (no double count) ---
    for gb, m in res.items():
        assert m.total_calls == 3, f"{gb}: expected 3 calls, got {m.total_calls}"
        assert _approx(m.total_cost_usd, TOTAL), f"{gb}: total {m.total_cost_usd} != {TOTAL}"
        # timeseries sums back to the headline total
        ts_cost = sum(p.cost_usd for p in m.timeseries)
        assert _approx(ts_cost, TOTAL), f"{gb}: timeseries {ts_cost} != {TOTAL}"

    # --- by model ---
    models = _by_key(res["model"].items)
    assert _approx(models[seed["claude_id"]].total_cost_usd, 0.0105 + 0.001)  # rec1 + rec3
    print("[model]", {k: round(v.total_cost_usd, 4) for k, v in models.items()})

    # --- by user: Alice=rec1, Bob=rec2, Unattributed=rec3 ---
    users = _by_key(res["user"].items)
    assert _approx(users[seed["alice_id"]].total_cost_usd, 0.0105)
    assert _approx(users[seed["bob_id"]].total_cost_usd, 0.02)
    assert _approx(users["__unattributed__"].total_cost_usd, 0.001)
    print("[user]", {u.label: round(u.total_cost_usd, 4) for u in res["user"].items})

    # --- by group: G1=Alice(rec1); Bob has no group + rec3 null user => Unattributed ---
    groups = _by_key(res["group"].items)
    assert _approx(groups[seed["g1_id"]].total_cost_usd, 0.0105)
    assert _approx(groups["__unattributed__"].total_cost_usd, 0.02 + 0.001)
    print("[group]", {g.label: round(g.total_cost_usd, 4) for g in res["group"].items})

    # --- by data_source: rec2 fans out to DS1 & DS2; rec3 (no report) => Unattributed ---
    ds = _by_key(res["data_source"].items)
    assert _approx(ds[seed["ds1_id"]].total_cost_usd, 0.0105 + 0.02)  # rec1(R1) + rec2(R2)
    assert _approx(ds[seed["ds2_id"]].total_cost_usd, 0.02)           # rec2(R2)
    assert _approx(ds["__unattributed__"].total_cost_usd, 0.001)      # rec3
    print("[data_source]", {d.label: round(d.total_cost_usd, 4) for d in res["data_source"].items})

    # --- by scope ---
    scopes = _by_key(res["scope"].items)
    assert _approx(scopes["planner"].total_cost_usd, 0.0105)
    assert _approx(scopes["answer"].total_cost_usd, 0.02)
    assert _approx(scopes["data_source.summary"].total_cost_usd, 0.001)
    print("[scope] OK; all groupings validated. TOTAL =", round(TOTAL, 4))
