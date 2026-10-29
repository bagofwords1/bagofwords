"""create_demo_dataset: gating, review round-trip, and install.

Runs the tool through run_stream against the real permission resolver,
confirmation registry/table, ConnectionService (schema refresh of the written
SQLite file) and DataSourceService (agents). Only the LLM is stubbed — it
returns the generator code a model would write for each table.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true \
      uv run pytest tests/training/test_create_demo_dataset_tool.py -v
"""
import os
import uuid

import pytest
from sqlalchemy import select

from app.ai.tools.implementations.create_demo_dataset import CreateDemoDatasetTool
from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.organization_settings import OrganizationSettings
from app.models.report import Report
from app.models.role import Role
from app.models.role_assignment import RoleAssignment
from app.models.user import User

GEN_CODE = {
    "regions": """```python
def generate(n, rng, tables, start, end):
    return pd.DataFrame({'region_id': np.arange(1, n + 1), 'name': ['North', 'South', 'East', 'West'][:n]})
```""",
    "stores": """```python
def generate(n, rng, tables, start, end):
    return pd.DataFrame({
        'store_id': np.arange(1, n + 1),
        'region_id': rng.choice(tables['regions']['region_id'].to_numpy(), size=n),
        'opened_on': start + pd.to_timedelta(rng.integers(0, 300, size=n), unit='D'),
    })
```""",
    "sales": """```python
def generate(n, rng, tables, start, end):
    return pd.DataFrame({
        'sale_id': np.arange(1, n + 1),
        'store_id': rng.choice(tables['stores']['store_id'].to_numpy(), size=n),
        'amount': np.round(rng.gamma(2.0, 30.0, size=n), 2),
    })
```""",
}


class _FakeLLM:
    """LLM boundary stub: answers with the generator for the table asked."""

    def __init__(self, model, **kwargs):
        pass

    def inference(self, prompt, *, system=None, usage_scope=None, **kwargs):
        for name, code in GEN_CODE.items():
            if f"TABLE TO GENERATE: {name} " in prompt:
                return code
        raise AssertionError("unexpected prompt")


class _FakeModel:
    name = "stub-small"
    model_id = "stub-small"


@pytest.fixture
def stub_llm(monkeypatch):
    import app.ai.llm as llm_mod
    from app.services.llm_service import LLMService

    async def _small(self, db, organization, user, is_small=False):
        return _FakeModel()

    monkeypatch.setattr(llm_mod, "LLM", _FakeLLM)
    monkeypatch.setattr(LLMService, "get_default_model", _small)


def _spec(suffix):
    return {
        "name": f"Demo Retail {suffix}",
        "domain": "retail sales",
        "icon": "🛒",
        "date_range_start": "2025-01-01",
        "date_range_end": "2025-12-31",
        "tables": [
            {"name": "regions", "description": "one row per region", "row_count": 4,
             "columns": [{"name": "region_id", "type": "integer", "primary_key": True, "description": "Region id"},
                         {"name": "name", "type": "text", "description": "Region name"}]},
            {"name": "stores", "description": "one row per store", "row_count": 12,
             "columns": [{"name": "store_id", "type": "integer", "primary_key": True},
                         {"name": "region_id", "type": "integer", "references": "regions.region_id"},
                         {"name": "opened_on", "type": "date"}]},
            {"name": "sales", "description": "one row per sale", "row_count": 300,
             "columns": [{"name": "sale_id", "type": "integer", "primary_key": True},
                         {"name": "store_id", "type": "integer", "references": "stores.store_id"},
                         {"name": "amount", "type": "real", "description": "Sale amount USD"}]},
        ],
        "agents": [
            {"name": f"Sales {suffix}", "icon": "💰", "tables": ["sales", "stores"],
             "conversation_starters": ["Revenue by store?"],
             "instructions": ["Revenue = sum(sales.amount)."]},
            {"name": f"Footprint {suffix}", "icon": "🗺️", "tables": ["stores", "regions"]},
        ],
    }


async def _seed(enabled=True):
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Demo Org {suffix}")
        db.add(org)
        await db.flush()
        admin = User(name=f"Admin {suffix}", email=f"admin-{suffix}@example.com", hashed_password="x",
                     is_active=True, is_verified=True)
        builder = User(name=f"Builder {suffix}", email=f"builder-{suffix}@example.com", hashed_password="x",
                       is_active=True, is_verified=True)
        db.add_all([admin, builder])
        await db.flush()
        db.add_all([
            Membership(user_id=admin.id, organization_id=org.id, role="admin"),
            Membership(user_id=builder.id, organization_id=org.id, role="member"),
        ])
        # Builder can create agents but NOT connections.
        role = Role(name=f"builder-{suffix}", organization_id=org.id,
                    permissions=["create_data_source"], is_system=False)
        db.add(role)
        await db.flush()
        db.add(RoleAssignment(organization_id=org.id, role_id=role.id,
                              principal_type="user", principal_id=builder.id))
        db.add(OrganizationSettings(organization_id=org.id, config={
            "enable_demo_data_generation": {"value": enabled, "name": "Demo data generation", "description": "x"},
        }))
        report = Report(title="Training", slug=f"training-{suffix}", user_id=admin.id,
                        organization_id=org.id, mode="training")
        db.add(report)
        await db.commit()
        return {"suffix": suffix, "org": org.id, "admin": admin.id, "builder": builder.id, "report": report.id}


async def _drive(tool_input, ids, user_key="admin", decision=None):
    """Run the tool; answer the review card with ``decision`` when it shows.
    Returns (final payload output, events)."""
    from app.ai.tools.confirmation import resolve_confirmation
    from app.services.tool_confirmation_service import ToolConfirmationService

    events = []
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        user = await db.get(User, ids[user_key])
        report = await db.get(Report, ids["report"])
        settings = await org.get_settings(db)
        ctx = {"db": db, "organization": org, "user": user, "report": report, "settings": settings, "mode": "training"}
        async for ev in CreateDemoDatasetTool().run_stream(tool_input, ctx):
            events.append(ev)
            if ev.type == "tool.confirmation":
                assert decision is not None, "unexpected review pause"
                cid = ev.payload["confirmation_id"]
                async with async_session_maker() as s2:
                    await ToolConfirmationService().resolve(
                        s2, confirmation_id=cid, approved=decision["approved"], remember=False,
                        user_id=str(user.id), response=decision.get("response"),
                    )
                resolve_confirmation(cid, {"approved": decision["approved"], "remember": False,
                                           "response": decision.get("response")})
    end = events[-1]
    assert end.type == "tool.end", end
    return end.payload["output"], events


def test_tool_is_registered_for_training_mode_only():
    from app.ai.registry import ToolRegistry
    r = ToolRegistry()
    for mode in ("training", "chat", "knowledge"):
        names = set()
        for pt in ("action", "research"):
            names |= {t["name"] for t in r.get_catalog_for_plan_type(pt, mode=mode)}
        assert ("create_demo_dataset" in names) == (mode == "training"), mode


@pytest.mark.asyncio
async def test_disabled_setting_blocks_before_any_review(stub_llm):
    ids = await _seed(enabled=False)
    out, events = await _drive(_spec(ids["suffix"]), ids)
    assert out["success"] is False and out["status"] == "disabled"
    assert not any(e.type == "tool.confirmation" for e in events)


@pytest.mark.asyncio
async def test_agent_creators_without_manage_connections_are_refused(stub_llm):
    ids = await _seed()
    out, events = await _drive(_spec(ids["suffix"]), ids, user_key="builder")
    assert out["success"] is False and out["status"] == "permission_denied"
    async with async_session_maker() as db:
        conns = (await db.execute(select(Connection).where(Connection.organization_id == ids["org"]))).scalars().all()
        assert conns == []


@pytest.mark.asyncio
async def test_invalid_spec_returns_errors_without_review(stub_llm):
    ids = await _seed()
    spec = _spec(ids["suffix"])
    spec["tables"][2]["columns"][1]["references"] = "shops.store_id"
    out, events = await _drive(spec, ids)
    assert out["status"] == "invalid_spec" and out["errors"]
    assert not any(e.type == "tool.confirmation" for e in events)


@pytest.mark.asyncio
async def test_reject_with_feedback_creates_nothing_and_returns_the_feedback(stub_llm):
    ids = await _seed()
    out, _ = await _drive(_spec(ids["suffix"]), ids,
                          decision={"approved": False, "response": {"feedback": "add a returns table"}})
    assert out["success"] is False and out["status"] == "rejected"
    assert out["feedback"] == "add a returns table"
    async with async_session_maker() as db:
        conns = (await db.execute(select(Connection).where(Connection.organization_id == ids["org"]))).scalars().all()
        assert conns == []


@pytest.mark.asyncio
async def test_approve_creates_connection_and_only_the_ticked_agents(stub_llm):
    ids = await _seed()
    spec = _spec(ids["suffix"])
    ticked = spec["agents"][0]["name"]
    out, events = await _drive(spec, ids, decision={"approved": True, "response": {"agents": [ticked]}})

    assert out["success"] is True and out["status"] == "created", out
    assert {t["name"]: t["rows"] for t in out["tables"]} == {"regions": 4, "stores": 12, "sales": 300}
    assert [a["name"] for a in out["agents"]] == [ticked]
    assert any(e.type == "tool.progress" and e.payload.get("stage") == "generating" for e in events)

    async with async_session_maker() as db:
        conn = await db.get(Connection, out["connection_id"])
        assert conn.type == "sqlite"
        import json as _json
        cfg = _json.loads(conn.config) if isinstance(conn.config, str) else conn.config
        path = cfg["database"]
        assert os.path.exists(path) and os.path.realpath(path).startswith(os.path.realpath("uploads/demo_data"))

        agent = (await db.execute(select(DataSource).where(DataSource.id == out["agents"][0]["data_source_id"]))).scalar_one()
        assert agent.icon == "emoji:💰"
        rows = (await db.execute(select(DataSourceTable).where(DataSourceTable.datasource_id == str(agent.id)))).scalars().all()
        active = sorted(r.name for r in rows if r.is_active)
        assert active == ["sales", "stores"]

        names = (await db.execute(select(DataSource.name).where(DataSource.organization_id == ids["org"]))).scalars().all()
        assert spec["agents"][1]["name"] not in names  # unticked → not created

        report = await db.get(Report, ids["report"])
        await db.refresh(report, ["data_sources"])
        assert str(agent.id) in {str(d.id) for d in report.data_sources}

    # Deleting the connection removes the generated file with it.
    from app.services.connection_service import ConnectionService
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        admin = await db.get(User, ids["admin"])
        await ConnectionService().delete_connection(db, out["connection_id"], org, admin)
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_rerun_with_taken_names_suffixes_agents_and_keeps_the_run_session_usable(stub_llm):
    """Re-running the same demo must still create its agents (suffixed), and
    nothing in the install may roll back the agent loop's shared session —
    a rollback there expires the run's own objects mid-loop."""
    from app.ai.tools.confirmation import resolve_confirmation
    from app.services.tool_confirmation_service import ToolConfirmationService

    ids = await _seed()
    spec = _spec(ids["suffix"])
    first, _ = await _drive(spec, ids, decision={"approved": True, "response": {"agents": [a["name"] for a in spec["agents"]]}})
    assert first["status"] == "created"

    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        user = await db.get(User, ids["admin"])
        report = await db.get(Report, ids["report"])
        settings = await org.get_settings(db)
        ctx = {"db": db, "organization": org, "user": user, "report": report, "settings": settings, "mode": "training"}
        end = None
        async for ev in CreateDemoDatasetTool().run_stream(spec, ctx):
            if ev.type == "tool.confirmation":
                cid = ev.payload["confirmation_id"]
                resolve_confirmation(cid, {"approved": True, "response": {"agents": [a["name"] for a in spec["agents"]]}})
            end = ev
        out = end.payload["output"]
        assert out["status"] == "created", out
        names = {a["name"] for a in out["agents"]}
        assert names == {f"{a['name']} (2)" for a in spec["agents"]}
        assert {a["requested_name"] for a in out["agents"]} == {a["name"] for a in spec["agents"]}
        # Still loaded, not expired: a plain attribute read must not need IO.
        assert report.title == "Training" and org.name.startswith("Demo Org")
