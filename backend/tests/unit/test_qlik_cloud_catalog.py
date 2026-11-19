"""Cloud discovery contracts using entirely synthetic REST and Engine fixtures."""
import copy
import sys
from types import SimpleNamespace

import pytest

from app.data_sources.clients.qlik_sense_client import QlikSenseClient
from tests.fixtures.fake_qlik_engine import FakeQlikSite
from tests.unit.test_qlik_sense_client import _install_rest, APP_METADATA_POPULATED
from tests.unit.test_qlik_sense_onprem_client import make_client


def cloud(site, monkeypatch, **options):
    client = QlikSenseClient(base_url="https://qlik.example.test", api_key="test-only", **options)
    _install_rest(client, {"/api/v1/items": {"data": [
        {"id": "item-" + a["id"], "resourceId": a["id"], "name": a["name"],
         "spaceId": (a.get("stream") or {}).get("id"),
         "spaceName": (a.get("stream") or {}).get("name"),
         "resourceAttributes": {"publishTime": "2026-01-01" if a.get("published") else ""}}
        for a in site.qrs_apps
    ], "links": {}}}, default={"tables": [], "fields": []})
    monkeypatch.setitem(sys.modules, "websockets", site.websockets_module())
    return client


@pytest.mark.parametrize("rich", [True, False])
def test_cloud_and_onprem_discover_same_engine_catalog(monkeypatch, rich):
    site = FakeQlikSite.demo()
    options = dict(include_master_items=rich, include_lineage=rich, include_sheet_objects=rich)
    c = cloud(site, monkeypatch, **options)
    onprem = make_client(**options)
    site.install(monkeypatch, onprem)
    try:
        expected = onprem.get_schemas()
        actual = c.get_schemas()
        assert {t.name for t in actual} == {t.name for t in expected}
        assert {t.name: [(v.name, v.dtype) for v in t.columns] for t in actual} == {
            t.name: [(v.name, v.dtype) for v in t.columns] for t in expected}
        assert all("qlik_sense" in t.metadata_json and "qlik_sense_onprem" not in t.metadata_json for t in actual)
        if rich:
            m = next(t for t in actual if t.metadata_json["qlik_sense"].get("objectType") == "master_items")
            assert m.metadata_json["qlik_sense"]["variables"]
            assert m.metadata_json["qlik_sense"]["lineage"]
            assert any(c.dtype == "measure" and "Sum(" in c.description for c in m.columns)
    finally:
        onprem.close()


def test_cloud_can_query_discovered_sheet_objects_and_search(monkeypatch):
    c = cloud(FakeQlikSite.demo(), monkeypatch)
    tables = c.get_schemas()
    assert any("/Sheets/" in t.name for t in tables)
    result = c.execute_query(app="Sales/Consumer Sales", object_id="obj-cy-kpi")
    assert not result.empty
    matches = c.execute_query(app="Sales/Consumer Sales", search="north")
    assert not matches.empty
    assert {"field", "value"}.issubset(matches.columns)


@pytest.mark.parametrize("row_count", [0, 7])
def test_rest_maps_documented_fields_and_omits_system_tables(monkeypatch, row_count):
    c = cloud(FakeQlikSite.demo(), monkeypatch, include_master_items=False, include_lineage=False, include_sheet_objects=False)
    meta = copy.deepcopy(APP_METADATA_POPULATED)
    meta["tables"][0]["no_of_rows"] = row_count
    meta["tables"].append({"name": "$Internal", "is_system": True, "no_of_fields": 1})
    meta["fields"][0]["src_tables"].append("$Internal")
    _install_rest(c, {"/api/v1/items": {"data": [{"resourceId": "demo", "name": "Demo"}]},
                      "/api/v1/apps/demo/data/metadata": meta})
    tables = c.get_schemas()
    assert {t.name for t in tables} == {"Demo/Sales", "Demo/Customers"}
    sales = next(t for t in tables if t.name == "Demo/Sales")
    assert {col.name for col in sales.columns} == {"OrderID", "Region", "Sales"}
    assert sales.metadata_json["qlik_sense"]["rowCount"] == row_count
    assert {fk.references_name for fk in sales.fks} == {"Demo/Customers"}


@pytest.mark.parametrize("broken", ["missing_membership", "partial_membership"])
def test_incomplete_rest_model_falls_back_to_engine(monkeypatch, broken):
    site = FakeQlikSite.demo()
    c = cloud(site, monkeypatch, include_master_items=False, include_lineage=False, include_sheet_objects=False)
    meta = copy.deepcopy(APP_METADATA_POPULATED)
    for field in meta["fields"]:
        field["src_tables"] = [] if broken == "missing_membership" else ["Sales"]
    _install_rest(c, {"/api/v1/items": {"data": [
        {"resourceId": a["id"], "name": a["name"]} for a in site.qrs_apps]},
        "/data/metadata": meta})
    tables = c.get_schemas()
    assert tables and all(t.columns for t in tables)
    assert all(t.metadata_json["qlik_sense"]["source"] == "qix" for t in tables)


def test_engine_failure_preserves_rest_model_and_marks_partial(monkeypatch):
    c = cloud(FakeQlikSite.demo(), monkeypatch)
    def unavailable(*args, **kwargs):
        raise ConnectionError("synthetic Engine outage")
    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=unavailable))
    _install_rest(c, {"/api/v1/items": {"data": [{"resourceId": "demo", "name": "Demo"}]},
                      "/api/v1/apps/demo/data/metadata": APP_METADATA_POPULATED})
    tables = c.get_schemas()
    assert tables and all(t.columns for t in tables)
    assert all(t.metadata_json["qlik_sense"].get("discoveryWarning") for t in tables)
    assert c.unindexed_table_prefixes() == ["Demo"]


def test_total_discovery_failure_is_not_success(monkeypatch):
    c = cloud(FakeQlikSite.demo(), monkeypatch)
    def unavailable(*args, **kwargs):
        raise ConnectionError("synthetic Engine outage")
    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=unavailable))
    with pytest.raises(RuntimeError):
        c.get_schemas()


def test_cloud_scope_rejects_unknown_apps_and_accepts_table_paths(monkeypatch):
    c = cloud(FakeQlikSite.demo(), monkeypatch, app_filter="Consumer Sales", max_apps=1)
    assert len(c.list_apps()) == 1
    assert not c.execute_query(app="Sales/Consumer Sales/Sales", dimensions=["Region Name"]).empty
    with pytest.raises(ValueError):
        c.execute_query(app="outside-scope", dimensions=["Region Name"])


def test_discovery_cap_does_not_restrict_queries_within_scope(monkeypatch):
    site = FakeQlikSite.demo()
    first = site.qrs_apps[0]
    second = copy.deepcopy(first)
    second.update(id="demo-second", name="Second Model")
    site.qrs_apps.append(second)
    site.apps[second["id"]] = copy.deepcopy(site.apps[first["id"]])
    site.apps[second["id"]].id = second["id"]
    c = cloud(site, monkeypatch, max_apps=1)
    assert len(c.list_apps()) == 1
    assert not c.execute_query(app=second["id"], dimensions=["Region Name"]).empty


def test_private_sheets_are_excluded_in_shared_cloud_spaces(monkeypatch):
    c = cloud(FakeQlikSite.demo(), monkeypatch)
    tables = c.get_schemas()
    assert not any("Private" in t.name for t in tables)
    assert any("/Sheets/" in t.name for t in tables)


def test_cloud_engine_auth_never_impersonates_an_onprem_user(monkeypatch):
    site = FakeQlikSite.demo()
    c = cloud(site, monkeypatch)
    ws = site.websockets_module()
    headers = []
    original = ws.connect
    def connect(uri, **kwargs):
        headers.append(dict(kwargs["additional_headers"]))
        return original(uri, **kwargs)
    ws.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", ws)
    c.get_schemas()
    c.execute_query(app="Sales/Consumer Sales", object_id="obj-cy-kpi")
    assert headers
    assert all(h == {"Authorization": "Bearer test-only"} for h in headers)


def test_cloud_registry_config_matches_constructor():
    import inspect
    from app.schemas.data_source_registry import resolve_client_class
    from app.schemas.data_sources.configs import QlikSenseConfig
    assert resolve_client_class("qlik_sense") is QlikSenseClient
    assert set(QlikSenseConfig.model_fields) <= set(inspect.signature(QlikSenseClient).parameters)
    with pytest.raises(ValueError):
        QlikSenseConfig(base_url="https://qlik.example.test", max_concurrency=5)


def test_cloud_catalog_and_object_query_over_real_tls(tmp_path):
    """Exercise real HTTPS and WSS, with a synthetic Engine on loopback."""
    import asyncio
    import json
    import ssl
    import threading
    from concurrent.futures import Future
    from aiohttp import web
    from tests.unit.test_qlik_sense_onprem_client import CERT_PEM, KEY_PEM

    site = FakeQlikSite.demo()
    ready = Future()
    loop = asyncio.new_event_loop()
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.write_text(CERT_PEM)
    key.write_text(KEY_PEM)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    seen_headers = []

    async def handle(request):
        seen_headers.append(dict(request.headers))
        if request.path.startswith("/app/"):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            session = site.open_session(request.path, list(request.headers.items()))
            async for message in ws:
                await ws.send_json(session.handle_request(json.loads(message.data)))
            return ws
        if request.path.endswith("/users/me"):
            return web.json_response({"id": "synthetic-service"})
        if request.path.endswith("/items"):
            return web.json_response({"data": [
                {"resourceId": a["id"], "name": a["name"],
                 "spaceId": a["stream"]["id"], "spaceName": a["stream"]["name"]}
                for a in site.qrs_apps], "links": {}})
        raise web.HTTPNotFound()

    async def start():
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls)
        await server.start()
        ready.set_result((runner, server._server.sockets[0].getsockname()[1]))

    def serve():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(start())
        except BaseException as exc:
            ready.set_exception(exc)
            return
        loop.run_forever()
        loop.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    runner, port = ready.result(timeout=10)
    try:
        c = QlikSenseClient(base_url=f"https://127.0.0.1:{port}", api_key="synthetic-token", verify_ssl=False)
        assert c.test_connection()["success"]
        tables = c.get_schemas()
        assert any(t.columns and "/Sheets/" in t.name for t in tables)
        assert not c.execute_query(app="Sales/Consumer Sales", object_id="obj-cy-kpi").empty
        assert all(h.get("Authorization") == "Bearer synthetic-token" for h in seen_headers)
        assert all("X-Qlik-User" not in h for h in seen_headers)
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        assert not thread.is_alive()


def test_cloud_semantic_entries_reach_agent_context(monkeypatch):
    from app.ai.context.sections.tables_schema_section import TablesSchemaContext
    from app.schemas.data_source_schema import DataSourceSummarySchema
    c = cloud(FakeQlikSite.demo(), monkeypatch)
    context = TablesSchemaContext(data_sources=[TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="demo", name="Demo", type="qlik_sense"), tables=c.get_schemas(),
    )])
    rendered = context.render("full")
    assert 'objectType="sheet"' in rendered
    assert 'sheetDescription=' in rendered
    assert 'dtype="variable"' in rendered
    assert 'id=obj-rev-region' in rendered
