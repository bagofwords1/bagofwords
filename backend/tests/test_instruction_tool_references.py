"""get_available_references must offer connection tools for agent-scoped instructions.

The instruction editor's @-mention dropdown and the References picker both call
this service. Connection tools are only resolved when the request carries a
data-source (agent) scope — these tests pin the scoped path so tool mentions
keep working from the editor.
"""
import uuid
import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.membership import Membership
from app.models.connection import Connection
from app.models.connection_tool import ConnectionTool
from app.models.data_source import DataSource
from app.models.domain_connection import domain_connection

from app.services.instruction_service import InstructionService


async def _seed():
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"ReproOrg {suffix}")
        db.add(org)
        await db.flush()

        admin = User(name=f"Admin {suffix}", email=f"admin-{suffix}@example.com",
                     hashed_password="x", is_active=True, is_verified=True)
        db.add(admin)
        await db.flush()
        db.add(Membership(user_id=admin.id, organization_id=org.id, role="admin"))

        conn_mcp = Connection(name=f"lnmcp-{suffix}", type="mcp",
                              config="{}", organization_id=org.id, is_active=True)
        db.add(conn_mcp)
        await db.flush()

        db.add_all([
            ConnectionTool(name="get_contract_deliverable", connection_id=conn_mcp.id,
                           description="Retrieve contract details", is_enabled=True, policy="allow"),
            ConnectionTool(name="query_production_orders_with_prompt", connection_id=conn_mcp.id,
                           description="Query LN ERP production orders", is_enabled=True, policy="allow"),
        ])

        ds = DataSource(name=f"LNTesting-{suffix}",
                        organization_id=org.id, is_public=True)
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(
            data_source_id=str(ds.id), connection_id=str(conn_mcp.id)))
        await db.commit()
        return {"org": org.id, "admin": admin.id, "ds": str(ds.id)}


@pytest.mark.asyncio
async def test_available_references_includes_tools_when_scoped():
    ids = await _seed()
    svc = InstructionService()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        admin = await db.get(User, ids["admin"])

        # Exactly what the instruction editor requests when scoped to one agent
        items = await svc.get_available_references(
            db, org, admin,
            q=None,
            types="instruction,datasource_table,metadata_resource,connection_tool",
            data_source_ids=ids["ds"],
        )
        tools = [i for i in items if i.get("type") == "connection_tool"]
        print("\nSCOPED result types:", [(i.get("type"), i.get("name")) for i in items])
        assert len(tools) == 2, f"expected 2 tools, got {tools}"

        # And what it requests with a search query, as typed after '@'
        items_q = await svc.get_available_references(
            db, org, admin,
            q="contract",
            types="instruction,datasource_table,metadata_resource,connection_tool",
            data_source_ids=ids["ds"],
        )
        tools_q = [i for i in items_q if i.get("type") == "connection_tool"]
        print("QUERY result:", [(i.get("type"), i.get("name")) for i in items_q])
        assert len(tools_q) == 1, f"expected 1 tool for q=contract, got {tools_q}"
