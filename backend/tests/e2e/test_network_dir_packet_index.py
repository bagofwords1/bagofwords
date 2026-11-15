"""Connecting a mixed share produces SQL relations for the agent in the background."""

import asyncio
import shutil

import duckdb
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
from app.models.organization import Organization
from app.dependencies import async_session_maker
from app.models.data_source import DataSource
from app.models.user import User
from app.services.data_source_service import DataSourceService
from tests.e2e.test_network_dir_incremental_e2e import _poll_until_terminal
from tests.fixtures.packet_captures import ARP, IP, write_capture


@pytest.mark.skipif(not shutil.which("tshark"), reason="requires the runtime TShark binary")
def test_connection_index_reaches_agent_sql(
    create_user, login_user, whoami, create_connection, create_domain_from_connection, test_client, tmp_path
):
    write_capture(tmp_path / "arp.pcap", [ARP])
    write_capture(tmp_path / "dns.pcapng", [IP, IP])
    (tmp_path / "notes.txt").write_text("incident notes")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    conn = create_connection(
        name="Capture share",
        type="network_dir",
        config={"root_path": str(tmp_path), "index_mode": "fulltext"},
        credentials={},
        user_token=token,
        org_id=org_id,
    )
    assert _poll_until_terminal(test_client, conn["id"], headers)["status"] == "completed"
    agent = create_domain_from_connection(name="Traffic", connection_id=conn["id"], user_token=token, org_id=org_id)
    result = test_client.get(f"/api/data_sources/{agent['id']}/full_schema?page=1", headers=headers)
    assert result.status_code == 200, result.text
    assert {t["name"] for t in result.json()["tables"]} == {"indexed_files", "packets"}

    async def query_agent(packets_enabled):
        async with async_session_maker() as db:
            source = (
                await db.execute(
                    select(DataSource).options(selectinload(DataSource.connections)).where(DataSource.id == agent["id"])
                )
            ).scalar_one()
            caller = (await db.execute(select(User).where(User.email == user["email"]))).scalar_one()
            org = await db.get(Organization, org_id)
            context = await SchemaContextBuilder(db, [source], org, report=None, user=caller).build(with_stats=False)
            scope = context.data_sources[0]
            assert scope.file_scopes
            schemas = {t.name: t for t in scope.tables}
            assert "indexed_files" in schemas
            assert ("packets" in schemas) == packets_enabled
            if packets_enabled:
                assert "dns.a" in {c.name for c in schemas["packets"].columns}
            clients = await DataSourceService().construct_clients(db, source, caller)
            client = clients[source.name]
            coverage = client.execute_query("SELECT file_id, status FROM indexed_files")
            assert dict(coverage.itertuples(index=False, name=None)) == {"arp.pcap": "ready", "dns.pcapng": "ready"}
            if not packets_enabled:
                with pytest.raises(duckdb.CatalogException):
                    client.execute_query("SELECT * FROM packets")
                return
            answer = client.execute_query('SELECT count(*) AS frames, count("dns.a") AS dns FROM packets')
            assert tuple(answer.iloc[0]) == (3, 2)

    asyncio.run(query_agent(True))
    changed = test_client.put(
        f"/api/data_sources/{agent['id']}/update_tables_status",
        json={"deactivate": ["packets"]},
        headers=headers,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["deactivated_count"] == 1
    asyncio.run(query_agent(False))
