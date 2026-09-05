"""Deterministic SharePoint Server contract; mock only the remote HTTP boundary."""
from app.schemas.data_source_registry import REGISTRY
import io
import json
from urllib.parse import unquote
from unittest.mock import MagicMock

import pandas as pd
import pytest
import requests
import sys
import types
from pypdf import PdfWriter

from app.data_sources.clients.sharepoint_onprem_client import SharepointOnpremClient
from app.data_sources.clients._file_source_common import GlobScopeError, NamedBytes


SITE = "https://portal.example.com/sites/research"
ROOT = "/sites/research/Shared Documents"
OTHER = "/sites/research/Policies"


@pytest.fixture
def remote(monkeypatch):
    """In-memory REST provider, not a mocked connector/service."""
    blobs = {
        ROOT + "/revenue.csv": b"region,amount\nNorth,120\nSouth,80\n",
        ROOT + "/nested/notes.txt": b"Quarterly revenue grew by twenty percent.",
        ROOT + "/O'Brien #100%.txt": b"Literal path characters are preserved.",
        OTHER + "/rules.txt": b"Employees may expense travel up to 500 euros.",
    }
    state = {"blobs": blobs, "status": None, "next": None, "calls": [], "search_paths": [], "identities": []}

    def meta(path):
        return {"Name": path.rsplit("/", 1)[1], "ServerRelativeUrl": path,
                "Length": str(len(blobs[path])), "TimeLastModified": "2026-08-01T10:00:00Z"}

    def respond(session, url, **kw):
        state["identities"].append(getattr(session.auth, "username", None))
        state["calls"].append((url, kw))
        assert kw["allow_redirects"] is False
        assert session.verify is True
        assert session.trust_env is False
        endpoint = unquote(url.split("/_api/", 1)[1])
        status, body = state["status"] or 200, {}
        if endpoint == "web/lists":
            body = {"value": [{"Id": "lib1", "Title": "Documents", "RootFolder": {"ServerRelativeUrl": ROOT}},
                              {"Id": "lib2", "Title": "Policies", "RootFolder": {"ServerRelativeUrl": OTHER}}]}
        elif endpoint == "web/DefaultDocumentLibrary":
            body = {"Id": "lib1"}
        elif endpoint == "web":
            body = {"Title": "Research", "Url": SITE}
        elif endpoint == "web/currentuser":
            body = {"LoginName": "DOMAIN\\reader"}
        elif endpoint == "search/query":
            body = {"PrimaryQueryResult": {"RelevantResults": {"Table": {"Rows": [
                {"Cells": [{"Key": "Path", "Value": path}]} for path in state["search_paths"]
            ]}}}}
        elif "decodedurl='" in endpoint:
            path = endpoint.split("decodedurl='", 1)[1].rsplit("')", 1)[0].replace("''", "'")
            if endpoint.endswith("/Files"):
                body = {"value": [meta(p) for p in blobs if p.rsplit("/", 1)[0] == path]}
                if state["next"]:
                    body["odata.nextLink"] = state["next"]
            elif endpoint.endswith("/Folders"):
                children = {p[len(path) + 1:].split("/")[0] for p in blobs if p.startswith(path + "/") and "/" in p[len(path) + 1:]}
                body = {"value": [{"Name": c, "ServerRelativeUrl": path + "/" + c} for c in children]}
            elif path in blobs:
                body = blobs[path] if endpoint.endswith("/$value") else meta(path)
            else:
                status = 404
        else:
            raise AssertionError(f"Unexpected REST endpoint: {endpoint}")
        response = MagicMock()
        response.status_code = status
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        response.headers = {"Content-Length": str(len(content))}
        response.iter_content.return_value = [content]
        response.__enter__.return_value = response
        return response

    monkeypatch.setattr(requests.Session, "get", respond)
    return state


def client(**kwargs):
    return SharepointOnpremClient(site_url=SITE, username="DOMAIN\\reader", password="unit-only", **kwargs)


def test_onprem_is_a_distinct_file_connector():
    entry = REGISTRY["sharepoint_onprem"]
    assert entry.data_shape == "files"
    assert entry.credentials_auth.default == "ntlm"
    assert "kerberos" in entry.credentials_auth.by_auth


@pytest.mark.parametrize("library,recursive,count", [("*", False, 3), ("*", True, 4), ("Documents", True, 3), ("", False, 2)])
def test_library_selection_and_recursion(remote, library, recursive, count):
    files = client(drive_name=library, recursive=recursive).list_files()
    assert len(files) == count
    assert len({f["id"] for f in files}) == count
    assert all(f["name"] and f["web_url"].startswith(SITE) for f in files)


def test_scoped_catalog_and_live_reads_share_paths(remote):
    c = client(recursive=True, include_globs="Documents/**/*.csv")
    tables = c.get_schemas()
    assert len(tables) == 1
    assert tables[0].name == "Documents/revenue.csv"
    frame = c.execute_query(table_name=tables[0].name)
    assert frame.shape == (2, 2)
    assert pd.to_numeric(frame["amount"]).sum() == 200


@pytest.mark.parametrize("path", [OTHER + "/rules.txt", ROOT + "/../Policies/rules.txt", ROOT + "/%2e%2e/rules.txt", ROOT + "/%252e%252e/rules.txt", "//evil.example/x", ROOT + "\\rules.txt"])
@pytest.mark.parametrize("method", ["read_file", "read_raw_bytes"])
def test_all_reads_reject_scope_escape_before_download(remote, path, method):
    c = client(drive_name="Documents", include_globs="**/*.csv")
    with pytest.raises(GlobScopeError):
        getattr(c, method)(path)
    assert not any("$value" in url for url, _ in remote["calls"])


def test_folder_and_extension_scope_is_enforced_on_direct_ids(remote):
    c = client(drive_name="Documents", folder_path="nested", allowed_extensions="txt")
    assert len(c.list_files()) == 1
    with pytest.raises(GlobScopeError):
        c.read_raw_bytes(ROOT + "/revenue.csv")
    with pytest.raises(GlobScopeError):
        c.list_files(folder_id=OTHER)


def test_special_character_file_names_roundtrip(remote):
    c = client(drive_name="Documents")
    path = ROOT + "/O'Brien #100%.txt"
    assert c.read_file(path) == remote["blobs"][path].decode()


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_global_cap_is_respected_across_libraries(remote, limit):
    assert len(client(recursive=True).list_files(limit=limit)) == limit


def test_no_index_means_no_catalog_network_calls(remote):
    assert client(index_mode="none").get_schemas() == []
    assert remote["calls"] == []


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
def test_provider_errors_are_not_successful_empty_catalogs(remote, status):
    remote["status"] = status
    c = client()
    assert c.test_connection()["success"] is False
    with pytest.raises(ValueError):
        c.list_files()


@pytest.mark.parametrize("url", ["https://evil.example/_api/web", "http://portal.example.com/sites/research/_api/web", SITE + "/../private/_api/web", SITE + "/_api/web/../../private"])
def test_untrusted_pagination_cannot_receive_credentials(remote, url):
    remote["next"] = url
    with pytest.raises(ValueError):
        client().list_files()
    assert all(request_url.startswith(SITE + "/_api/") for request_url, _ in remote["calls"])


def test_download_limit_rejects_instead_of_corrupting_documents(remote):
    with pytest.raises(ValueError, match="limit"):
        client().read_file(ROOT + "/revenue.csv", max_bytes=3)
    assert not any("$value" in url for url, _ in remote["calls"])


def test_content_search_revalidates_site_scope_and_adds_live_names(remote):
    remote["search_paths"] = ["https://portal.example.com" + ROOT + "/nested/notes.txt", "https://evil.example/secret.txt", "https://portal.example.com" + OTHER + "/rules.txt"]
    hits = client(drive_name="Documents", recursive=True).search_files("revenue")
    assert {f["name"] for f in hits} == {"notes.txt", "revenue.csv"}


def test_pdf_page_range_and_raw_fallback(remote):
    writer, buf = PdfWriter(), io.BytesIO()
    writer.add_blank_page(width=300, height=300)
    writer.add_blank_page(width=300, height=300)
    writer.write(buf)
    path = ROOT + "/scan.pdf"
    remote["blobs"][path] = buf.getvalue()
    c = client()
    assert isinstance(c.read_file(path), NamedBytes)
    result = c.read_file(path, page_range=(2, 2))
    assert result["pages_total"] == 2 and result["first"] == result["last"] == 2
    assert result["raw"] == buf.getvalue()


def test_excel_sheet_selection(remote):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as writer:
        pd.DataFrame({"value": [1]}).to_excel(writer, sheet_name="First", index=False)
        pd.DataFrame({"value": [7, 9]}).to_excel(writer, sheet_name="Second", index=False)
    path = ROOT + "/book.xlsx"
    remote["blobs"][path] = buf.getvalue()
    assert client().read_file(path, sheet="Second")["value"].tolist() == [7, 9]


@pytest.mark.parametrize("site", ["http://portal.example.com", "https://user:pass@portal.example.com", "https://portal.example.com?token=secret", "file:///etc/passwd"])
def test_invalid_or_insecure_site_rejected(site):
    with pytest.raises(ValueError):
        SharepointOnpremClient(site_url=site, username="reader", password="test")


def test_kerberos_uses_explicit_credentials_and_no_ntlm_fallback(remote, monkeypatch):
    acquired, auths = [], []
    class Creds:
        lifetime = 3600
        def __init__(self, **kwargs):
            acquired.append(kwargs)
    def auth(**kwargs):
        auths.append(kwargs)
        return object()
    fake = types.SimpleNamespace(Credentials=Creds, Name=lambda name, kind: name,
        NameType=types.SimpleNamespace(kerberos_principal="principal"),
        MechType=types.SimpleNamespace(kerberos="kerberos-only"))
    monkeypatch.setitem(sys.modules, "gssapi", fake)
    monkeypatch.setitem(sys.modules, "requests_gssapi", types.SimpleNamespace(HTTPSPNEGOAuth=auth, REQUIRED="required"))
    monkeypatch.setenv("KRB5_CLIENT_KTNAME", "/run/secrets/sharepoint.keytab")
    monkeypatch.setenv("KRB5CCNAME", "FILE:/existing-user-cache")
    c = SharepointOnpremClient(site_url=SITE, kerberos=True, principal="svc@EXAMPLE.COM")
    assert c.test_connection()["success"]
    assert acquired[0] == {"usage": "initiate", "name": "svc@EXAMPLE.COM", "store": {"client_keytab": "/run/secrets/sharepoint.keytab"}}
    assert auths[0]["mech"] == "kerberos-only"
    assert auths[0]["mutual_authentication"] == "required"
    import os
    assert os.environ["KRB5CCNAME"] == "FILE:/existing-user-cache"
    # Expiry must acquire a new credential, not repeatedly reuse an expired one.
    Creds.lifetime = 30
    c.test_connection()
    assert len(acquired) > 1


def test_missing_kerberos_dependency_fails_closed(monkeypatch):
    monkeypatch.setitem(sys.modules, "requests_gssapi", None)
    c = SharepointOnpremClient(site_url=SITE, kerberos=True)
    assert c.test_connection()["success"] is False


def test_transient_read_failure_retries_a_fresh_session(remote, monkeypatch):
    original = requests.Session.get
    sessions = []
    def fail_once(session, *args, **kwargs):
        sessions.append(session)
        if len(sessions) == 1:
            raise requests.ConnectionError("closed keep-alive")
        return original(session, *args, **kwargs)
    monkeypatch.setattr(requests.Session, "get", fail_once)
    assert client().test_connection()["success"]
    assert sessions[0] is not sessions[1]
