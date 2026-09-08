"""Documentum connector contract against the in-process mock REST/OTDS server.

The mock (tools/documentum/mock_documentum_server.py) is the remote boundary:
a real HTTP server speaking Documentum REST Services shapes with ACLs,
versions, multi-filing, paging and OTDS token grants. Everything on our side
(client, registry, extraction pipeline) runs for real.
"""
import importlib.util
import io
import sys
import threading
from pathlib import Path

import pandas as pd
import pytest

from app.data_sources.clients._file_source_common import DocumentText, GlobScopeError, NamedBytes
from app.data_sources.clients.documentum_client import DocumentumClient, DocumentumHTTPError
from app.schemas.data_source_registry import REGISTRY, is_overlay_auth

_MOCK_PATH = Path(__file__).resolve().parents[3] / "tools" / "documentum" / "mock_documentum_server.py"


def _load_mock():
    if "documentum_mock" in sys.modules:
        return sys.modules["documentum_mock"]
    spec = importlib.util.spec_from_file_location("documentum_mock", _MOCK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["documentum_mock"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def dctm_server():
    mock = _load_mock()
    server = mock.make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield {"mock": mock, "rest_url": url + "/dctm-rest", "otds_url": url, "repository": mock.REPO}
    server.shutdown()


@pytest.fixture
def dctm(dctm_server):
    mock = dctm_server["mock"]
    mock.REQUEST_LOG.clear()
    mock.FORCE_ACS_LINKS = False
    saved_max = mock.MAX_PAGE_SIZE
    yield dctm_server
    mock.MAX_PAGE_SIZE = saved_max
    mock.FORCE_ACS_LINKS = False


def client(dctm, user="alice", password=None, **kwargs):
    pw = password or (dctm["mock"].USERS[user]["password"] if user in dctm["mock"].USERS else "wrong")
    return DocumentumClient(rest_url=dctm["rest_url"], repository=dctm["repository"], allow_http=True,
                            username=user, password=pw, **kwargs)


def otds_client(dctm, login=None, client_id="bow", secret="bow-secret", **kwargs):
    return DocumentumClient(rest_url=dctm["rest_url"], repository=dctm["repository"], allow_http=True,
                            otds_url=dctm["otds_url"], client_id=client_id, client_secret=secret,
                            partition="Corp", documentum_login=login, **kwargs)


def paths(files):
    return sorted(f["path"] for f in files)


def test_registry_contract():
    entry = REGISTRY["documentum"]
    assert entry.data_shape == "files" and entry.category == "files"
    assert entry.credentials_auth.default == "userpass"
    assert "user" in entry.credentials_auth.by_auth["userpass"].scopes
    assert entry.credentials_auth.by_auth["otds_client"].scopes == ["system"]
    assert entry.credentials_auth.by_auth["otds_impersonation"].scopes == ["user"]
    assert is_overlay_auth("documentum", "otds_impersonation")
    assert not is_overlay_auth("documentum", "userpass")


@pytest.mark.parametrize("user,expected", [
    ("alice", 12), ("bob", 10), ("carol", 4), ("dmadmin", 12),
])
def test_listing_is_acl_trimmed_per_identity(dctm, user, expected):
    files = client(dctm, user).list_files()
    assert len(files) == expected
    assert len({f["id"] for f in files}) == expected  # multi-filed docs appear once
    assert all(f["path"] and f["name"] and f["id"] and f["size"] >= 0 for f in files)
    assert all(f["path"].count("Q1_revenue.csv") <= 1 for f in files)  # only CURRENT versions


def test_root_folder_scopes_paths_and_reads(dctm):
    c = client(dctm, root_path="/Finance")
    files = c.list_files()
    assert paths(files) == ["Invoices/INV-1001.pdf", "Invoices/INV-1002.pdf", "Reports/2026/Q1_revenue.csv",
                            "Reports/2026/Q2_revenue.csv", "Reports/2026/board_summary.txt", "Reports/2026/regional_targets.xlsx"]
    frame = c.read_file("Reports/2026/Q1_revenue.csv")
    assert frame.shape == (5, 4)
    assert pd.to_numeric(frame["revenue"]).sum() == 407500  # CURRENT 2.0, not the 1.0 version
    tables = c.get_schemas()
    assert [t.name for t in tables] == [f["path"] for f in files]
    assert all(t.metadata_json["documentum"]["file_id"] == f["id"] for t, f in zip(tables, files))


def test_multi_filed_document_is_visible_under_each_root(dctm):
    hr = client(dctm, root_path="/HR").list_files()
    assert "Policies/board_summary.txt" in paths(hr)
    fin = client(dctm, root_path="/Finance").list_files()
    assert "Reports/2026/board_summary.txt" in paths(fin)


def test_include_globs_and_object_types_filter_listing_and_reads(dctm):
    c = client(dctm, root_path="/Finance", include_globs="**/*.csv")
    assert paths(c.list_files()) == ["Reports/2026/Q1_revenue.csv", "Reports/2026/Q2_revenue.csv"]
    xlsx_id = next(f["id"] for f in client(dctm, root_path="/Finance").list_files() if f["name"].endswith(".xlsx"))
    with pytest.raises(GlobScopeError):
        c.read_raw_bytes(xlsx_id)
    assert not any("content-media" in p for _, p in dctm["mock"].REQUEST_LOG)
    invoices = client(dctm, object_types="bow_invoice").list_files()
    assert {f["name"] for f in invoices} == {"INV-1001.pdf", "INV-1002.pdf"}
    assert all(f["object_type"] == "bow_invoice" for f in invoices)


def test_reads_outside_root_are_denied_before_download(dctm):
    c = client(dctm, root_path="/Finance")
    salary = next(f["id"] for f in client(dctm, root_path="/HR").list_files() if f["name"] == "Salary_Bands.csv")
    with pytest.raises(GlobScopeError):
        c.read_file(salary)
    with pytest.raises(GlobScopeError):
        c.read_file("../HR/Policies/Salary_Bands.csv")
    with pytest.raises(GlobScopeError):
        c.read_file("%2e%2e/HR/Policies/Salary_Bands.csv")
    with pytest.raises(GlobScopeError):
        c.list_files(folder_id=next(iter(dctm["mock"].REPO_DATA.objects.values()))["r_object_id"] if False else
                     next(o["r_object_id"] for o in dctm["mock"].REPO_DATA.objects.values() if o.get("r_folder_path") == ["/HR/Policies"]))
    assert not any("content-media" in p for _, p in dctm["mock"].REQUEST_LOG)


def test_acl_denied_object_is_a_permission_error_not_empty_content(dctm):
    salary = next(f["id"] for f in client(dctm, "alice").list_files() if f["name"] == "Salary_Bands.csv")
    with pytest.raises(DocumentumHTTPError) as exc:
        client(dctm, "bob").read_file(salary)
    assert exc.value.status == 403


def test_document_formats_render_through_the_shared_pipeline(dctm):
    c = client(dctm)
    by_name = {f["name"]: f["id"] for f in c.list_files()}
    text = c.read_file(by_name["Travel_Policy.docx"])
    assert isinstance(text, DocumentText) and "500 euros" in text and text.raw[:2] == b"PK"
    pdf = c.read_file(by_name["INV-1001.pdf"])
    assert isinstance(pdf, DocumentText) and "Acme Logistics" in pdf
    pages = c.read_file(by_name["INV-1001.pdf"], page_range=(2, 2))
    assert pages["pages_total"] == 2 and "Line items" in pages["text"]
    sheet = c.read_file(by_name["regional_targets.xlsx"], sheet="Notes")
    assert "board" in " ".join(map(str, sheet.iloc[:, 0].tolist())).lower()
    assert c.read_file(by_name["scratch.json"])["items"] == [1, 2, 3]
    assert "Documentum" in c.read_file(by_name["architecture.md"])
    raw, name, mime = c.read_raw_bytes(by_name["big.bin"])
    assert name == "big.bin" and len(raw) > 1_000_000
    assert isinstance(c.read_file(by_name["big.bin"]), NamedBytes)


def test_download_limit_rejects_before_transfer(dctm):
    c = client(dctm, max_file_size_mb=1)
    big = next(f["id"] for f in c.list_files() if f["name"] == "big.bin")
    with pytest.raises(ValueError, match="limit"):
        c.read_file(big)
    assert not any("content-media" in p for _, p in dctm["mock"].REQUEST_LOG)


def test_off_rest_content_links_are_refused(dctm):
    dctm["mock"].FORCE_ACS_LINKS = True
    c = client(dctm)
    csv_id = next(f["id"] for f in c.list_files() if f["name"] == "Q2_revenue.csv")
    with pytest.raises(ValueError, match="outside the configured REST URL"):
        c.read_file(csv_id)


def test_search_combines_index_and_names_and_respects_scope(dctm):
    alice = client(dctm)
    assert {f["name"] for f in alice.search_files("travel")} == {"Travel_Policy.docx", "board_summary.txt"}
    assert {f["name"] for f in client(dctm, "bob").search_files("travel")} == {"board_summary.txt"}
    assert {f["name"] for f in alice.search_files("revenue")} >= {"Q1_revenue.csv", "Q2_revenue.csv"}
    assert paths(client(dctm, root_path="/HR").search_files("travel")) == ["Policies/Travel_Policy.docx", "Policies/board_summary.txt"]
    assert client(dctm, root_path="/Finance", include_globs="**/*.pdf").search_files("revenue") == []
    assert alice.search_files("  ") == []
    hits = alice.search_files("invoice", limit=1)
    assert len(hits) == 1


def test_pagination_follows_next_links(dctm):
    dctm["mock"].MAX_PAGE_SIZE = 2
    assert len(client(dctm).list_files()) == 12
    assert len(client(dctm, root_path="/Finance").search_files("revenue")) >= 2


@pytest.mark.parametrize("limit", [0, 1, 3])
def test_listing_cap(dctm, limit):
    assert len(client(dctm).list_files(limit=limit)) == limit


def test_no_index_means_no_catalog_and_no_network(dctm):
    assert client(dctm, index_mode="none").get_schemas() == []
    assert dctm["mock"].REQUEST_LOG == []


def test_test_connection_reports_identity_and_failures(dctm):
    ok = client(dctm, "bob").test_connection()
    assert ok["success"] and ok["details"]["identity"] == "bob" and ok["details"]["auth_method"] == "basic"
    bad = client(dctm, "bob", password="nope").test_connection()
    assert bad["success"] is False and "nope" not in bad["message"]
    missing = DocumentumClient(rest_url=dctm["rest_url"], repository="other_repo", allow_http=True, username="bob", password="bob123").test_connection()
    assert missing["success"] is False


def test_otds_impersonation_runs_as_the_named_user(dctm):
    bob = otds_client(dctm, login="bob")
    probe = bob.test_connection()
    assert probe["success"] and probe["details"]["identity"] == "bob" and probe["details"]["auth_method"] == "otds_impersonation"
    assert "Salary_Bands.csv" not in {f["name"] for f in bob.list_files()}
    alice = otds_client(dctm, login="alice@Corp")
    assert "Salary_Bands.csv" in {f["name"] for f in alice.list_files()}
    denied = otds_client(dctm, login="bob", client_id="noimp", secret="noimp-secret").test_connection()
    assert denied["success"] is False and "impersonat" in denied["message"].lower()
    unknown = otds_client(dctm, login="nobody").test_connection()
    assert unknown["success"] is False


def test_otds_client_credentials_act_as_service_user_and_refresh_expired_tokens(dctm):
    svc = otds_client(dctm)
    assert svc.test_connection()["details"]["identity"] == "svc_bow"
    files = svc.list_files()
    assert len(files) == 12
    dctm["mock"].TOKENS.clear()  # server-side expiry/revocation → one transparent refresh
    assert len(svc.list_files()) == 12


def test_delegated_access_token_is_used_as_bearer(dctm):
    import requests
    tok = requests.post(dctm["otds_url"] + "/otdsws/oauth2/token", data={
        "grant_type": "password", "client_id": "bow", "client_secret": "bow-secret", "username": "carol@Corp", "password": "carol123"}).json()["access_token"]
    c = DocumentumClient(rest_url=dctm["rest_url"], repository=dctm["repository"], allow_http=True, access_token=tok)
    assert c.test_connection()["details"]["identity"] == "carol"
    assert paths(c.list_files()) == ["Engineering/Specs/architecture.md", "Engineering/Specs/release_notes.txt", "Temp/big.bin", "Temp/scratch.json"]


@pytest.mark.parametrize("kwargs", [
    dict(rest_url="http://dctm.example.com/dctm-rest", repository="r", username="u", password="p"),
    dict(rest_url="https://user:pw@dctm.example.com/dctm-rest", repository="r", username="u", password="p"),
    dict(rest_url="https://dctm.example.com/dctm-rest", repository="bad repo!", username="u", password="p"),
    dict(rest_url="https://dctm.example.com/dctm-rest", repository="r"),
    dict(rest_url="https://dctm.example.com/dctm-rest", repository="r", documentum_login="bob"),
    dict(rest_url="https://dctm.example.com/dctm-rest", repository="r", otds_url="https://otds", client_id="x"),
    dict(rest_url="https://dctm.example.com/dctm-rest", repository="r", username="u", password="p", root_path="/a/../b"),
    dict(rest_url="https://dctm.example.com/dctm-rest", repository="r", username="u", password="p", object_types="dm_document; drop"),
])
def test_invalid_configuration_is_rejected_up_front(kwargs):
    with pytest.raises(ValueError):
        DocumentumClient(**kwargs)
