"""Training-mode connection/agent tools: list_connections / get_connection / create_agent.

Exercises the tools through run_stream against the real DataSourceService and
permission resolver, including the create-agents tier (org `create_data_source`
+ per-connection `create_data_sources`) that the tools must enforce themselves
(the AI tool path bypasses HTTP route decorators).

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true \
      .venv/bin/python -m pytest tests/training/test_connection_agent_tools.py -v -s
"""
import uuid
import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.connection_tool import ConnectionTool
from app.models.data_source import DataSource
from app.models.data_source_connection_tool import DataSourceConnectionTool
from app.models.datasource_table import DataSourceTable
from app.models.membership import Membership
from app.models.report import Report
from app.models.resource_grant import ResourceGrant
from app.models.role import Role
from app.models.role_assignment import RoleAssignment

from app.ai.tools.implementations.list_connections import ListConnectionsTool
from app.ai.tools.implementations.get_connection import GetConnectionTool
from app.ai.tools.implementations.create_agent import CreateAgentTool

from sqlalchemy import select


async def _final(tool, tool_input, ctx):
    """Drain run_stream and return the terminal event's payload."""
    last = None
    async for ev in tool.run_stream(tool_input, ctx):
        last = ev
    return last


def _u(suffix, name):
    return User(name=f"{name} {suffix}", email=f"{name.lower()}-{suffix}@example.com",
                hashed_password="x", is_active=True, is_verified=True)


def _tbl(conn_id, name, schema, cols=3, rows=100):
    return ConnectionTable(
        name=name, connection_id=conn_id,
        columns=[{"name": f"c{i}", "dtype": "text"} for i in range(cols)],
        pks=[], fks=[], no_rows=rows,
        metadata_json={"schema": schema} if schema else None,
    )


async def _seed():
    """Org with: creator (create tier on conn_main only), plain member, admin.

    conn_main   postgres  4 tables across 2 schemas (sales, analytics)
    conn_other  postgres  1 table  — creator holds NO grant on it
    conn_mcp    mcp       3 tools  — admin-only via full admin
    """
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"ConnTool Org {suffix}")
        db.add(org); await db.flush()

        creator, member, admin = _u(suffix, "Creator"), _u(suffix, "Member"), _u(suffix, "Admin")
        db.add_all([creator, member, admin]); await db.flush()
        db.add_all([
            Membership(user_id=creator.id, organization_id=org.id, role="member"),
            Membership(user_id=member.id, organization_id=org.id, role="member"),
            Membership(user_id=admin.id, organization_id=org.id, role="admin"),
        ])

        # Org-level create_data_source for the creator via a custom role.
        role = Role(name=f"agent-builder-{suffix}", organization_id=org.id,
                    permissions=["create_data_source"], is_system=False)
        db.add(role); await db.flush()
        db.add(RoleAssignment(organization_id=org.id, role_id=role.id,
                              principal_type="user", principal_id=creator.id))

        conn_main = Connection(name=f"warehouse-{suffix}", type="postgresql",
                               config="{}", organization_id=org.id, is_active=True)
        conn_other = Connection(name=f"other-{suffix}", type="postgresql",
                                config="{}", organization_id=org.id, is_active=True)
        conn_mcp = Connection(name=f"boards-{suffix}", type="mcp",
                              config="{}", organization_id=org.id, is_active=True)
        db.add_all([conn_main, conn_other, conn_mcp]); await db.flush()

        db.add_all([
            _tbl(conn_main.id, "orders", "sales", cols=5, rows=1000),
            _tbl(conn_main.id, "customers", "sales", cols=4, rows=200),
            _tbl(conn_main.id, "events", "analytics", cols=6, rows=9999),
            _tbl(conn_main.id, "sessions", "analytics", cols=2, rows=50),
            _tbl(conn_other.id, "misc", None, cols=1, rows=1),
        ])
        db.add_all([
            ConnectionTool(name="get_board", connection_id=conn_mcp.id,
                           description="Read one board", is_enabled=True, policy="allow"),
            ConnectionTool(name="list_items", connection_id=conn_mcp.id,
                           description="List items", is_enabled=True, policy="allow"),
            ConnectionTool(name="send_message", connection_id=conn_mcp.id,
                           description="Write access", is_enabled=True, policy="confirm"),
        ])

        # Per-connection create tier for the creator on conn_main only.
        db.add(ResourceGrant(
            organization_id=org.id, resource_type="connection", resource_id=str(conn_main.id),
            principal_type="user", principal_id=creator.id, permissions=["create_data_sources"],
        ))

        report = Report(title="Training session", slug=f"training-{suffix}",
                        user_id=creator.id, organization_id=org.id, mode="training")
        db.add(report)
        await db.commit()
        return {
            "suffix": suffix, "org": org.id,
            "creator": creator.id, "member": member.id, "admin": admin.id,
            "conn_main": str(conn_main.id), "conn_other": str(conn_other.id),
            "conn_mcp": str(conn_mcp.id), "report": str(report.id),
        }


def test_registry_gates_tools_to_training_mode():
    from app.ai.registry import ToolRegistry
    r = ToolRegistry()
    new_tools = {"list_connections", "get_connection", "create_agent"}
    for mode in ("training", "chat", "deep", "knowledge"):
        names = set()
        for pt in ("action", "research"):
            names |= {t["name"] for t in r.get_catalog_for_plan_type(pt, mode=mode)}
        if mode == "training":
            assert new_tools <= names, names
        else:
            assert not (new_tools & names), (mode, new_tools & names)


@pytest.mark.asyncio
async def test_list_connections_scoped_to_create_tier():
    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        creator = await db.get(User, ids["creator"])
        member = await db.get(User, ids["member"])
        admin = await db.get(User, ids["admin"])

        # Creator: exactly the connection they hold the grant on.
        ev = await _final(ListConnectionsTool(), {}, {"db": db, "organization": org, "user": creator})
        out = ev.payload["output"]
        assert out["success"] is True
        got = {c["id"] for c in out["connections"]}
        assert got == {ids["conn_main"]}, got
        main = out["connections"][0]
        assert main["data_shape"] == "tables"
        assert sorted(main["schemas"]) == ["analytics", "sales"]
        assert main["table_count"] == 4

        # Admin: every connection, including the tools-shaped one.
        ev = await _final(ListConnectionsTool(), {}, {"db": db, "organization": org, "user": admin})
        got = {c["id"] for c in ev.payload["output"]["connections"]}
        assert {ids["conn_main"], ids["conn_other"], ids["conn_mcp"]} <= got
        by_id = {c["id"]: c for c in ev.payload["output"]["connections"]}
        assert by_id[ids["conn_mcp"]]["data_shape"] == "tools"
        assert by_id[ids["conn_mcp"]]["tool_count"] == 3

        # Plain member: nothing (lacks org create_data_source entirely).
        ev = await _final(ListConnectionsTool(), {}, {"db": db, "organization": org, "user": member})
        out = ev.payload["output"]
        assert out["success"] is True and out["connections"] == [] and out["total"] == 0

        # Glob search narrows by name/type.
        ev = await _final(ListConnectionsTool(), {"search": "warehouse*"},
                          {"db": db, "organization": org, "user": admin})
        got = {c["id"] for c in ev.payload["output"]["connections"]}
        assert got == {ids["conn_main"]}, got


@pytest.mark.asyncio
async def test_get_connection_tables_glob_schema_pagination():
    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        creator = await db.get(User, ids["creator"])
        ctx = {"db": db, "organization": org, "user": creator}

        # Full catalog: 4 tables, both schemas reported.
        ev = await _final(GetConnectionTool(), {"connection_id": ids["conn_main"]}, ctx)
        out = ev.payload["output"]
        assert out["success"] is True and out["data_shape"] == "tables"
        assert out["total"] == 4
        assert sorted(out["schemas"]) == ["analytics", "sales"]
        by_name = {t["name"]: t for t in out["tables"]}
        assert by_name["orders"]["schema_name"] == "sales"
        assert by_name["orders"]["column_count"] == 5
        assert by_name["orders"]["row_count"] == 1000

        # Schema filter.
        ev = await _final(GetConnectionTool(),
                          {"connection_id": ids["conn_main"], "schema_name": "analytics"}, ctx)
        out = ev.payload["output"]
        assert {t["name"] for t in out["tables"]} == {"events", "sessions"} and out["total"] == 2

        # Glob pattern (substring + glob forms).
        ev = await _final(GetConnectionTool(),
                          {"connection_id": ids["conn_main"], "pattern": "s*s"}, ctx)
        assert {t["name"] for t in ev.payload["output"]["tables"]} == {"sessions"}
        ev = await _final(GetConnectionTool(),
                          {"connection_id": ids["conn_main"], "pattern": "order"}, ctx)
        assert {t["name"] for t in ev.payload["output"]["tables"]} == {"orders"}

        # Pagination.
        ev = await _final(GetConnectionTool(),
                          {"connection_id": ids["conn_main"], "page": 1, "page_size": 3}, ctx)
        out = ev.payload["output"]
        assert len(out["tables"]) == 3 and out["has_more"] is True and out["total"] == 4
        ev = await _final(GetConnectionTool(),
                          {"connection_id": ids["conn_main"], "page": 2, "page_size": 3}, ctx)
        out = ev.payload["output"]
        assert len(out["tables"]) == 1 and out["has_more"] is False

        # No grant on conn_other → permission_denied, not a leak.
        ev = await _final(GetConnectionTool(), {"connection_id": ids["conn_other"]}, ctx)
        out = ev.payload["output"]
        assert out["success"] is False and out["rejected_reason"] == "permission_denied"


@pytest.mark.asyncio
async def test_get_connection_tools_shape():
    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        admin = await db.get(User, ids["admin"])
        ctx = {"db": db, "organization": org, "user": admin}

        ev = await _final(GetConnectionTool(), {"connection_id": ids["conn_mcp"]}, ctx)
        out = ev.payload["output"]
        assert out["success"] is True and out["data_shape"] == "tools"
        assert out["total"] == 3
        by_name = {t["name"]: t for t in out["tools"]}
        assert by_name["send_message"]["default_policy"] in ("confirm", "ask")
        assert by_name["get_board"]["default_enabled"] is True

        ev = await _final(GetConnectionTool(),
                          {"connection_id": ids["conn_mcp"], "pattern": "get_*"}, ctx)
        assert {t["name"] for t in ev.payload["output"]["tools"]} == {"get_board"}


@pytest.mark.asyncio
async def test_create_agent_with_schema_selection_and_report_attach():
    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        creator = await db.get(User, ids["creator"])
        report = await db.get(Report, ids["report"])
        ctx = {"db": db, "organization": org, "user": creator, "report": report, "mode": "training"}

        ev = await _final(CreateAgentTool(), {
            "name": f"Sales Agent {ids['suffix']}",
            "connection_ids": [ids["conn_main"]],
            "description": "Sales reporting",
            "schemas": ["Sales"],  # case-insensitive
        }, ctx)
        out = ev.payload["output"]
        assert out["success"] is True, out
        assert out["tables_total"] == 4
        assert out["tables_active"] == 2
        assert set(out["active_table_sample"]) == {"orders", "customers"}
        assert out["unresolved"] == []
        assert out["attached_to_report"] is True
        ds_id = out["data_source_id"]

        # DB truth: only the sales tables are active.
        rows = (await db.execute(
            select(DataSourceTable).where(DataSourceTable.datasource_id == ds_id)
        )).scalars().all()
        assert {r.name for r in rows if r.is_active} == {"orders", "customers"}

        # Attached to the training session's report.
        await db.refresh(report)
        assert ds_id in {str(d.id) for d in report.data_sources}

        # Creator can manage the new agent (the grant instruction tools key on).
        from app.core.permission_resolver import resolve_permissions
        resolved = await resolve_permissions(db, str(creator.id), str(org.id))
        assert resolved.has_resource_permission("data_source", ds_id, "manage_instructions")

        # Duplicate name → clean name_taken rejection.
        ev = await _final(CreateAgentTool(), {
            "name": f"Sales Agent {ids['suffix']}",
            "connection_ids": [ids["conn_main"]],
        }, ctx)
        out = ev.payload["output"]
        assert out["success"] is False and out["rejected_reason"] == "name_taken", out


@pytest.mark.asyncio
async def test_agent_with_tool_overlays_can_be_deleted():
    """Deleting an agent whose tool selection created overlay rows must work —
    the ORM cascades the DataSourceConnectionTool rows instead of NULLing
    their NOT NULL FK (SQLite doesn't enforce the DDL ON DELETE CASCADE)."""
    from app.services.data_source_service import DataSourceService

    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        admin = await db.get(User, ids["admin"])
        ctx = {"db": db, "organization": org, "user": admin}

        ev = await _final(CreateAgentTool(), {
            "name": f"Disposable {ids['suffix']}",
            "connection_ids": [ids["conn_mcp"]],
            "tools": ["get_*"],
        }, ctx)
        ds_id = ev.payload["output"]["data_source_id"]
        overlays = (await db.execute(
            select(DataSourceConnectionTool).where(DataSourceConnectionTool.data_source_id == ds_id)
        )).scalars().all()
        assert overlays, "selection must have created overlay rows"

        await DataSourceService().delete_data_source(db, ds_id, org, admin)

        assert (await db.execute(
            select(DataSource).where(DataSource.id == ds_id)
        )).scalar_one_or_none() is None
        assert not (await db.execute(
            select(DataSourceConnectionTool).where(DataSourceConnectionTool.data_source_id == ds_id)
        )).scalars().all()


@pytest.mark.asyncio
async def test_instruction_after_create_agent_scopes_to_new_agent():
    """The keystone integration: an instruction created later in the SAME run
    (same ctx/report) attaches to the just-created agent, not org-wide."""
    from app.ai.tools.implementations.create_instruction import CreateInstructionTool
    from app.models.instruction import Instruction
    from sqlalchemy.orm import selectinload

    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        admin = await db.get(User, ids["admin"])
        report = await db.get(Report, ids["report"])
        ctx = {"db": db, "organization": org, "user": admin, "report": report, "mode": "training"}

        ev = await _final(CreateAgentTool(), {
            "name": f"Scoped Agent {ids['suffix']}",
            "connection_ids": [ids["conn_main"]],
            "schemas": ["sales"],
        }, ctx)
        ds_id = ev.payload["output"]["data_source_id"]
        assert ev.payload["output"]["attached_to_report"] is True

        ev = await _final(CreateInstructionTool(), {
            "text": "Revenue figures are always in USD.",
            "category": "general",
            "confidence": 0.95,
            "load_mode": "always",
        }, ctx)
        out = ev.payload["output"]
        assert out["success"] is True, out
        inst = (await db.execute(
            select(Instruction)
            .options(selectinload(Instruction.data_sources))
            .where(Instruction.id == out["instruction_id"])
        )).scalar_one()
        attached = {str(d.id) for d in (inst.data_sources or [])}
        # Scoped to the report's agents — which now include the new one — and
        # never a global (data-source-less) instruction.
        assert ds_id in attached, attached
        assert attached, "instruction must not be global"


@pytest.mark.asyncio
async def test_create_agent_permission_gates():
    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        creator = await db.get(User, ids["creator"])
        member = await db.get(User, ids["member"])

        # Plain member: lacks org create_data_source.
        ev = await _final(CreateAgentTool(), {
            "name": f"Nope {ids['suffix']}", "connection_ids": [ids["conn_main"]],
        }, {"db": db, "organization": org, "user": member})
        out = ev.payload["output"]
        assert out["success"] is False and out["rejected_reason"] == "permission_denied"

        # Creator on a connection they hold no grant on.
        ev = await _final(CreateAgentTool(), {
            "name": f"Nope2 {ids['suffix']}", "connection_ids": [ids["conn_other"]],
        }, {"db": db, "organization": org, "user": creator})
        out = ev.payload["output"]
        assert out["success"] is False and out["rejected_reason"] == "permission_denied"

        # Unknown connection id.
        ev = await _final(CreateAgentTool(), {
            "name": f"Nope3 {ids['suffix']}", "connection_ids": [str(uuid.uuid4())],
        }, {"db": db, "organization": org, "user": creator})
        out = ev.payload["output"]
        assert out["success"] is False and out["rejected_reason"] == "connection_not_found"

        # Nothing was created by any of the denied calls.
        names = (await db.execute(
            select(DataSource.name).where(DataSource.organization_id == str(org.id))
        )).scalars().all()
        assert not any(n.startswith("Nope") for n in names), names


@pytest.mark.asyncio
async def test_create_agent_unresolved_selection_keeps_defaults():
    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        creator = await db.get(User, ids["creator"])
        ctx = {"db": db, "organization": org, "user": creator}

        ev = await _final(CreateAgentTool(), {
            "name": f"Ghost Agent {ids['suffix']}",
            "connection_ids": [ids["conn_main"]],
            "schemas": ["nonexistent_schema"],
            "tables": ["bogus_*"],
        }, ctx)
        out = ev.payload["output"]
        assert out["success"] is True, out
        # Nothing matched → the seeded default selection is kept, and every
        # requested selector is reported back, never silently dropped.
        assert set(out["unresolved"]) == {"nonexistent_schema", "bogus_*"}, out
        assert out["tables_total"] == 4


@pytest.mark.asyncio
async def test_create_agent_tool_selection_overlay():
    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        admin = await db.get(User, ids["admin"])
        ctx = {"db": db, "organization": org, "user": admin}

        ev = await _final(CreateAgentTool(), {
            "name": f"Boards Reader {ids['suffix']}",
            "connection_ids": [ids["conn_mcp"]],
            "tools": ["get_*", "list_*"],
        }, ctx)
        out = ev.payload["output"]
        assert out["success"] is True, out
        assert out["tools_total"] == 3
        assert out["tools_enabled"] == 2
        ds_id = out["data_source_id"]

        overlays = (await db.execute(
            select(DataSourceConnectionTool).where(
                DataSourceConnectionTool.data_source_id == ds_id
            )
        )).scalars().all()
        tools = (await db.execute(
            select(ConnectionTool).where(ConnectionTool.connection_id == ids["conn_mcp"])
        )).scalars().all()
        name_by_id = {str(t.id): t.name for t in tools}
        enabled = {name_by_id[str(o.connection_tool_id)] for o in overlays if o.is_enabled}
        disabled = {name_by_id[str(o.connection_tool_id)] for o in overlays if not o.is_enabled}
        assert enabled == {"get_board", "list_items"}
        assert disabled == {"send_message"}
