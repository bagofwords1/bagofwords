"""KMS Lighthouse connector contract against the in-process mock KMS API.

The mock (tools/kms_lighthouse/mock_kms_server.py) is the remote boundary: a
real HTTP server on a real socket speaking the KMS Lighthouse public API
shapes, with per-user visibility, cookie and/or token sessions and pasted API
tokens. Everything on our side (client, schemas, registry) runs for real.
"""
import importlib
import importlib.util
import sys
import threading
from pathlib import Path

import uuid

import pydantic
import pytest

from app.data_sources.clients import kms_lighthouse_client as kc
from app.schemas.data_source_registry import REGISTRY, is_overlay_auth

_MOCK_PATH = Path(__file__).resolve().parents[3] / "tools" / "kms_lighthouse" / "mock_kms_server.py"


def _load_mock():
    if "kms_lighthouse_mock" in sys.modules:
        return sys.modules["kms_lighthouse_mock"]
    spec = importlib.util.spec_from_file_location("kms_lighthouse_mock", _MOCK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["kms_lighthouse_mock"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def kms_server():
    mock = _load_mock()
    server = mock.make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield {"mock": mock, "base_url": f"http://{host}:{port}", "port": port}
    server.shutdown()
    server.server_close()


@pytest.fixture
def kms(kms_server):
    kms_server["mock"].reset()
    yield kms_server
    kms_server["mock"].reset()


def client(kms, user="alice", **kw):
    """A userpass client for a mock user (pass password=... to override)."""
    mock = kms["mock"]
    kw.setdefault("password", mock.USERS[user]["password"] if user in mock.USERS else "not-a-user-pass")
    kw.setdefault("base_url", kms["base_url"])
    kw.setdefault("allow_http", True)
    return kc.KmsLighthouseClient(username=user, **kw)


def token_client(kms, user="alice", **kw):
    """A pasted-token client for a mock user."""
    mock = kms["mock"]
    token = next(t for t, u in mock.TOKENS.items() if u == user)
    kw.setdefault("api_token", token)
    kw.setdefault("base_url", kms["base_url"])
    kw.setdefault("allow_http", True)
    return kc.KmsLighthouseClient(**kw)


def _rows(kms, path_suffix):
    mock = kms["mock"]
    return [row for row in mock.REQUEST_LOG if row[2] == mock.PREFIX + path_suffix]


def test_registry_contract():
    entry = REGISTRY["kms_lighthouse"]
    assert entry.type == "kms_lighthouse"
    assert entry.category == "files" and entry.data_shape == "files"
    assert entry.catalog_ownership == "none"
    assert entry.catalog_nouns == ("document", "documents")
    assert entry.requires_license == "enterprise"
    variants = entry.credentials_auth.by_auth
    assert set(variants) == {"userpass", "token"}
    assert entry.credentials_auth.default == "userpass"
    for name, variant in variants.items():
        assert variant.scopes == ["system", "user"]
        assert not is_overlay_auth("kms_lighthouse", name)
    module_path, cls_name = entry.client_path.rsplit(".", 1)
    assert getattr(importlib.import_module(module_path), cls_name).__name__ == "KmsLighthouseClient"

    valid = {"userpass": {"username": "alice", "password": "pw"}, "token": {"api_token": "tok"}}
    for name, variant in variants.items():
        for field in valid[name]:
            with pytest.raises(pydantic.ValidationError):
                variant.schema(**{**valid[name], field: ""})
        dumped = variant.schema(**valid[name], base_url="http://169.254.169.254", allow_http=True).model_dump()
        assert dumped == valid[name]


_SECRET_PW = "s3cret-" + uuid.uuid4().hex


@pytest.mark.parametrize("case", [
    "no_credentials", "username_without_password", "http_without_allow_http", "userinfo",
    "query", "fragment", "prefix_with_dotdot", "prefix_with_bad_chars", "base_path_not_prefix",
])
def test_invalid_configuration_is_rejected(kms, case):
    base, port = kms["base_url"], kms["port"]
    kwargs = {"base_url": base, "allow_http": True, "username": "alice", "password": "alice-kms-pass"}
    if case == "no_credentials":
        kwargs.pop("username"), kwargs.pop("password")
    elif case == "username_without_password":
        kwargs["password"] = ""
    elif case == "http_without_allow_http":
        kwargs["allow_http"] = False
    elif case == "userinfo":
        kwargs["base_url"] = f"http://alice:{_SECRET_PW}@127.0.0.1:{port}"
    elif case == "query":
        kwargs["base_url"] = base + "/?tenant=1"
    elif case == "fragment":
        kwargs["base_url"] = base + "/#top"
    elif case == "prefix_with_dotdot":
        kwargs["api_prefix"] = "/kms/../api"
    elif case == "prefix_with_bad_chars":
        kwargs["api_prefix"] = "/kms/lh api?x=1"
    elif case == "base_path_not_prefix":
        kwargs["base_url"] = base + "/somewhere/else"
    with pytest.raises(kc.KmsError) as info:
        kc.KmsLighthouseClient(**kwargs)
    assert _SECRET_PW not in str(info.value)
    assert kms["mock"].REQUEST_LOG == []


@pytest.mark.parametrize("suffix,api_prefix,expected_prefix", [
    ("/kms/lh/api", None, "/kms/lh/api"),
    ("/kms/lh/api/", None, "/kms/lh/api"),
    ("/", None, "/kms/lh/api"),
    ("/custom/api", "/custom/api", "/custom/api"),
])
def test_base_url_with_prefix_is_normalised(kms, suffix, api_prefix, expected_prefix):
    extra = {"api_prefix": api_prefix} if api_prefix else {}
    c = client(kms, base_url=kms["base_url"] + suffix, **extra)
    result = c.test_connection()
    paths = [row[2] for row in kms["mock"].REQUEST_LOG]
    assert paths, "the client made no request"
    assert all(p.startswith(expected_prefix + "/") for p in paths)
    assert not any(p.startswith(expected_prefix + expected_prefix) for p in paths)
    # The mock serves the API at the default prefix only.
    assert result["success"] is (expected_prefix == kms["mock"].PREFIX)


@pytest.mark.parametrize("login_mode", ["cookie", "token", "both"])
def test_login_establishes_session(kms, login_mode):
    kms["mock"].STATE["login_mode"] = login_mode
    result = client(kms).test_connection()
    assert result["success"] is True, result
    assert kms["mock"].STATE["accepted_logins"] == 1
    tree = _rows(kms, "/cm/tree/branch/view")
    assert tree and {row[0] for row in tree} == {"alice"}


def test_token_variant_never_logs_in(kms):
    result = token_client(kms).test_connection()
    assert result["success"] is True, result
    assert kms["mock"].STATE["login_count"] == 0
    assert _rows(kms, "/login") == []
    assert {row[0] for row in _rows(kms, "/cm/tree/branch/view")} == {"alice"}


@pytest.mark.parametrize("case,expected_method,expected_logins", [
    ("userpass_with_leftover_token", "userpass", 1),
    ("token_with_leftover_userpass", "token", 0),
    ("userpass_named_but_only_a_token_row", "token", 0),
    ("no_auth_type_with_userpass", "userpass", 1),
    ("no_auth_type_with_token", "token", 0),
])
def test_auth_variant_follows_auth_type_when_its_fields_are_present(kms, case, expected_method, expected_logins):
    mock = kms["mock"]
    alice = {"username": "alice", "password": mock.USERS["alice"]["password"]}
    bob = {"username": "bob", "password": mock.USERS["bob"]["password"]}
    kwargs = {
        "userpass_with_leftover_token": {"auth_type": "userpass", **alice, "api_token": "bob-api-token-1"},
        "token_with_leftover_userpass": {"auth_type": "token", **bob, "api_token": "alice-api-token-1"},
        "userpass_named_but_only_a_token_row": {"auth_type": "userpass", "api_token": "alice-api-token-1"},
        "no_auth_type_with_userpass": dict(alice),
        "no_auth_type_with_token": {"api_token": "alice-api-token-1"},
    }[case]
    c = kc.KmsLighthouseClient(base_url=kms["base_url"], allow_http=True, **kwargs)
    assert c.auth_method == expected_method
    result = c.test_connection()
    assert result["success"] is True, result
    assert mock.STATE["login_count"] == expected_logins
    assert {row[0] for row in _rows(kms, "/cm/tree/branch/view")} == {"alice"}


def test_failed_login_message_has_no_secret(kms):
    wrong = "Wrong-" + uuid.uuid4().hex
    result = client(kms, password=wrong).test_connection()
    assert result["success"] is False
    message = result["message"]
    assert message
    for leaked in (wrong, "password=", "/login", str(kms["port"])):
        assert leaked not in message
    assert kms["mock"].STATE["login_count"] == 1
    assert _rows(kms, "/cm/tree/branch/view") == []


@pytest.mark.parametrize("make,user,method", [
    (client, "alice", "userpass"), (client, "bob", "userpass"),
    (token_client, "alice", "token"), (token_client, "bob", "token"),
])
def test_test_connection_reports_identity(kms, make, user, method):
    result = make(kms, user=user).test_connection()
    assert result["success"] is True, result
    details = result["details"]
    assert details["identity"] == user
    assert details["auth_method"] == method
    assert details["host"] == f"127.0.0.1:{kms['port']}"
    assert details["root_folder_id"] is None
    assert user in result["message"]


def test_execute_query_is_unavailable_until_read_exists(kms):
    c = client(kms)
    with pytest.raises(kc.KmsError) as info:
        c.execute_query('{"file_id": "item:279"}')
    assert "item:279" not in str(info.value)
    assert kms["mock"].REQUEST_LOG == []
    assert c.get_schemas() == [] and c.get_schema("anything") is None and c.prompt_schema() == ""
    events = []
    assert c.get_schemas(progress_callback=lambda *e: events.append(e)) == []
    assert events and events[-1][0] == "catalog_ready"
    assert kms["mock"].REQUEST_LOG == []


@pytest.mark.parametrize("root", [None, 4711])
@pytest.mark.parametrize("max_results", [50, None])
@pytest.mark.parametrize("layout", [None, 7])
def test_planner_scope_note_states_handle_cap_and_scope(root, max_results, layout):
    config = {"base_url": "https://kms.example.com"}
    if root is not None:
        config["root_folder_id"] = root
    if max_results is not None:
        config["max_results"] = max_results
    if layout is not None:
        config["layout_id"] = layout
    note = kc.planner_scope_note(config)
    assert "folder_id" in note
    assert "Procedures/Billing" in note
    assert str(max_results or 200) in note
    assert "browsing only" in note
    assert ("4711" in note) is (root is not None)
    assert ("layout 7" in note) is (layout is not None)


@pytest.mark.parametrize("shape", ["list", "error", "bad_id"])
@pytest.mark.parametrize("make", [client, token_client])
def test_unexpected_response_shapes_never_echo_provider_text(kms, shape, make):
    mock = kms["mock"]
    mock.STATE["odd_shapes"] = shape
    result = make(kms).test_connection()
    assert result["success"] is False
    assert result["message"]
    assert mock.ODD_MARKER not in repr(result)
    assert _rows(kms, "/cm/tree/branch/view"), "the tree call was not exercised"


def test_top_level_rejection_hints_root_folder(kms):
    kms["mock"].STATE["top_level_requires_parent"] = True
    result = client(kms).test_connection()
    assert result["success"] is False
    assert "Root folder ID" in result["message"]
    # Folder 95 exists and is empty: with a root set, an empty folder is a pass.
    rooted = client(kms, root_folder_id=95).test_connection()
    assert rooted["success"] is True, rooted
    assert rooted["details"]["root_folder_id"] == 95
