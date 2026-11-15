"""Connecting a mixed share produces SQL relations for the agent in the background."""

import asyncio
import shutil
from pathlib import Path
from threading import Event

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
    create_user,
    login_user,
    whoami,
    create_connection,
    create_domain_from_connection,
    test_client,
    tmp_path,
    monkeypatch,
):
    write_capture(tmp_path / "arp.pcap", [ARP])
    write_capture(tmp_path / "dns.pcapng", [IP, IP])
    (tmp_path / "notes.txt").write_text("incident notes")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    from app.data_sources.clients import _network_dir_tables
    from tests.e2e.test_network_dir_incremental_e2e import _catalog_meta

    entered, resume = Event(), Event()
    convert = _network_dir_tables.convert_capture

    def paused_decode(*args, **kwargs):
        entered.set()
        assert resume.wait(30)
        return convert(*args, **kwargs)

    monkeypatch.setattr(_network_dir_tables, "convert_capture", paused_decode)
    conn = create_connection(
        name="Capture share",
        type="network_dir",
        config={"root_path": str(tmp_path), "index_mode": "fulltext"},
        credentials={},
        user_token=token,
        org_id=org_id,
    )
    try:
        assert entered.wait(20)
        pending = asyncio.run(_catalog_meta(conn["id"]))
        assert "notes.txt" in pending and pending["dns.pcapng"]["status"] == "pending"
    finally:
        resume.set()
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
                assert {c.name for c in schemas["packets"].columns} == {
                    "capture_id",
                    "frame_number",
                    "frame_bytes",
                    "epoch_seconds",
                }
            clients = await DataSourceService().construct_clients(db, source, caller)
            client = clients[source.name]
            coverage = client.execute_query("SELECT file_id, status FROM indexed_files")
            assert dict(coverage.itertuples(index=False, name=None)) == {"arp.pcap": "ready", "dns.pcapng": "ready"}
            if not packets_enabled:
                with pytest.raises(duckdb.CatalogException):
                    client.execute_query("SELECT * FROM packets")
                return
            discovered = client.execute_query(
                "SELECT column_name FROM duckdb_columns() WHERE table_name='packets' AND column_name LIKE 'dns.%'"
            )
            assert "dns.a" in set(discovered.column_name)
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

    qvd = Path(__file__).resolve().parents[1] / "config/test_source.qvd"
    shutil.copyfile(qvd, tmp_path / "new.qvd")
    refresh = test_client.post(f"/api/connections/{conn['id']}/reindex", headers=headers)
    assert refresh.status_code in (200, 202), refresh.text
    assert _poll_until_terminal(test_client, conn["id"], headers)["status"] == "completed"
    catalog = test_client.get(f"/api/data_sources/{agent['id']}/full_schema?page=1", headers=headers)
    assert catalog.status_code == 200, catalog.text
    (qvd_table,) = [t for t in catalog.json()["tables"] if t["name"].startswith("qvd_new_")]
    assert not qvd_table["is_active"]
