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
    assert "Procedures/Billing" in note
    assert "50" in note
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


# ------------------------------------------------------------------ tool schema


def test_list_files_folder_id_help_mentions_kms_paths():
    from app.ai.tools.schemas.file_tools import ListFilesInput

    help_text = ListFilesInput.model_json_schema()["properties"]["folder_id"]["description"]
    assert "KMS Lighthouse" in help_text
    assert "Procedures/Billing" in help_text
