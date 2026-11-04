"""KMS Lighthouse connector contract against the in-process mock KMS API.

The mock (tools/kms_lighthouse/mock_kms_server.py) is the remote boundary: a
real HTTP server on a real socket speaking the KMS Lighthouse public API
shapes, with per-user visibility, cookie and/or token sessions and pasted API
tokens. Everything on our side (client, schemas, registry) runs for real.
"""
import importlib
import importlib.util
import json
import logging
import socket
import sys
import threading
import traceback
import uuid
from pathlib import Path

import pydantic
import pytest
import requests

from app.ai.tools.implementations._file_tool_common import _AUTH_ERROR_MARKERS
from app.data_sources.clients import kms_lighthouse_client as kc
from app.data_sources.clients._file_source_common import FileTooLargeError, GlobScopeError
from app.data_sources.clients.base import SearchStopped
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
    # The client's process-level session pool (ADR-6) would otherwise carry
    # sessions and login holds from one test into the next.
    kc.clear_session_cache()
    kms_server["mock"].reset()
    yield kms_server
    kms_server["mock"].reset()
    kc.clear_session_cache()


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


def _no_marker(text):
    low = text.lower()
    return [m for m in _AUTH_ERROR_MARKERS if m in low]


@pytest.fixture
def raw_kms():
    """A real socket that answers every request with one canned (possibly broken) HTTP response.

    Some transport failures (a cut-off body, a bad gzip stream, duplicate
    cookies) cannot come out of http.server, so this boundary writes the bytes.
    """
    kc.clear_session_cache()   # a reused port must not inherit another test's pooled session
    state = {"response": b"", "requests": []}
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.1)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                data = b""
                try:
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    state["requests"].append(data.decode("latin-1"))
                    conn.sendall(state["response"])
                except OSError:
                    pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield {"base_url": f"http://127.0.0.1:{listener.getsockname()[1]}", "state": state}
    stop.set()
    thread.join(2)
    listener.close()


def _http_200(body: bytes, *headers: str, length: int | None = None) -> bytes:
    head = ["HTTP/1.1 200 OK", "Content-Type: application/json",
            f"Content-Length: {len(body) if length is None else length}", "Connection: close", *headers]
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body


_RAW_MARKER = "raw-marker-" + uuid.uuid4().hex
_RAW_BODIES = {
    # Content-Length promises more than arrives before the socket closes.
    "truncated_body": lambda: _http_200(b'{"status": "SUCCESS", "data": ', length=4096),
    "bad_gzip": lambda: _http_200(b"this is not a gzip stream " + _RAW_MARKER.encode(), "Content-Encoding: gzip"),
}


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
    assert getattr(importlib.import_module(module_path), cls_name) is kc.KmsLighthouseClient

    valid = {"userpass": {"username": "alice", "password": "pw"}, "token": {"api_token": "tok"}}
    for name, variant in variants.items():
        for field in valid[name]:
            for blank in ("", "   "):
                with pytest.raises(pydantic.ValidationError):
                    variant.schema(**{**valid[name], field: blank})
        dumped = variant.schema(**valid[name], base_url="http://169.254.169.254", allow_http=True).model_dump()
        assert dumped == valid[name]


def test_pasted_token_is_trimmed_and_must_be_plain_text():
    schema = REGISTRY["kms_lighthouse"].credentials_auth.by_auth["token"].schema
    # A copied token often carries a trailing newline or spaces: they are trimmed.
    assert schema(api_token="  alice-api-token-1\n").model_dump() == {"api_token": "alice-api-token-1"}
    for bad in ("two words", "tok​zero-width", "“smart-quoted”", "tab\tinside", "café"):
        with pytest.raises(pydantic.ValidationError):
            schema(api_token=bad)


def test_pasted_token_is_checked_before_any_request(kms):
    # Trimmed, the pasted token works.
    assert token_client(kms, api_token="alice-api-token-1\n").test_connection()["success"] is True
    kms["mock"].reset()
    for bad in ("alice-api-token-1​", "“alice-api-token-1”", "alice api-token-1"):
        with pytest.raises(kc.KmsError) as info:
            token_client(kms, api_token=bad)
        assert bad not in str(info.value) and not _no_marker(str(info.value))
        assert info.value.__context__ is None and info.value.__cause__ is None
    assert kms["mock"].REQUEST_LOG == []


_SECRET_PW = "s3cret-" + uuid.uuid4().hex


@pytest.mark.parametrize("case", [
    "no_credentials", "username_without_password", "http_without_allow_http", "userinfo",
    "query", "fragment", "prefix_with_dotdot", "prefix_with_bad_chars", "base_path_not_prefix",
    "whitespace_username", "whitespace_password", "allow_http_as_text_false", "empty_userinfo",
    "colon_only_userinfo", "port_out_of_range", "port_not_a_number", "space_in_host", "unclosed_ipv6",
])
def test_invalid_configuration_is_rejected(kms, case):
    base, port = kms["base_url"], kms["port"]
    kwargs = {"base_url": base, "allow_http": True, "username": "alice", "password": "alice-kms-pass"}
    if case == "no_credentials":
        kwargs.pop("username"), kwargs.pop("password")
    elif case == "username_without_password":
        kwargs["password"] = ""
    elif case == "whitespace_username":
        kwargs["username"] = "   "
    elif case == "whitespace_password":
        kwargs["password"] = " \t "
    elif case == "http_without_allow_http":
        kwargs["allow_http"] = False
    elif case == "allow_http_as_text_false":
        # An unvalidated config dict may carry the text "false".
        kwargs["allow_http"] = "false"
    elif case == "userinfo":
        kwargs["base_url"] = f"http://alice:{_SECRET_PW}@127.0.0.1:{port}"
    elif case == "empty_userinfo":
        kwargs["base_url"] = f"http://@127.0.0.1:{port}"
    elif case == "colon_only_userinfo":
        kwargs["base_url"] = f"http://:@127.0.0.1:{port}"
    elif case == "port_out_of_range":
        kwargs["base_url"] = "http://127.0.0.1:99999"
    elif case == "port_not_a_number":
        kwargs["base_url"] = "http://127.0.0.1:80a"
    elif case == "space_in_host":
        kwargs["base_url"] = "http://kms example.com"
    elif case == "unclosed_ipv6":
        kwargs["base_url"] = "http://[::1"
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
    assert _SECRET_PW not in str(info.value) and _SECRET_PW not in repr(info.value)
    assert info.value.__context__ is None and info.value.__cause__ is None
    # The tool layer turns any message with an auth marker into "sign in again".
    assert _no_marker(str(info.value)) == []
    assert kms["mock"].REQUEST_LOG == []


def test_text_flags_from_an_unvalidated_config_are_read_as_text(kms):
    shown = client(kms, include_offline="false").description
    assert "Only Online content is shown" in shown
    assert "Online and offline content is shown" in client(kms, include_offline="true").description


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
    ("no_auth_type_with_both_kinds", "token", 0),
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
        # Both kinds present and no auth_type: the token wins (bob's login is never used).
        "no_auth_type_with_both_kinds": {**bob, "api_token": "alice-api-token-1"},
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
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms, password=wrong)._request("tree", "/cm/tree/branch/view")
    assert info.value.kind == "auth"
    assert wrong not in repr(info.value)
    assert info.value.__context__ is None and info.value.__cause__ is None


def test_login_without_a_session_is_not_treated_as_logged_in(kms):
    mock = kms["mock"]
    mock.STATE["login_mode"] = "none"   # SUCCESS, but no cookie and no token
    c = client(kms)
    result = c.test_connection()
    assert result["success"] is False
    assert _no_marker(result["message"]) == []
    # Not blamed on the password: the login itself was accepted.
    assert result["message"] != str(kc.KmsHTTPError(401, "auth"))
    # Nor is it the generic "unexpected response" answer: it names what is missing.
    assert result["message"] != str(kc.KmsHTTPError(200, "server"))
    for _ in range(2):
        with pytest.raises(kc.KmsHTTPError) as info:
            c._request("tree", "/cm/tree/branch/view")
        assert (info.value.status, info.value.kind) == (200, "server")
        assert str(info.value) == result["message"]
    # Every call tried to log in again; none went out without a session.
    assert mock.STATE["accepted_logins"] == 3
    assert _rows(kms, "/cm/tree/branch/view") == []
    assert _rows(kms, "/sessionInfo") == []


@pytest.mark.parametrize("make,user,method", [
    (client, "alice", "userpass"), (client, "bob", "userpass"),
    (token_client, "alice", "token"), (token_client, "bob", "token"),
])
def test_test_connection_reports_identity(kms, make, user, method):
    # KMS reports its own user name, which differs from the login name typed in.
    reported = kms["mock"].USERS[user]["session_username"]
    assert reported != user
    result = make(kms, user=user).test_connection()
    assert result["success"] is True, result
    details = result["details"]
    assert details["identity"] == reported
    assert details["auth_method"] == method
    assert details["host"] == f"127.0.0.1:{kms['port']}"
    assert details["root_folder_id"] is None
    assert reported in result["message"]


def test_identity_ignores_a_non_text_user_name(kms):
    kms["mock"].STATE["odd_shapes"] = "bad_username"
    result = client(kms).test_connection()
    assert result["success"] is True, result
    assert kms["mock"].ODD_MARKER not in repr(result)
    assert result["details"]["identity"] == kms["mock"].USERS["alice"]["full_name"]


def test_dropped_session_info_error_is_logged_by_kind_only(kms, caplog):
    caplog.set_level(logging.DEBUG, logger=kc.__name__)
    kms["mock"].STATE["odd_shapes"] = "error"
    assert token_client(kms).test_connection()["success"] is False
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "session_info" in text and "rejected" in text
    assert kms["mock"].ODD_MARKER not in text


def test_unexpected_failure_is_logged_by_class_name_only(kms, caplog, monkeypatch):
    marker = "unexpected-" + uuid.uuid4().hex

    def explode(self, *args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(kc.requests.Session, "request", explode)
    caplog.set_level(logging.DEBUG, logger=kc.__name__)
    result = token_client(kms).test_connection()
    assert result == {"success": False, "message": "KMS connection test failed."}
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings and "RuntimeError" in warnings[0].getMessage()
    assert all(marker not in r.getMessage() and not r.exc_info for r in caplog.records)


_KINDS = ("auth", "forbidden", "not_found", "throttled", "bad_request", "server", "no_data", "redirect",
          "rejected", "unreachable", "too_large")


def test_error_kinds_have_fixed_messages_without_auth_markers():
    for kind in _KINDS:
        err = kc.KmsHTTPError(500, kind)
        assert (err.status, err.kind) == (500, kind)
        assert str(err) and _no_marker(str(err)) == [], kind
    with pytest.raises(ValueError):
        kc.KmsHTTPError(500, "no-such-kind")
    # The sweep over every fixed string, including those only some paths use.
    fixed = [*kc._MESSAGES.values(), kc._USERPASS_AUTH, kc._TOKEN_AUTH, kc._TOP_LEVEL_HINT, kc._NO_SESSION,
             kc._HELD_AUTH, kc._SESSION_REFUSED]
    for message in fixed:
        assert _no_marker(message) == [], message


def test_description_names_one_identity_for_both_variants(kms):
    userpass, token = client(kms).description, token_client(kms).description
    assert userpass == token
    assert "this connection's KMS identity" in userpass
    assert _no_marker(userpass) == []


# An unusable login token is a server error, not a transport one:
# test_unusable_login_token_is_a_server_error_before_any_request.
@pytest.mark.parametrize("case,variant", [
    ("truncated_body", "userpass"), ("truncated_body", "token"), ("bad_gzip", "userpass"),
    ("bad_gzip", "token"),
])
def test_broken_responses_are_unreachable_without_chains(raw_kms, caplog, case, variant):
    caplog.set_level(logging.DEBUG, logger=kc.__name__)
    raw_kms["state"]["response"] = _RAW_BODIES[case]()
    creds = ({"username": "alice", "password": _SECRET_PW} if variant == "userpass"
             else {"api_token": "raw-api-token"})
    c = kc.KmsLighthouseClient(base_url=raw_kms["base_url"], allow_http=True, **creds)
    with pytest.raises(kc.KmsHTTPError) as info:
        c._request("tree", "/cm/tree/branch/view")
    err = info.value
    assert (err.status, err.kind) == (0, "unreachable")
    assert err.__context__ is None and err.__cause__ is None
    for leaked in (_RAW_MARKER, _SECRET_PW, raw_kms["base_url"].split("//")[1]):
        assert leaked not in str(err) and leaked not in repr(err)
    # This module's own records (urllib3's are gated: test_debug_logs_never_contain_credentials_or_query).
    own = [r.getMessage() for r in caplog.records if r.name == kc.__name__]
    failures = [m for m in own if " failed: " in m]
    assert failures and failures[-1].startswith("kms_lighthouse ") and failures[-1].endswith("Error)")
    assert all(_RAW_MARKER not in m and _SECRET_PW not in m for m in own)
    # test_connection reports the same fixed message.
    assert c.test_connection() == {"success": False, "message": str(err)}


def test_duplicate_session_cookies_still_log_in(raw_kms):
    body = json.dumps({"status": "SUCCESS", "data": {"userId": 11}}).encode()
    raw_kms["state"]["response"] = _http_200(
        body, "Set-Cookie: JSESSIONID=first; Path=/", "Set-Cookie: JSESSIONID=second; Path=/kms")
    c = kc.KmsLighthouseClient(base_url=raw_kms["base_url"], allow_http=True, username="alice", password="pw")
    assert c._request("session_info", "/sessionInfo") == {"status": "SUCCESS", "data": {"userId": 11}}
    login, follow_up = raw_kms["state"]["requests"][:2]
    assert login.startswith("POST ")
    assert "\r\nCookie: JSESSIONID=" in follow_up


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


@pytest.mark.parametrize("key,value", [("odd_shapes", True), ("odd_shapes", "lists"), ("login_mode", "cookies")])
def test_mock_refuses_unknown_switch_values(kms, key, value):
    # A typo in a test switch must fail loudly, not silently test the happy path.
    with pytest.raises(ValueError):
        kms["mock"].STATE[key] = value


def test_top_level_rejection_hints_root_folder(kms):
    kms["mock"].STATE["top_level_requires_parent"] = True
    result = client(kms).test_connection()
    assert result["success"] is False
    assert "Root folder ID" in result["message"] and _no_marker(result["message"]) == []
    # Folder 95 exists and is empty: with a root set, an empty folder is a pass.
    rooted = client(kms, root_folder_id=95).test_connection()
    assert rooted["success"] is True, rooted
    assert rooted["details"]["root_folder_id"] == 95
    assert "root folder came back empty" in rooted["message"]
    assert "empty" not in client(kms, root_folder_id=78).test_connection()["message"]


# ------------------------------------------------------------------ P2: transport hardening
# `_request` is the transport seam the plan declares for P2-P6 (search, list and
# read arrive in P3-P5); these tests drive it against the mock's real socket.
_TREE = "/cm/tree/branch/view"


def _assert_closed(err, *secrets):
    """A KmsHTTPError with no exception chain and none of `secrets` in its text."""
    assert err.__context__ is None and err.__cause__ is None
    assert _no_marker(str(err)) == []
    for secret in secrets:
        assert secret not in str(err) and secret not in repr(err)


@pytest.mark.parametrize("expired_response", ["401", "302"])
def test_expired_session_relogs_in_once(kms, expired_response):
    mock = kms["mock"]
    mock.STATE["expired_response"] = expired_response
    c = client(kms)
    c._request("tree", _TREE)
    logins = mock.STATE["login_count"]
    mock.expire_sessions()
    payload = c._request("tree", _TREE)
    assert payload["status"] == "SUCCESS" and payload["data"]
    assert mock.STATE["login_count"] == logins + 1
    assert mock.STATE["stale_401s"] == 1
    # The expired call was answered unauthenticated, the retry ran as alice again.
    assert [row[0] for row in _rows(kms, _TREE)] == ["alice", None, "alice"]


@pytest.mark.parametrize("expired_response", ["401", "302"])
def test_second_rejection_is_auth_error(kms, caplog, expired_response):
    caplog.set_level(logging.DEBUG, logger=kc.__name__)
    mock = kms["mock"]
    mock.STATE["expired_response"] = expired_response
    mock.STATE["session_expiry"] = "immediate"   # every login session is dead on arrival
    c = client(kms)
    with pytest.raises(kc.KmsHTTPError) as info:
        c._request("tree", _TREE)
    assert (info.value.status, info.value.kind) == (401, "auth")
    _assert_closed(info.value, mock.USERS["alice"]["password"])
    # KMS just accepted the password, so the refusal is not blamed on it.
    assert str(info.value) != str(kc.KmsHTTPError(401, "auth"))
    warnings = [r.getMessage() for r in caplog.records if r.name == kc.__name__ and r.levelno == logging.WARNING]
    assert any(w.startswith("kms_lighthouse tree ") for w in warnings), warnings
    assert all(mock.USERS["alice"]["password"] not in w for w in warnings)
    # The first login plus exactly one re-login, and the call plus exactly one retry.
    assert mock.STATE["accepted_logins"] == 2
    assert len(_rows(kms, _TREE)) == 2


@pytest.mark.parametrize("expired_response,kind", [("401", "auth"), ("302", "redirect")])
def test_token_expiry_is_auth_error_without_login(kms, expired_response, kind):
    mock = kms["mock"]
    mock.STATE["expired_response"] = expired_response
    stale = "expired-token-" + uuid.uuid4().hex   # well formed, but KMS no longer knows it
    c = token_client(kms, api_token=stale)
    with pytest.raises(kc.KmsHTTPError) as info:
        c._request("tree", _TREE)
    assert info.value.kind == kind
    _assert_closed(info.value, stale)
    if kind == "auth":
        # The token wording, not the username/password one.
        assert str(info.value) != str(kc.KmsHTTPError(401, "auth"))
    assert mock.STATE["login_count"] == 0
    assert len(_rows(kms, _TREE)) == 1
    result = c.test_connection()
    assert result["success"] is False and stale not in repr(result)
    assert mock.STATE["login_count"] == 0


def test_concurrent_expiry_relogs_in_once(kms):
    mock = kms["mock"]
    c = client(kms)
    c._request("tree", _TREE)
    accepted = mock.STATE["accepted_logins"]
    mock.STATE["hold_stale_401s"] = 2
    mock.expire_sessions()
    results, errors = [], []

    def call():
        try:
            results.append(c._request("tree", _TREE))
        except Exception as exc:  # recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert errors == [] and len(results) == 2
    assert all(r["status"] == "SUCCESS" and r["data"] for r in results)
    # Both threads really got a 401 before anyone logged in again ...
    assert mock.STATE["stale_401s"] == 2
    assert [row[0] for row in _rows(kms, _TREE)].count(None) == 2
    # ... and the two expiries caused one login.
    assert mock.STATE["accepted_logins"] == accepted + 1


@pytest.mark.parametrize("make", [client, token_client])
def test_status_error_on_200_maps_to_not_found(kms, make):
    item_id = str(uuid.uuid4().int)[:12]
    c = make(kms)
    with pytest.raises(kc.KmsHTTPError) as info:
        c._request("item", f"/_mock/status_error/{item_id}", missing="not_found")
    assert (info.value.status, info.value.kind) == (200, "not_found")
    _assert_closed(info.value, item_id)
    # The default `missing` kind for an ERROR body.
    with pytest.raises(kc.KmsHTTPError) as info:
        c._request("item", f"/_mock/status_error/{item_id}")
    assert (info.value.status, info.value.kind) == (200, "rejected")
    _assert_closed(info.value, item_id)
    assert {row[0] for row in _rows(kms, f"/_mock/status_error/{item_id}")} == {"alice"}


@pytest.mark.parametrize("make", [client, token_client])
def test_empty_body_maps_to_no_data(kms, make):
    with pytest.raises(kc.KmsHTTPError) as info:
        make(kms)._request("item", "/_mock/empty")
    assert (info.value.status, info.value.kind) == (200, "no_data")
    _assert_closed(info.value)
    assert {row[0] for row in _rows(kms, "/_mock/empty")} == {"alice"}


def test_oversized_response_is_refused(kms, caplog, monkeypatch):
    mock = kms["mock"]
    caplog.set_level(logging.DEBUG, logger=kc.__name__)
    c = token_client(kms)
    monkeypatch.setattr(kc, "_MAX_RESPONSE_BYTES", mock.OVERSIZED_BYTES // 4)
    with pytest.raises(kc.KmsHTTPError) as info:
        c._request("item", "/_mock/oversized")
    assert (info.value.status, info.value.kind) == (200, "too_large")
    _assert_closed(info.value)
    own = [r.getMessage() for r in caplog.records if r.name == kc.__name__]
    assert any("too_large" in m for m in own)
    # The same body under a cap that fits it is read in full.
    monkeypatch.setattr(kc, "_MAX_RESPONSE_BYTES", mock.OVERSIZED_BYTES * 2)
    assert len(c._request("item", "/_mock/oversized")["data"]) == mock.OVERSIZED_BYTES


def _closed_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@pytest.mark.parametrize("variant", ["userpass", "token"])
def test_transport_errors_carry_no_url_or_secret(variant):
    port = _closed_port()
    secret = "secret-" + uuid.uuid4().hex
    query = "query-marker-" + uuid.uuid4().hex
    creds = {"username": "alice", "password": secret} if variant == "userpass" else {"api_token": secret}
    c = kc.KmsLighthouseClient(base_url=f"http://127.0.0.1:{port}", allow_http=True, **creds)
    with pytest.raises(kc.KmsHTTPError) as info:
        c._request("search", "/search/full", params={"query": query})
    err = info.value
    assert (err.status, err.kind) == (0, "unreachable")
    assert err.__context__ is None and err.__cause__ is None
    # Only the exception-message lines: source lines may name a "password" variable.
    shown = [part for part in traceback.format_exception(err) if not part.startswith(("Traceback", "  File "))]
    result = c.test_connection()
    assert result["success"] is False
    for text in (str(err), repr(err), "".join(shown), repr(result)):
        for leaked in (secret, query, f"127.0.0.1:{port}", "/login", "query=", "/search/full"):
            assert leaked not in text, (leaked, text)


def test_debug_logs_never_contain_credentials_or_query(kms, caplog, monkeypatch):
    mock = kms["mock"]
    password = "pw-" + uuid.uuid4().hex
    monkeypatch.setitem(mock.USERS["alice"], "password", password)
    query = "probe-" + uuid.uuid4().hex
    caplog.set_level(logging.DEBUG)
    payload = client(kms)._request("tree", _TREE, params={"parentId": "78", "probe": query})
    assert payload["status"] == "SUCCESS"
    logged = caplog.text + "\n".join(r.getMessage() for r in caplog.records)
    assert password not in logged and query not in logged and "password=" not in logged
    assert any(r.name == kc.__name__ and "login" in r.getMessage() for r in caplog.records)
    # The gate is real: the same urllib3 loggers still log a request made outside the client.
    outside = "outside-" + uuid.uuid4().hex
    with requests.Session() as plain:
        plain.trust_env = False
        plain.get(f"{kms['base_url']}{mock.PREFIX}/sessionInfo?probe={outside}", timeout=5)
    assert any(r.name.startswith("urllib3") and outside in r.getMessage() for r in caplog.records)


def test_closed_errors_pass_fixed_errors_and_close_the_rest(caplog):
    caplog.set_level(logging.DEBUG, logger=kc.__name__)
    marker = "unexpected-" + uuid.uuid4().hex
    fixed = [kc.KmsError("fixed"), kc.KmsHTTPError(404, "not_found"), GlobScopeError("outside"),
             FileTooLargeError("too big"), SearchStopped("deadline", [])]
    for exc in fixed:
        @kc._closed_errors
        def fixed_failure(exc=exc):
            raise exc
        with pytest.raises(type(exc)) as info:
            fixed_failure()
        assert info.value is exc

    @kc._closed_errors
    def broken(value):
        if value:
            raise RuntimeError(marker)
        return "ok"

    assert broken(False) == "ok" and broken.__name__ == "broken"
    with pytest.raises(kc.KmsHTTPError) as info:
        broken(True)
    assert (info.value.status, info.value.kind) == (0, "server")
    _assert_closed(info.value, marker)
    # Logged by class name only.
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("broken" in w and "RuntimeError" in w for w in warnings), warnings
    assert all(marker not in r.getMessage() and not r.exc_info for r in caplog.records)


def test_deeply_nested_json_is_a_server_error(raw_kms):
    raw_kms["state"]["response"] = _http_200(b"[" * 200000)
    c = kc.KmsLighthouseClient(base_url=raw_kms["base_url"], allow_http=True, api_token="raw-api-token")
    with pytest.raises(kc.KmsHTTPError) as info:
        c._request("tree", _TREE)
    assert (info.value.status, info.value.kind) == (0, "server")
    _assert_closed(info.value)
    result = c.test_connection()
    assert result["success"] is False and result["message"] == str(info.value)


@pytest.mark.parametrize("token", ["lt-☃-{m}", "lt-{m}\r\nX-Injected: 1", "lt-{m}\n", "lt {m}"])
def test_unusable_login_token_is_a_server_error_before_any_request(raw_kms, token):
    marker = "tok-" + uuid.uuid4().hex
    raw_kms["state"]["response"] = _http_200(json.dumps(
        {"status": "SUCCESS", "data": {"token": token.format(m=marker)}}).encode())
    c = kc.KmsLighthouseClient(base_url=raw_kms["base_url"], allow_http=True, username="alice", password=_SECRET_PW)
    with pytest.raises(kc.KmsHTTPError) as info:
        c._request("tree", _TREE)
    assert (info.value.status, info.value.kind) == (200, "server")
    _assert_closed(info.value, marker, _SECRET_PW)
    # Only the login went out: the unusable token never reached a header.
    assert [r.split(" ", 1)[0] for r in raw_kms["state"]["requests"]] == ["POST"]


def test_rejected_credentials_are_not_echoed_by_validation_errors():
    by_auth = REGISTRY["kms_lighthouse"].credentials_auth.by_auth
    marker = "bad token " + uuid.uuid4().hex     # a space makes it invalid
    with pytest.raises(pydantic.ValidationError) as info:
        by_auth["token"].schema(api_token=marker)
    assert marker not in str(info.value) and marker.split()[-1] not in str(info.value)
    # Short enough that pydantic would print it whole if inputs were shown.
    password = "pw-" + uuid.uuid4().hex[:8]
    with pytest.raises(pydantic.ValidationError) as info:   # no username: the whole input is the culprit
        by_auth["userpass"].schema(password=password)
    assert password not in str(info.value) and "input_value" not in str(info.value)


def test_username_is_trimmed_but_password_is_not(kms):
    schema = REGISTRY["kms_lighthouse"].credentials_auth.by_auth["userpass"].schema
    assert schema(username="  alice \n", password=" pw ").model_dump() == {"username": "alice", "password": " pw "}
    result = kc.KmsLighthouseClient(base_url=kms["base_url"], allow_http=True, username=" alice\t",
                                    password=kms["mock"].USERS["alice"]["password"]).test_connection()
    assert result["success"] is True, result
    assert [row[0] for row in _rows(kms, "/login")] == ["alice"]


def test_token_pattern_is_anchored_at_both_ends():
    # Any caller that searches with it (not only fullmatch) must still refuse a bad token.
    from app.schemas.data_sources.configs import KMS_TOKEN_RE
    for bad in ("two words", " leading-space", "café-tok", "tok\n", "tok\r\nX-Injected: 1"):
        assert KMS_TOKEN_RE.search(bad) is None, bad
    assert KMS_TOKEN_RE.search("alice-api-token-1")


# ------------------------------------------------------------------ P2: urllib3 gate scope
def test_urllib3_gate_is_per_thread(kms, caplog):
    mock = kms["mock"]
    caplog.set_level(logging.DEBUG)
    c = token_client(kms)
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(c._request("item", "/_mock/held")))
    worker.start()
    outside = "outside-" + uuid.uuid4().hex
    try:
        # The KMS request is now parked inside the client, with its thread's gate closed.
        assert mock.HELD["arrived"].wait(5)
        with requests.Session() as plain:
            plain.trust_env = False
            plain.get(f"{kms['base_url']}{mock.PREFIX}/sessionInfo?probe={outside}", timeout=5)
    finally:
        mock.HELD["release"].set()
        worker.join(10)
    assert outcome and outcome[0]["status"] == "SUCCESS"
    urllib3_lines = [r.getMessage() for r in caplog.records if r.name.startswith("urllib3")]
    # This thread's request is logged; the parked KMS request is not.
    assert any(outside in line for line in urllib3_lines)
    assert not any("/_mock/held" in line for line in urllib3_lines)


def test_urllib3_gate_reopens_after_a_transport_failure(caplog):
    caplog.set_level(logging.DEBUG)
    port = _closed_port()
    c = kc.KmsLighthouseClient(base_url=f"http://127.0.0.1:{port}", allow_http=True, api_token="raw-api-token")
    with pytest.raises(kc.KmsHTTPError) as info:
        c._request("tree", _TREE)
    assert info.value.kind == "unreachable"
    outside = "outside-" + uuid.uuid4().hex
    with requests.Session() as plain:
        plain.trust_env = False
        with pytest.raises(requests.ConnectionError):
            plain.get(f"http://127.0.0.1:{port}/?probe={outside}", timeout=5)
    assert any(r.name.startswith("urllib3") and f"127.0.0.1:{port}" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------ P2: refusals are logged by reason
_REFUSAL_MARKER = "refusal-marker-" + uuid.uuid4().hex


@pytest.mark.parametrize("case,op", [
    ("not_json", "tree"), ("error_body", "item"), ("bad_tree_shape", "tree"),
    ("unusable_login_token", "login"), ("no_session", "login"),
])
def test_refusals_are_logged_by_a_fixed_reason_only(kms, raw_kms, caplog, case, op):
    caplog.set_level(logging.DEBUG, logger=kc.__name__)
    mock = kms["mock"]
    if case == "not_json":
        raw_kms["state"]["response"] = _http_200(b"<html>" + _REFUSAL_MARKER.encode() + b"</html>")
        c = kc.KmsLighthouseClient(base_url=raw_kms["base_url"], allow_http=True, api_token="raw-api-token")
        call = lambda: c._request("tree", _TREE)  # noqa: E731
    elif case == "error_body":
        call = lambda: token_client(kms)._request("item", f"/_mock/status_error/{_REFUSAL_MARKER}")  # noqa: E731
    elif case == "bad_tree_shape":
        mock.STATE["odd_shapes"] = "bad_id"
        call = None
    elif case == "unusable_login_token":
        raw_kms["state"]["response"] = _http_200(json.dumps(
            {"status": "SUCCESS", "data": {"token": "lt " + _REFUSAL_MARKER}}).encode())
        c = kc.KmsLighthouseClient(base_url=raw_kms["base_url"], allow_http=True, username="alice",
                                   password=_SECRET_PW)
        call = lambda: c._request("tree", _TREE)  # noqa: E731
    else:
        mock.STATE["login_mode"] = "none"
        call = lambda: client(kms)._request("tree", _TREE)  # noqa: E731
    if call is None:
        result = token_client(kms).test_connection()
        assert result["success"] is False
        info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any("test_connection" in line and "server" in line for line in info_lines), info_lines
    else:
        with pytest.raises(kc.KmsHTTPError):
            call()
    own = [r.getMessage() for r in caplog.records if r.name == kc.__name__]
    assert any(line.startswith(f"kms_lighthouse {op} refused: ") for line in own), own
    for line in own:
        assert _REFUSAL_MARKER not in line and _SECRET_PW not in line and mock.ODD_MARKER not in line


# ------------------------------------------------------------------ P2b: session reuse and login hold
def _frozen_clock(monkeypatch, start=1000.0):
    """Replace the client's clock seam; returns a one-item list holding the current time."""
    now = [start]
    monkeypatch.setattr(kc, "_clock", lambda: now[0])
    return now


def _tree_identities(rows):
    return [row[0] for row in rows if row[1] == "GET" and row[2].endswith(_TREE)]


def test_session_is_reused_across_client_instances(kms):
    mock = kms["mock"]
    for _ in range(2):
        assert client(kms)._request("tree", _TREE)["status"] == "SUCCESS"
    assert mock.STATE["login_count"] == 1
    assert _tree_identities(mock.REQUEST_LOG) == ["alice", "alice"]


@pytest.mark.parametrize("case", ["other_user", "wrong_password", "other_prefix"])
def test_sessions_are_never_shared_across_identities(kms, monkeypatch, case):
    mock = kms["mock"]
    # The hardest case for the key: bob's password is the same text as alice's.
    monkeypatch.setitem(mock.USERS["bob"], "password", mock.USERS["alice"]["password"])
    client(kms)._request("tree", _TREE)          # alice's session is now pooled
    second = {
        "other_user": lambda: client(kms, user="bob"),
        "wrong_password": lambda: client(kms, password="wrong-" + uuid.uuid4().hex),
        "other_prefix": lambda: client(kms, api_prefix="/other/api"),
    }[case]()
    start = len(mock.REQUEST_LOG)
    if case == "other_user":
        assert second._request("tree", _TREE)["status"] == "SUCCESS"
    else:
        with pytest.raises(kc.KmsHTTPError) as info:
            second._request("tree", _TREE)
        assert info.value.kind == {"wrong_password": "auth", "other_prefix": "not_found"}[case]
    gets = [row for row in mock.REQUEST_LOG[start:] if row[1] == "GET"]
    # The mock names whichever session a request carried: never alice's for another identity.
    assert all(row[0] != "alice" for row in gets), gets
    if case == "other_user":
        assert gets and {row[0] for row in gets} == {"bob"}
    else:
        assert gets == []


def test_a_password_that_is_not_valid_utf8_still_builds_a_client(kms):
    # A JSON credential can carry a lone surrogate; building the pool key must not raise with it.
    odd = "pw-\ud800-" + uuid.uuid4().hex[:8]
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms, password=odd)._request("tree", _TREE)
    _assert_closed(info.value, odd)
    assert _rows(kms, _TREE) == []


def test_session_reuse_expires_after_ttl(kms, monkeypatch):
    mock = kms["mock"]
    now = _frozen_clock(monkeypatch)
    client(kms)._request("tree", _TREE)
    now[0] += 599                                # the plan's 10-minute reuse window
    client(kms)._request("tree", _TREE)
    assert mock.STATE["login_count"] == 1
    now[0] += 2
    client(kms)._request("tree", _TREE)
    assert mock.STATE["login_count"] == 2
    assert _tree_identities(mock.REQUEST_LOG) == ["alice"] * 3


def test_rejected_login_is_not_repeated_within_hold(kms, monkeypatch):
    mock = kms["mock"]
    now = _frozen_clock(monkeypatch)
    wrong = "wrong-" + uuid.uuid4().hex
    messages = []
    for _ in range(2):
        with pytest.raises(kc.KmsHTTPError) as info:
            client(kms, password=wrong)._request("tree", _TREE)
        assert info.value.kind == "auth"
        _assert_closed(info.value, wrong)
        messages.append(str(info.value))
    assert mock.STATE["login_count"] == 1
    # The held refusal says so; it is not a second "check the password" answer.
    assert messages[1] != messages[0]
    # The right password is a different identity key: it logs in at once.
    assert client(kms)._request("tree", _TREE)["status"] == "SUCCESS"
    assert mock.STATE["login_count"] == 2
    now[0] += 299                                # the plan's 5-minute hold
    with pytest.raises(kc.KmsHTTPError):
        client(kms, password=wrong)._request("tree", _TREE)
    assert mock.STATE["login_count"] == 2
    now[0] += 2
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms, password=wrong)._request("tree", _TREE)
    assert info.value.kind == "auth"
    assert mock.STATE["login_count"] == 3


def test_concurrent_expiry_across_clients_relogs_in_once(kms):
    mock = kms["mock"]
    clients = [client(kms), client(kms)]
    for c in clients:
        c._request("tree", _TREE)
    accepted = mock.STATE["accepted_logins"]
    mock.STATE["hold_stale_401s"] = 2
    mock.expire_sessions()
    results, errors = [], []

    def call(c):
        try:
            results.append(c._request("tree", _TREE))
        except Exception as exc:  # recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=call, args=(c,)) for c in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert errors == [] and len(results) == 2
    assert all(r["status"] == "SUCCESS" and r["data"] for r in results)
    # Both instances really got a 401 before anyone logged in again ...
    assert mock.STATE["stale_401s"] == 2
    # ... and the two expiries caused one login.
    assert mock.STATE["accepted_logins"] == accepted + 1


def test_test_connection_logs_in_fresh_but_honours_hold(kms, monkeypatch):
    mock = kms["mock"]
    _frozen_clock(monkeypatch)
    client(kms)._request("tree", _TREE)          # a tool call pools alice's session
    assert mock.STATE["login_count"] == 1
    assert client(kms).test_connection()["success"] is True
    assert mock.STATE["login_count"] == 2        # the probe was not answered warm
    # The password changes on the KMS side; BOW still holds the old one.
    saved = mock.USERS["alice"]["password"]
    changed = "changed-" + uuid.uuid4().hex
    monkeypatch.setitem(mock.USERS["alice"], "password", changed)
    first = client(kms, password=saved).test_connection()
    assert first["success"] is False and mock.STATE["login_count"] == 3
    second = client(kms, password=saved).test_connection()
    assert second["success"] is False
    assert mock.STATE["login_count"] == 3        # held: no new login went out
    assert second["message"] != first["message"] and _no_marker(second["message"]) == []
    for text in (repr(first), repr(second)):
        assert changed not in text and saved not in text
    # Tool calls keep the session they already hold (ADR-6), without a login.
    assert client(kms, password=saved)._request("tree", _TREE)["status"] == "SUCCESS"
    assert mock.STATE["login_count"] == 3


@pytest.mark.parametrize("key,value,error", [
    ("no_such_switch", True, KeyError), ("top_level_requires_parent", "yes", ValueError),
    ("ai_text_available", 1, ValueError), ("hold_stale_401s", -1, ValueError),
    ("session_expiry", "soon", ValueError),
])
def test_mock_state_refuses_unknown_keys_and_bad_values(kms, key, value, error):
    with pytest.raises(error):
        kms["mock"].STATE[key] = value
    with pytest.raises(error):
        kms["mock"].STATE.update({key: value})
