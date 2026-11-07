"""KMS Lighthouse wiring into the shared file-tool, context and credential code (P7).

Every shared change is gated by the connection type ``kms_lighthouse``; the
existing tests of the touched modules (OneNote membership, default user auth
modes, the file-scope render) show other connector types are unchanged.
"""
import importlib.util
import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai.context.sections.tables_schema_section import FileScopeItem, TablesSchemaContext
from app.ai.tools.implementations._file_tool_common import friendly_tool_error
from app.data_sources.clients import kms_lighthouse_client as kc
from app.schemas.data_source_schema import DataSourceSummarySchema

_MOCK_PATH = Path(__file__).resolve().parents[3] / "tools" / "kms_lighthouse" / "mock_kms_server.py"


def _load_mock():
    if "kms_lighthouse_mock" in sys.modules:
        return sys.modules["kms_lighthouse_mock"]
    spec = importlib.util.spec_from_file_location("kms_lighthouse_mock", _MOCK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["kms_lighthouse_mock"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def kms_server():
    mock = _load_mock()
    server = mock.make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield {"mock": mock, "base_url": f"http://{host}:{port}"}
    server.shutdown()
    server.server_close()


@pytest.fixture
def kms(kms_server):
    kc.clear_session_cache()
    kms_server["mock"].reset()
    yield kms_server
    kms_server["mock"].reset()
    kc.clear_session_cache()


def _userpass(kms, password=None, **kw):
    mock = kms["mock"]
    return kc.KmsLighthouseClient(base_url=kms["base_url"], allow_http=True, username="alice",
                                  password=password or mock.USERS["alice"]["password"], **kw)


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ set membership


def test_kms_lighthouse_is_in_every_file_source_set():
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
    from app.ai.tools.implementations._file_tool_common import FILE_SOURCE_TYPES
    from app.ai.tools.implementations.connection_catalog_common import (
        FILE_SOURCE_TYPES as CATALOG_FILE_TYPES,
    )

    assert "kms_lighthouse" in FILE_SOURCE_TYPES
    assert "kms_lighthouse" in CATALOG_FILE_TYPES
    assert "kms_lighthouse" in SchemaContextBuilder._FILE_SOURCE_TYPES
    assert "kms_lighthouse" in SchemaContextBuilder._NATIVE_SEARCH_TYPES
    # Not token-scoped: the descriptor's scope handling comes from the KMS branch instead.
    assert "kms_lighthouse" not in SchemaContextBuilder._TOKEN_SCOPED_TYPES


def test_default_user_auth_modes_leaves_kms_unset():
    from app.services.connection_service import default_user_auth_modes

    config = {"base_url": "https://kms.example.com"}
    assert default_user_auth_modes("kms_lighthouse", config, {"username": "u", "password": "p"}) is None
    assert default_user_auth_modes("kms_lighthouse", config, {"api_token": "dummy-token"}) is None


# ------------------------------------------------------------------ friendly_tool_error pass-through


def _list_with_tree_status(status):
    def produce(kms):
        kms["mock"].STATE["tree_status"] = {"10": status}
        _userpass(kms, root_folder_id=10).list_files()
    return produce


def _list_with_shape(shape):
    def produce(kms):
        kms["mock"].STATE["odd_shapes"] = shape
        _userpass(kms).list_files()
    return produce


def _wrong_password(kms):
    _userpass(kms, password="wrong-password-for-p7").list_files()


def _held_login(kms):
    with pytest.raises(kc.KmsHTTPError):
        _wrong_password(kms)
    _wrong_password(kms)          # the second refusal is the held one


def _stale_token(expired_response):
    def produce(kms):
        kms["mock"].STATE["expired_response"] = expired_response
        kc.KmsLighthouseClient(base_url=kms["base_url"], allow_http=True,
                               api_token="expired-token-p7-0123456789").list_files()
    return produce


def _unreachable(kms):
    kc.KmsLighthouseClient(base_url=f"http://127.0.0.1:{_closed_port()}", allow_http=True,
                           api_token="dummy-token-p7").list_files()


# A real client against the mock (or a closed port) wherever the kind is reachable that way.
_DRIVEN = {
    "auth_userpass": (_wrong_password, "auth", kc._USERPASS_AUTH),
    "auth_held": (_held_login, "auth", kc._HELD_AUTH),
    "auth_token": (_stale_token("401"), "auth", kc._TOKEN_AUTH),
    "auth_session_refused": (_list_with_tree_status(401), "auth", kc._SESSION_REFUSED),
    "redirect": (_stale_token("302"), "redirect", None),
    "forbidden": (_list_with_tree_status(403), "forbidden", None),
    "not_found": (_list_with_tree_status(404), "not_found", None),
    "throttled": (_list_with_tree_status(429), "throttled", None),
    "bad_request": (_list_with_tree_status(400), "bad_request", None),
    "server": (_list_with_tree_status(500), "server", None),
    "no_data": (_list_with_tree_status(200), "no_data", None),
    "rejected": (_list_with_shape("error"), "rejected", None),
    "unreachable": (_unreachable, "unreachable", None),
}


def _assert_passes_through(exc):
    assert friendly_tool_error("search_files", "KMS", exc) == f"search_files failed: {exc}"


@pytest.mark.parametrize("case", sorted(_DRIVEN))
def test_kms_errors_pass_through_friendly_tool_error(kms, case):
    produce, kind, message = _DRIVEN[case]
    with pytest.raises(kc.KmsHTTPError) as info:
        produce(kms)
    assert info.value.kind == kind
    if message is not None:
        assert str(info.value) == message
    _assert_passes_through(info.value)


# too_large needs a response over the 16 MiB cap; the fixed message is what a tool call would carry.
@pytest.mark.parametrize("kind", sorted(kc._MESSAGES))
def test_every_kms_error_kind_passes_through_friendly_tool_error(kind):
    _assert_passes_through(kc.KmsHTTPError(0, kind))


def test_driven_cases_cover_every_kind_but_too_large():
    assert {kind for _, kind, _ in _DRIVEN.values()} == set(kc._MESSAGES) - {"too_large"}


# ------------------------------------------------------------------ planner file-scope descriptor

_KMS_CONFIG = {"base_url": "https://kms.example.com", "root_folder_id": 10, "max_results": 50}


def _connection_xml(out: str, name: str) -> str:
    start = out.index(f'<connection name="{name}"')
    return out[start:out.index("</connection>", start)]


def _context():
    kms_scope = FileScopeItem(
        connection_id="k1", name="KMS", type="kms_lighthouse", base="https://kms.example.com / 10",
        index_mode="none", supports_search=True, enforces_scope=False,
        note=kc.planner_scope_note(_KMS_CONFIG),
    )
    files_scope = FileScopeItem(
        connection_id="n1", name="NetFiles", type="network_dir", base="/mnt/share", file_count=42,
        sample=["report.txt", "data.csv"], index_mode="content", supports_search=True,
    )
    ds = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="ds1", name="Support Agent", type="kms_lighthouse"),
        tables=[], file_scopes=[kms_scope, files_scope],
    )
    return TablesSchemaContext(data_sources=[ds])


@pytest.mark.parametrize("render", ["combined", "full"])
def test_kms_file_scope_descriptor_tells_the_planner_how_to_browse(render):
    ctx = _context()
    out = ctx.render_combined(top_k_per_ds=10, index_limit=200) if render == "combined" else ctx.render("full")
    kms_xml = _connection_xml(out, "KMS")
    assert 'kind="files"' in kms_xml
    assert "<note>" in kms_xml
    note = kms_xml[kms_xml.index("<note>"):kms_xml.index("</note>")]
    assert "folder_id" in note
    assert "(for example 'Folder/Subfolder')" in note and "Procedures" not in note
    assert "paths start below the configured root folder when one is set" in note
    assert f"at most {_KMS_CONFIG['max_results']} articles and scenarios (folders do not count)" in note
    assert f"after {kc._MAX_FOLDER_CALLS} folder lookups" in note
    assert "no catalog" in note
    assert "always 0" in note
    assert "browsing only" in note
    assert kms_xml.index("<usage>") < kms_xml.index("<note>")
    assert "anything else is denied" not in kms_xml
    assert "<scope>" not in kms_xml
    files_xml = _connection_xml(out, "NetFiles")
    assert "<note>" not in files_xml
    assert "anything else is denied" in files_xml


def test_kms_file_scope_is_built_with_the_note_and_no_enforced_scope():
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder

    conns = [
        SimpleNamespace(id="k1", name="KMS", type="kms_lighthouse", config=dict(_KMS_CONFIG, index_mode="none"),
                        auth_policy="user_required", allowed_user_auth_modes=["userpass"]),
        SimpleNamespace(id="n1", name="NetFiles", type="network_dir", config={"root_path": "/mnt/share"},
                        auth_policy="system_only", allowed_user_auth_modes=None),
    ]
    builder = SchemaContextBuilder.__new__(SchemaContextBuilder)
    scopes, remaining = builder._build_file_scopes(SimpleNamespace(connections=conns), [])
    by_name = {s.name: s for s in scopes}
    kms_scope, files_scope = by_name["KMS"], by_name["NetFiles"]
    assert remaining == []
    assert kms_scope.base == "https://kms.example.com / 10"
    assert kms_scope.enforces_scope is False
    assert kms_scope.supports_search is True
    assert kms_scope.note == kc.planner_scope_note(_KMS_CONFIG)
    assert files_scope.enforces_scope is True
    assert files_scope.note is None
    assert files_scope.base == "/mnt/share"


# ConnectForm stores a blank optional number as "", and create_connection does not
# validate config, so the note must read each value exactly as the client does.
_BLANKS = [("missing", {}), ("empty", {"root_folder_id": "", "layout_id": ""}),
           ("none", {"root_folder_id": None, "layout_id": None}),
           ("zero", {"root_folder_id": 0, "layout_id": 0}),
           ("ten", {"root_folder_id": 10, "layout_id": 10})]


@pytest.mark.parametrize("cfg", [c for _, c in _BLANKS], ids=[n for n, _ in _BLANKS])
def test_planner_note_reads_root_and_layout_as_the_client_does(cfg):
    client = kc.KmsLighthouseClient(base_url="https://kms.example.com", username="alice", password="pw", **cfg)
    note = kc.planner_scope_note(dict(cfg, base_url="https://kms.example.com"))
    if client.root_folder_id is None:
        assert "from the top level" in note
        assert "(id " not in note
    else:
        assert f"from the root folder (id {client.root_folder_id})" in note
    if client.layout_id is None:
        assert " in layout" not in note
    else:
        assert f" in layout {client.layout_id}," in note
    assert "(id )" not in note
    assert "layout ," not in note


@pytest.mark.parametrize("root,base", [(0, "https://kms.example.com / 0"), (10, "https://kms.example.com / 10"),
                                       ("", "https://kms.example.com"), (None, "https://kms.example.com")])
def test_kms_file_scope_base_keeps_root_folder_zero(root, base):
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder

    conn = SimpleNamespace(id="k1", name="KMS", type="kms_lighthouse",
                           config={"base_url": "https://kms.example.com", "root_folder_id": root, "index_mode": "none"},
                           auth_policy="user_required", allowed_user_auth_modes=["userpass"])
    builder = SchemaContextBuilder.__new__(SchemaContextBuilder)
    scopes, _ = builder._build_file_scopes(SimpleNamespace(connections=[conn]), [])
    assert scopes[0].base == base


# ------------------------------------------------------------------ member credential save


def _member_save(conn_type, auth_mode, credentials):
    from app.services.connection_service import ConnectionService

    connection = SimpleNamespace(type=conn_type, auth_policy="user_required", allowed_user_auth_modes=None)
    payload = SimpleNamespace(auth_mode=auth_mode, credentials=credentials)
    # Validation fails before any database access, so no db or user is needed.
    return ConnectionService().save_windows_user_credentials(None, connection, None, payload)


@pytest.mark.asyncio
async def test_member_save_shows_the_kms_token_format_message():
    from app.errors import AppError
    from app.schemas.data_sources.configs import KMS_TOKEN_FORMAT_MESSAGE

    with pytest.raises(AppError) as info:
        await _member_save("kms_lighthouse", "token", {"api_token": "dummy tok3n"})
    assert info.value.status_code == 422
    assert KMS_TOKEN_FORMAT_MESSAGE in info.value.message
    assert "dummy tok3n" not in info.value.message


@pytest.mark.asyncio
async def test_member_save_keeps_the_generic_message_for_other_types():
    from app.errors import AppError

    with pytest.raises(AppError) as info:
        await _member_save("documentum", "userpass", {"username": "alice"})
    assert info.value.status_code == 422
    assert info.value.message == "The required credential fields are missing or invalid"


# ------------------------------------------------------------------ tool schema


def test_list_files_folder_id_help_mentions_kms_paths():
    from app.ai.tools.schemas.file_tools import ListFilesInput

    help_text = ListFilesInput.model_json_schema()["properties"]["folder_id"]["description"]
    assert "KMS Lighthouse" in help_text
    assert "(for example 'Folder/Subfolder')" in help_text and "Procedures" not in help_text
    assert "paths start below the connection's root folder when one is set" in help_text


# ------------------------------------------------------------------ list_files without a client

_CLIENT_ERR = "Failed to construct client: 403: User credentials required for this connection"
_EGRESS_ERR = "Failed to construct client: Outbound access to that address is not allowed."


def _list_files_without_a_client(conn_type, client_err=_CLIENT_ERR, **tool_input):
    """ListFilesTool when the connection's client could not be built (no catalog rows either)."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.ai.tools.implementations.list_files import ListFilesTool

    conn = SimpleNamespace(id="CONN-K", type=conn_type, name="KMS", is_active=True)
    ds = SimpleNamespace(id="DS-K", name="Support Agent", is_active=True, connections=[conn])
    ctx = {"report": SimpleNamespace(data_sources=[ds])}

    async def run():
        with patch("app.ai.tools.implementations.list_files.resolve_file_client",
                   new=AsyncMock(return_value=(None, client_err))), \
             patch("app.ai.tools.implementations.list_files.resolve_file_data_source",
                   new=AsyncMock(return_value=(ds, None))), \
             patch("app.services.data_source_service.DataSourceService.get_data_source_schema",
                   new=AsyncMock(return_value=[])):
            return [e async for e in ListFilesTool().run_stream({"connection_id": "CONN-K", **tool_input}, ctx)]

    return asyncio.run(run())[-1].payload


def test_list_files_reports_a_kms_client_error_instead_of_an_empty_catalog():
    # A missing sign-in reads as the call to Connect that the other file tools give.
    payload = _list_files_without_a_client("kms_lighthouse")
    out = payload["output"]
    assert out["success"] is False, out
    assert "tell the user to Connect 'KMS' from the agent selector" in out["error"], out
    assert "Catalog is empty" not in str(payload)
    # Any other client failure keeps its own text.
    payload = _list_files_without_a_client("kms_lighthouse", _EGRESS_ERR)
    assert payload["output"]["success"] is False
    assert _EGRESS_ERR in payload["output"]["error"] and "Connect" not in payload["output"]["error"]
    assert "Catalog is empty" not in str(payload)


def test_list_files_without_a_client_is_unchanged_for_other_types():
    payload = _list_files_without_a_client("onedrive")
    assert payload["output"]["success"] is True
    assert "Catalog is empty" in payload["observation"]["summary"]


@pytest.mark.parametrize("tool_input", [{"source": "catalog"}, {"cursor": "abc"}, {"persist": True}],
                         ids=["catalog", "cursor", "persist"])
def test_list_files_kms_client_error_comes_before_catalog_cursor_and_persist_checks(tool_input):
    # Without a client there is no catalog either: the client's own error is the answer, never "...: None.".
    out = _list_files_without_a_client("kms_lighthouse", **tool_input)["output"]
    assert out["success"] is False, out
    assert "tell the user to Connect 'KMS' from the agent selector" in out["error"], out
    assert "None." not in out["error"] and "cursor" not in out["error"], out


# ------------------------------------------------------------------ an inactive connection

_INACTIVE_TEXT = ("is inactive after failed connection tests", "ask an admin to test")


def _inactive_ctx(conn_type, *, conn_active=False, ds_active=True, name="KMS"):
    """A run scope whose only file connection (or its data source) was turned off by failed tests.

    The real resolvers run: the allow-list drops the inactive connection, so resolve_file_client
    answers with its selection error and the catalog resolver still finds the data source."""
    conn = SimpleNamespace(id="CONN-K", type=conn_type, name=name, is_active=conn_active,
                           auth_policy="system_only")
    ds = SimpleNamespace(id="DS-K", name="Support Agent", is_active=ds_active, connections=[conn])
    return {"db": object(), "organization": SimpleNamespace(id="ORG"),
            "report": SimpleNamespace(data_sources=[ds])}


def _run_tool(tool, tool_input, ctx):
    import asyncio
    from unittest.mock import AsyncMock, patch

    async def run():
        with patch("app.services.data_source_service.DataSourceService.get_data_source_schema",
                   new=AsyncMock(return_value=[])):
            return [e async for e in tool.run_stream(tool_input, ctx)]

    return asyncio.run(run())[-1].payload


_AGENT_OFF_TEXT = ("The agent that holds the KMS Lighthouse connection 'KMS' was turned off because one of its "
                   "connections failed its tests. Do NOT retry: tell the user to ask an admin to test that "
                   "agent's connections, then continue with the remaining sources.")


@pytest.mark.parametrize("selection", ["CONN-K", "DS-K", "support agent", "KMS", "no-such-id"])
@pytest.mark.parametrize("flags", [{"conn_active": False}, {"conn_active": True, "ds_active": False},
                                   {"conn_active": False, "ds_active": False}],
                         ids=["connection-off", "agent-off", "both-off"])
def test_list_files_says_an_inactive_kms_connection_is_inactive(selection, flags):
    from app.ai.tools.implementations.list_files import ListFilesTool

    payload = _run_tool(ListFilesTool(), {"connection_id": selection}, _inactive_ctx("kms_lighthouse", **flags))
    out = payload["output"]
    assert out["success"] is False, out
    if flags["conn_active"]:
        # Only the agent is off: the connection itself is not blamed.
        assert out["error"] == _AGENT_OFF_TEXT, out
        assert "after failed connection tests" not in out["error"], out
    else:
        assert all(t in out["error"] for t in _INACTIVE_TEXT), out
        assert out["error"] == kc.INACTIVE_CONNECTION_MESSAGE.format(name="KMS")
    assert "Catalog is empty" not in str(payload) and "still connected" not in out["error"]


@pytest.mark.parametrize("name", ["", None], ids=["empty", "none"])
def test_inactive_text_names_this_source_when_the_connection_has_no_name(name):
    from app.ai.tools.implementations.list_files import ListFilesTool

    out = _run_tool(ListFilesTool(), {"connection_id": "CONN-K"}, _inactive_ctx("kms_lighthouse", name=name))["output"]
    assert out["success"] is False, out
    assert out["error"] == ("The KMS Lighthouse connection 'this source' is inactive after failed connection "
                            "tests. Do NOT retry: tell the user to ask an admin to test the connection, then "
                            "continue with the remaining sources."), out


def test_list_files_inactive_connection_is_unchanged_for_other_types():
    from app.ai.tools.implementations.list_files import ListFilesTool

    payload = _run_tool(ListFilesTool(), {"connection_id": "CONN-K"}, _inactive_ctx("onedrive"))
    assert payload["output"]["success"] is True, payload["output"]
    assert "Catalog is empty" in payload["observation"]["summary"]


def test_search_and_read_say_an_inactive_kms_connection_is_inactive():
    from app.ai.tools.implementations.read_file import ReadFileTool
    from app.ai.tools.implementations.search_files import SearchFilesTool

    for tool, tool_input in ((SearchFilesTool(), {"connection_id": "CONN-K", "query": "billing"}),
                             (ReadFileTool(), {"connection_id": "CONN-K", "file_id": "item:301"})):
        out = _run_tool(tool, tool_input, _inactive_ctx("kms_lighthouse"))["output"]
        assert out["success"] is False, out
        assert all(t in out["error"] for t in _INACTIVE_TEXT), out
        assert out["error"] == kc.INACTIVE_CONNECTION_MESSAGE.format(name="KMS"), out
        # Another type keeps the resolver's own selection error.
        other = _run_tool(tool, tool_input, _inactive_ctx("onedrive"))["output"]
        assert other["success"] is False and other["error"].startswith("Invalid file-source selection"), other


# ------------------------------------------------------------------ the connection the resolver picked
# Multi-connection scopes where an inactive entry matches the selection before the active one the
# shared resolver (resolve_file_client) actually uses. The real resolver runs; only the client build
# fails, so the text must follow the connection the resolver picked.

_OUTAGE = kc._MESSAGES["unreachable"]
_CREDENTIALS = "403: User credentials required for this connection"


def _conn(cid, conn_type, name, active=True):
    return SimpleNamespace(id=cid, type=conn_type, name=name, is_active=active, auth_policy="system_only")


def _agent(did, name, conns, active=True):
    return SimpleNamespace(id=did, name=name, is_active=active, connections=conns)


class _NoRowsDb:
    """A db session whose every query finds nothing (search_files' index lookup on a multi-connection agent)."""

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: None)


def _scope(*agents):
    return {"db": _NoRowsDb(), "organization": SimpleNamespace(id="ORG"),
            "report": SimpleNamespace(data_sources=list(agents))}


def _with_build_error(tool, tool_input, ctx, build_error):
    """Run `tool` through the real resolver; the selected connection's client build raises build_error.

    Returns the tool output and the connections the resolver tried to build."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    built = []

    async def construct(_self, _db, conn, _user):
        built.append(conn)
        raise Exception(build_error)

    async def run():
        with patch("app.services.connection_service.ConnectionService.construct_client", new=construct), \
             patch("app.services.data_source_service.DataSourceService.get_data_source_schema",
                   new=AsyncMock(return_value=[])):
            return [e async for e in tool.run_stream(tool_input, ctx)]

    return asyncio.run(run())[-1].payload, built


def _three_tools(selection):
    from app.ai.tools.implementations.list_files import ListFilesTool
    from app.ai.tools.implementations.read_file import ReadFileTool
    from app.ai.tools.implementations.search_files import SearchFilesTool

    return [("list_files", ListFilesTool(), {"connection_id": selection}),
            ("search_files", SearchFilesTool(), {"connection_id": selection, "query": "billing"}),
            ("read_file", ReadFileTool(), {"connection_id": selection, "file_id": "item:301"})]


def test_p1_an_inactive_kms_entry_does_not_relabel_the_active_onedrive_error():
    # P1: one agent [KMS inactive, OneDrive active], selected by agent id: the resolver uses OneDrive.
    onedrive = _conn("CONN-O", "onedrive", "Drive")
    agent = _agent("DS-1", "Support Agent", [_conn("CONN-K", "kms_lighthouse", "KMS", active=False), onedrive])
    for op, tool, tool_input in _three_tools("DS-1"):
        payload, built = _with_build_error(tool, tool_input, _scope(agent), "boom")
        out = payload["output"]
        assert built and all(c is onedrive for c in built), (op, built)
        assert "inactive" not in str(payload) and "KMS" not in str(out.get("error")), (op, out)
        if op == "list_files":
            # OneDrive without a client falls through to the cache path, exactly as before.
            assert out["success"] is True and "Catalog is empty" in payload["observation"]["summary"], (op, out)
        else:
            assert out["success"] is False and out["error"] == "Failed to construct client: boom", (op, out)


def test_p2_an_inactive_onedrive_entry_does_not_hide_the_kms_connect_prompt():
    # P2: one agent [OneDrive inactive, KMS active], selected by agent id: the resolver uses KMS.
    kms_conn = _conn("CONN-K", "kms_lighthouse", "KMS")
    agent = _agent("DS-1", "Support Agent", [_conn("CONN-O", "onedrive", "Drive", active=False), kms_conn])
    for op, tool, tool_input in _three_tools("DS-1"):
        payload, built = _with_build_error(tool, tool_input, _scope(agent), _CREDENTIALS)
        out = payload["output"]
        assert built and all(c is kms_conn for c in built), (op, built)
        assert out["success"] is False, (op, out)
        assert "tell the user to Connect 'KMS' from the agent selector" in out["error"], (op, out)
        assert "inactive" not in out["error"], (op, out)


def test_p3_a_kms_connection_shared_with_an_inactive_agent_gets_the_connect_prompt():
    # P3: one KMS connection on agent A (inactive) and agent B (active), selected by connection id.
    shared = _conn("CONN-K", "kms_lighthouse", "KMS")
    scope = _scope(_agent("DS-A", "Old Agent", [shared], active=False), _agent("DS-B", "Support Agent", [shared]))
    for op, tool, tool_input in _three_tools("CONN-K"):
        payload, built = _with_build_error(tool, tool_input, scope, _CREDENTIALS)
        out = payload["output"]
        assert built and all(c is shared for c in built), (op, built)
        assert out["success"] is False, (op, out)
        assert "tell the user to Connect 'KMS' from the agent selector" in out["error"], (op, out)
        assert "inactive" not in out["error"] and "turned off" not in out["error"], (op, out)


def test_p5_two_kms_connections_named_alike_report_the_active_ones_outage():
    # P5: two KMS connections both named "KMS", the first inactive, selected by name.
    active = _conn("CONN-K2", "kms_lighthouse", "KMS")
    agent = _agent("DS-1", "Support Agent", [_conn("CONN-K1", "kms_lighthouse", "KMS", active=False), active])
    for op, tool, tool_input in _three_tools("KMS"):
        payload, built = _with_build_error(tool, tool_input, _scope(agent), _OUTAGE)
        out = payload["output"]
        assert built and all(c is active for c in built), (op, built)
        assert out["success"] is False, (op, out)
        assert out["error"] == f"{op} failed: Failed to construct client: KMS Lighthouse could not be reached.", \
            (op, out)


# ------------------------------------------------------------------ search_files / read_file without a client

def _without_a_client(module, tool, tool_input, conn_type, client_err=_CLIENT_ERR):
    """`tool` when the connection's client could not be built."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    conn = SimpleNamespace(id="CONN-K", type=conn_type, name="KMS", is_active=True)
    ds = SimpleNamespace(id="DS-K", name="Support Agent", is_active=True, connections=[conn])
    ctx = {"report": SimpleNamespace(data_sources=[ds])}

    async def run():
        with patch(f"app.ai.tools.implementations.{module}.resolve_file_client",
                   new=AsyncMock(return_value=(None, client_err))):
            return [e async for e in tool.run_stream(tool_input, ctx)]

    return asyncio.run(run())[-1].payload["output"]


def _search_and_read_inputs():
    from app.ai.tools.implementations.read_file import ReadFileTool
    from app.ai.tools.implementations.search_files import SearchFilesTool

    return [
        ("search_files", SearchFilesTool(), {"connection_id": "CONN-K", "query": "billing"}),
        ("read_file", ReadFileTool(), {"connection_id": "CONN-K", "file_id": "item:301"}),
        # No connection named: read_file falls back to the agent's only file connection.
        ("read_file", ReadFileTool(), {"file_id": "item:301"}),
    ]


def test_search_and_read_tell_a_member_without_credentials_to_connect():
    for module, tool, tool_input in _search_and_read_inputs():
        out = _without_a_client(module, tool, tool_input, "kms_lighthouse")
        assert out["success"] is False, (module, out)
        assert "tell the user to Connect 'KMS' from the agent selector" in out["error"], (module, out)
        assert "User credentials required" not in out["error"], (module, out)
        # Any other client failure keeps its own text.
        out = _without_a_client(module, tool, tool_input, "kms_lighthouse", _EGRESS_ERR)
        assert out["success"] is False and _EGRESS_ERR in out["error"], (module, out)
        assert "Connect" not in out["error"], (module, out)


def test_search_and_read_client_errors_are_unchanged_for_other_types():
    for module, tool, tool_input in _search_and_read_inputs():
        out = _without_a_client(module, tool, tool_input, "onedrive")
        assert out["success"] is False and out["error"] == _CLIENT_ERR, (module, out)
