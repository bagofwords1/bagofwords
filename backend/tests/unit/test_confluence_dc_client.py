"""Confluence Data Center connector contract against the in-process mock REST API.

The mock (tools/confluence/mock_confluence_server.py) is the remote boundary: a
real HTTP server on a real socket speaking the Confluence Data Center REST
shapes, with per-user space permissions, page restrictions, a context path,
server-side page-size caps and the on-prem failure modes (CAPTCHA, SSO
redirect, disabled PATs, 429). Everything on our side (client, schemas,
registry) runs for real.
"""
import importlib.util
import sys
import threading
import time
from pathlib import Path

import pydantic
import pytest

from app.data_sources.clients import _atlassian_dc_http as dc
from app.data_sources.clients import confluence_dc_client as cc
from app.data_sources.clients._file_source_common import FileTooLargeError, GlobScopeError
from app.data_sources.clients.base import SearchStopped
from app.schemas.data_source_registry import REGISTRY, resolve_client_class

_MOCK_PATH = Path(__file__).resolve().parents[3] / "tools" / "confluence" / "mock_confluence_server.py"


def _load_mock():
    if "confluence_dc_mock" in sys.modules:
        return sys.modules["confluence_dc_mock"]
    spec = importlib.util.spec_from_file_location("confluence_dc_mock", _MOCK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["confluence_dc_mock"] = module
    spec.loader.exec_module(module)
    return module


def _serve(context):
    mock = _load_mock()
    server = mock.make_server(0, context=context)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture(scope="session")
def confluence_server():
    """A mock Confluence at http://127.0.0.1:<port>/confluence (an on-prem context path)."""
    server = _serve("/confluence")
    host, port = server.server_address[:2]
    yield {"mock": _load_mock(), "base_url": f"http://{host}:{port}/confluence", "root": f"http://{host}:{port}",
           "port": port}
    server.shutdown()
    server.server_close()


@pytest.fixture
def conf(confluence_server, monkeypatch):
    mock = confluence_server["mock"]
    mock.reset()
    monkeypatch.setattr(dc, "_sleep", lambda s: None)   # a 429 retry must not slow the suite
    yield confluence_server
    mock.reset()


def _token(conf, user):
    return next(t for t, u in conf["mock"].TOKENS.items() if u == user)


def client(conf, user="bob", **kw):
    """A PAT client for a mock user (pass username/password for Basic)."""
    kw.setdefault("base_url", conf["base_url"])
    kw.setdefault("allow_http", True)
    if "username" not in kw and "personal_access_token" not in kw:
        kw["personal_access_token"] = _token(conf, user)
    return cc.ConfluenceDcClient(**kw)


def basic(conf, user="bob", password=None, **kw):
    return client(conf, username=user, password=password or conf["mock"].USERS[user]["password"], **kw)


def users_seen(conf):
    return {user for user, _, _ in conf["mock"].REQUEST_LOG}


def ids(entries):
    return {e["id"] for e in entries}


# ----------------------------------------------------------------------------- registry and schemas

def test_registry_entry_is_a_live_per_user_file_source():
    entry = REGISTRY["confluence_dc"]
    assert resolve_client_class("confluence_dc") is cc.ConfluenceDcClient
    assert entry.data_shape == "files" and entry.is_document_based
    assert entry.catalog_ownership == "none"
    assert set(entry.credentials_auth.by_auth) == {"token", "userpass"}
    for variant in entry.credentials_auth.by_auth.values():
        assert set(variant.scopes) == {"system", "user"}, "members may connect with their own token or login"
    assert cc.ConfluenceDcClient.cheap_live_listing is True


def test_config_schema_validates_space_keys_and_stays_live_only():
    cfg = REGISTRY["confluence_dc"].config_schema
    assert cfg(base_url="https://c.example.com", spaces=" eng,HR  ~jdoe ").spaces == "eng, HR, ~jdoe"
    assert cfg(base_url="https://c.example.com", spaces="  ").spaces is None
    with pytest.raises(pydantic.ValidationError):
        cfg(base_url="https://c.example.com", spaces="Engineering Team!")
    with pytest.raises(pydantic.ValidationError):
        cfg(base_url="https://c.example.com", index_mode="content")


def test_credential_schemas_never_echo_a_rejected_secret():
    token_schema = REGISTRY["confluence_dc"].credentials_auth.by_auth["token"].schema
    secret = "has space-secret-81f"
    with pytest.raises(pydantic.ValidationError) as err:
        token_schema(personal_access_token=secret)
    assert secret not in str(err.value)
    assert token_schema(personal_access_token="  abc123==  ").personal_access_token == "abc123=="
    userpass = REGISTRY["confluence_dc"].credentials_auth.by_auth["userpass"].schema
    with pytest.raises(pydantic.ValidationError) as err:
        userpass(username="u", password="   ")
    assert "Enter a value" in str(err.value)


# ----------------------------------------------------------------------------- URL handling

@pytest.mark.parametrize("url", [
    "ftp://c.example.com", "https://user:pw@c.example.com", "https://c.example.com/?x=1",
    "https://c.example.com/#frag", "https://c.example.com/../etc", "not a url", "", "https://c.example.com/a b",
])
def test_bad_urls_are_refused_with_a_fixed_message(url):
    with pytest.raises(cc.ConfluenceError) as err:
        cc.ConfluenceDcClient(base_url=url, personal_access_token="t0k", allow_http=True)
    assert "pw" not in str(err.value)


def test_plain_http_needs_the_lab_switch():
    with pytest.raises(cc.ConfluenceError):
        cc.ConfluenceDcClient(base_url="http://c.example.com", personal_access_token="t0k")
    assert cc.ConfluenceDcClient(base_url="http://c.example.com", personal_access_token="t0k", allow_http=True)


def test_a_pasted_rest_api_suffix_is_dropped_and_the_context_path_kept(conf):
    c = client(conf, base_url=conf["base_url"] + "/rest/api/")
    assert c.context_path == "/confluence"
    assert c.test_connection()["success"] is True


def test_a_missing_context_path_is_named_as_not_the_rest_api(conf):
    result = client(conf, base_url=conf["root"]).test_connection()
    assert result["success"] is False
    assert "context path" in result["message"]
    assert "login_refused" not in result


def test_a_site_served_at_the_root_works_without_a_context_path():
    server = _serve("")
    try:
        mock = _load_mock()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        token = next(t for t, u in mock.TOKENS.items() if u == "alice")
        c = cc.ConfluenceDcClient(base_url=base, personal_access_token=token, allow_http=True)
        assert c.test_connection()["success"] is True
        assert "page:1003" in ids(c.search_files("failover"))
    finally:
        server.shutdown()
        server.server_close()


# ----------------------------------------------------------------------------- test_connection / auth

@pytest.mark.parametrize("make", [lambda conf: client(conf, "bob"), lambda conf: basic(conf, "bob")],
                         ids=["token", "userpass"])
def test_connection_names_the_confluence_identity(conf, make):
    result = make(conf).test_connection()
    assert result["success"] is True, result
    assert result["details"]["identity"] == "bob"
    assert result["details"]["context_path"] == "/confluence"
    assert users_seen(conf) == {"bob"}


def test_a_wrong_password_is_a_refused_login_and_the_password_never_leaks(conf):
    wrong = "wrong-conf-pass-5e1"
    result = basic(conf, "bob", password=wrong).test_connection()
    assert result["success"] is False and result["login_refused"] == "rejected"
    assert wrong not in str(result)


@pytest.mark.parametrize("switch", ["bad_token", "pats_disabled"])
def test_a_token_confluence_refuses_is_a_refused_login(conf, switch):
    if switch == "pats_disabled":
        conf["mock"].STATE["pats_disabled"] = True
        c = client(conf, "bob")
    else:
        c = client(conf, personal_access_token="NOTaREALpat0000000000")
    result = c.test_connection()
    assert result["success"] is False and result["login_refused"] == "rejected"
    assert "personal access token" in result["message"]


def test_a_captcha_locked_account_is_named_as_such(conf):
    conf["mock"].STATE["captcha_users"] = {"bob"}
    result = basic(conf, "bob").test_connection()
    assert result["success"] is False and result["login_refused"] == "captcha"
    assert "CAPTCHA" in result["message"]


def test_an_anonymous_answer_is_not_a_successful_login(conf):
    conf["mock"].STATE["anonymous_access"] = True
    c = client(conf, "bob")
    c._auth_header = ""   # the credentials did not make it onto the wire (e.g. stripped by a proxy)
    c._local = threading.local()
    result = c.test_connection()
    assert result["success"] is False and result["login_refused"] == "rejected"


@pytest.mark.parametrize("switch,needle", [("sso_redirect", "redirected"), ("html_page", "REST API")])
def test_sso_and_proxy_pages_are_named_and_not_followed(conf, switch, needle):
    conf["mock"].STATE[switch] = True
    result = client(conf).test_connection()
    assert result["success"] is False
    assert needle in result["message"]
    assert "login_refused" not in result, "an SSO page is an admin setting, not a wrong token"
    assert len(conf["mock"].REQUEST_LOG) == 1, "a redirect is never followed"


def test_spaces_that_the_identity_cannot_see_fail_the_test_but_stay_savable(conf):
    result = client(conf, "bob", spaces="ENG, SEC, NOPE").test_connection()
    assert result["success"] is False and result["reachable"] is True
    assert "SEC" in result["message"] and "NOPE" in result["message"] and "ENG," not in result["message"]
    assert client(conf, "alice", spaces="ENG, SEC").test_connection()["success"] is True


def test_an_unreachable_host_is_a_fixed_message():
    c = cc.ConfluenceDcClient(base_url="http://127.0.0.1:9/confluence", personal_access_token="tok", allow_http=True)
    result = c.test_connection()
    assert result["success"] is False
    assert "127.0.0.1" not in result["message"]


# ----------------------------------------------------------------------------- search

def test_search_runs_as_the_identity_and_hides_restricted_pages(conf):
    bob = ids(client(conf, "bob").search_files("rotation"))
    alice = ids(client(conf, "alice").search_files("rotation"))
    assert "page:1004" not in bob, "a page restricted away from bob must not appear in his search"
    assert "page:1004" in alice
    assert users_seen(conf) == {"bob", "alice"}


def test_search_entries_carry_a_path_snippet_and_link(conf):
    hits = client(conf, "bob").search_files("failover")
    by_id = {h["id"]: h for h in hits}
    assert {"page:1003", "blogpost:2001"} <= set(by_id)
    page = by_id["page:1003"]
    assert page["path"] == "ENG/Database Failover"
    assert "@@@" not in page["snippet"] and "failover" in page["snippet"].lower()
    assert page["web_url"] == conf["base_url"] + "/pages/viewpage.action?pageId=1003"
    assert page["is_folder"] is False and page["kind"] == "page"


def test_space_scope_limits_search_even_against_cql_injection(conf):
    scoped = client(conf, "alice", spaces="ENG")
    assert {h["path"].split("/")[0] for h in scoped.search_files("failover")} == {"ENG"}
    hostile = scoped.search_files('failover") OR space = "SEC" OR text ~ ("x')
    assert all(h["path"].startswith("ENG/") for h in hostile)
    assert not any(h["id"] == "page:1202" for h in hostile)


def test_blog_posts_can_be_left_out(conf):
    assert "blogpost:2001" not in ids(client(conf, "bob", include_blogposts=False).search_files("failover"))


def test_search_pages_past_the_server_cap(conf):
    conf["mock"].STATE["max_limit"] = 1
    hits = client(conf, "alice").search_files("failover")
    assert ids(hits) == {"page:1003", "page:1006", "blogpost:2001", "page:1202"}


def test_search_stops_at_the_limit_and_says_so(conf):
    with pytest.raises(SearchStopped) as stop:
        client(conf, "alice").search_files("failover", limit=2)
    assert stop.value.stop_reason == "max_results"
    assert len(stop.value.files) == 2


def test_an_expired_deadline_returns_what_was_found(conf):
    with pytest.raises(SearchStopped) as stop:
        client(conf).search_files("failover", deadline=time.monotonic() - 1)
    assert stop.value.stop_reason == "deadline" and stop.value.files == []


def test_operator_only_queries_send_nothing(conf):
    assert client(conf).search_files(' "" ~ * AND ( ) ') == []
    assert conf["mock"].REQUEST_LOG == []


def test_a_429_is_retried_after_retry_after(conf):
    conf["mock"].STATE["rate_limit_next"] = 1
    assert "page:1003" in ids(client(conf).search_files("failover"))
    assert conf["mock"].STATE["rate_limited"] == 1


def test_persistent_rate_limiting_is_a_typed_throttle_error(conf):
    conf["mock"].STATE["rate_limit_next"] = 10
    with pytest.raises(cc.ConfluenceHTTPError) as err:
        client(conf).search_files("failover")
    assert err.value.kind == "throttled" and err.value.status == 429
    assert conf["mock"].STATE["rate_limited"] == cc._RATE_LIMIT_RETRIES + 1


def test_a_rejected_cql_is_a_fixed_message(conf):
    conf["mock"].STATE["fail_status"] = {"/rest/api/search": 400}
    with pytest.raises(cc.ConfluenceHTTPError) as err:
        client(conf).search_files("failover")
    assert "plain words" in str(err.value)


# ----------------------------------------------------------------------------- browse

def test_listing_walks_every_visible_space_with_title_paths(conf):
    entries = client(conf, "bob").list_files()
    paths = {e["id"]: e["path"] for e in entries}
    assert paths["page:1003"] == "ENG/Engineering Home/Runbooks/Database Failover"
    assert paths["page:1102"] == "HR/HR Home/Vacation Policy"
    assert paths["blogpost:2001"] == "ENG/Blog/Q3 incident review"
    assert "page:1004" not in paths and not any(p.startswith("SEC/") for p in paths.values())


def test_a_title_with_a_slash_keeps_its_path_unambiguous_and_resolvable(conf):
    paths = {e["id"]: e["path"] for e in client(conf, "bob").list_files()}
    path = paths["page:1006"]
    assert path.count("/") == 3, path
    # The folder part of the path opens that page (it has no children here).
    assert client(conf, "bob").list_files(path) == []


def test_a_space_folder_lists_its_root_pages_or_its_whole_tree(conf):
    c = client(conf, "bob")
    assert ids(c.list_files("ENG")) == {"page:1001"}
    assert ids(c.list_files("eng", recursive=True)) == {"page:1001", "page:1002", "page:1003", "page:1005",
                                                        "page:1006", "blogpost:2001"}


@pytest.mark.parametrize("folder", ["ENG/Engineering Home/Runbooks", "ENG/Runbooks", "page:1002"])
def test_a_page_folder_lists_its_children(conf, folder):
    assert ids(client(conf, "alice").list_files(folder)) == {"page:1003", "page:1004"}
    assert ids(client(conf, "bob").list_files(folder)) == {"page:1003"}


def test_a_page_folder_lists_its_whole_subtree_when_recursive(conf):
    assert ids(client(conf, "bob").list_files("page:1001", recursive=True)) == {
        "page:1002", "page:1003", "page:1005", "page:1006"}


def test_a_path_whose_ancestors_do_not_match_is_not_found(conf):
    with pytest.raises(cc.ConfluenceHTTPError) as err:
        client(conf).list_files("ENG/Release Process/Runbooks")
    assert err.value.kind == "not_found"


@pytest.mark.parametrize("folder", ["SEC", "SEC/Security Home", "page:1202"])
def test_browsing_never_leaves_the_configured_spaces(conf, folder):
    with pytest.raises(GlobScopeError):
        client(conf, "alice", spaces="ENG, HR").list_files(folder)


def test_a_scoped_listing_lists_only_those_spaces(conf):
    entries = client(conf, "alice", spaces="HR").list_files()
    assert {e["path"].split("/")[0] for e in entries} == {"HR"}


def test_listing_honours_the_limit(conf):
    assert len(client(conf, "alice").list_files(limit=3)) == 3
    with pytest.raises(cc.ConfluenceError):
        client(conf).list_files(limit=0)


def test_bad_folders_are_refused_before_any_request(conf):
    for folder in ("page:abc", "bad key!/x"):
        with pytest.raises(cc.ConfluenceError):
            client(conf).list_files(folder)
    assert conf["mock"].REQUEST_LOG == []


# ----------------------------------------------------------------------------- read

def test_read_returns_a_header_and_tables_as_markdown(conf):
    doc = client(conf, "bob").read_file("page:1003")
    assert doc.name == "Database Failover.txt"
    assert doc.startswith("# Database Failover")
    assert "Space: Engineering (ENG)" in doc
    assert "Path: ENG / Engineering Home / Runbooks / Database Failover" in doc
    assert "(version 12)" in doc and "Dana DBA" in doc
    assert f"URL: {conf['base_url']}/pages/viewpage.action?pageId=1003" in doc
    assert "| Step | Action | Owner |" in doc
    assert "| 2 | Promote `orders-standby` (RTO 15 min) | DBA |" in doc
    assert "[Warning]" in doc and "pg_ctl promote" in doc
    assert "<td" not in doc and "&amp;" not in doc


@pytest.mark.parametrize("file_id", ["1102", "page:1102", "PAGE:1102",
                                     "{base}/pages/viewpage.action?pageId=1102"])
def test_read_accepts_ids_bare_digits_and_page_links(conf, file_id):
    doc = client(conf).read_file(file_id.format(base=conf["base_url"]))
    assert "| 3-5 | 23 |" in doc


def test_a_restricted_page_reads_as_not_found(conf):
    with pytest.raises(cc.ConfluenceHTTPError) as err:
        client(conf, "bob").read_file("page:1004")
    assert err.value.kind == "not_found"
    assert "Dana" in client(conf, "alice").read_file("page:1004")


def test_reads_never_leave_the_configured_spaces(conf):
    with pytest.raises(GlobScopeError):
        client(conf, "alice", spaces="HR").read_file("page:1003")


@pytest.mark.parametrize("file_id", ["space:ENG", "page:12a", "https://elsewhere.example.com/x?pageId=1003",
                                     "../../etc/passwd", ""])
def test_ids_that_are_not_content_are_refused_without_a_request(conf, file_id):
    with pytest.raises(cc.ConfluenceError):
        client(conf).read_file(file_id)
    assert conf["mock"].REQUEST_LOG == []


def test_an_id_of_the_wrong_kind_is_refused(conf):
    with pytest.raises(cc.ConfluenceError):
        client(conf).read_file("page:2001")      # 2001 is a blog post
    assert "22 minutes" in client(conf).read_file("blogpost:2001")


def test_an_oversized_answer_is_a_fixed_error(conf):
    conf["mock"].STATE["oversized"] = 1102
    with pytest.raises(cc.ConfluenceHTTPError) as err:
        client(conf).read_file("page:1102")
    assert err.value.kind == "too_large"


def test_read_raw_bytes_and_execute_query_share_the_read(conf):
    c = client(conf)
    data, name, mime = c.read_raw_bytes("page:1102")
    assert name == "Vacation Policy.txt" and mime == "text/plain" and b"| 6+ | 26 |" in data
    with pytest.raises(FileTooLargeError):
        c.read_raw_bytes("page:1102", max_bytes=10)
    assert "Vacation" in c.execute_query('{"file_id": "page:1102"}')
    with pytest.raises(cc.ConfluenceError):
        c.execute_query('{"nope": 1}')


def test_no_catalog_is_ever_built(conf):
    c = client(conf)
    assert c.get_schemas() == [] and c.prompt_schema() == ""
    assert conf["mock"].REQUEST_LOG == []


# ----------------------------------------------------------------------------- planner text

def test_planner_note_and_description_state_the_scope():
    note = cc.planner_scope_note({"spaces": "ENG, HR"})
    assert "ENG, HR" in note and "refused" in note and "page:<id>" in note
    assert "every space" in cc.planner_scope_note({})
    c = cc.ConfluenceDcClient(base_url="https://wiki.example.com/confluence", personal_access_token="t0k", spaces="ops")
    assert "OPS" in c.description and "wiki.example.com/confluence" in c.description
