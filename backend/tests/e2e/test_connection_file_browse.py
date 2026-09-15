"""The agent file browser's listing endpoint.

GET /data_sources/{id}/connections/{cid}/files is what the Files tab builds its
folder tree from: connectors list files only (never folders), so the tree is
rebuilt client-side from each file's `path`. These tests pin the contract the
browser depends on — `path` is returned and scope-relative, off-scope files
never appear, and a whole scope comes back in one call instead of 500-row pages
that would each re-walk the source.
"""
import pytest


def _agent_on_dir(tmp_path, create_user, login_user, whoami, create_data_source, test_client, **config):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}
    ds = create_data_source(
        name="Shared drive", type="network_dir",
        config={"root_path": str(tmp_path), **config},
        credentials={"auth_type": "none"}, user_token=token, org_id=org_id,
    )
    conns = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=headers).json()
    return f"/api/data_sources/{ds['id']}/connections/{conns[0]['id']}/files", headers


@pytest.mark.e2e
def test_browse_returns_scope_relative_paths_and_hides_off_scope_files(
    tmp_path, test_client, create_user, login_user, whoami, create_data_source,
):
    (tmp_path / "Finance" / "2025").mkdir(parents=True)
    (tmp_path / "Finance" / "2025" / "Q2.csv").write_text("a,b\n1,2\n")
    (tmp_path / "Finance" / "budget.csv").write_text("a,b\n1,2\n")
    (tmp_path / "Legal").mkdir()
    (tmp_path / "Legal" / "contract.txt").write_text("confidential")
    (tmp_path / "readme.txt").write_text("hi")

    endpoint, headers = _agent_on_dir(
        tmp_path, create_user, login_user, whoami, create_data_source, test_client,
        include_globs="Finance/**",
    )
    r = test_client.get(endpoint, params={"limit": 5000}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()

    assert sorted(f["path"] for f in body["files"]) == ["Finance/2025/Q2.csv", "Finance/budget.csv"]
    assert body["total"] == 2 and body["has_more"] is False
    for f in body["files"]:
        assert f["name"] == f["path"].rsplit("/", 1)[-1]
        assert "web_url" in f


@pytest.mark.e2e
def test_browse_returns_more_than_500_files_in_one_call(
    tmp_path, test_client, create_user, login_user, whoami, create_data_source,
):
    for i in range(3):
        folder = tmp_path / f"batch-{i}"
        folder.mkdir()
        for j in range(210):
            (folder / f"f{j:03d}.txt").write_text("x")

    endpoint, headers = _agent_on_dir(
        tmp_path, create_user, login_user, whoami, create_data_source, test_client,
    )
    body = test_client.get(endpoint, params={"limit": 5000}, headers=headers).json()
    assert body["total"] == 630
    assert len(body["files"]) == 630
    assert body["has_more"] is False


# --- file preview (…/files/content) -------------------------------------------


@pytest.fixture
def preview_agent(tmp_path, test_client, create_user, login_user, whoami, create_data_source):
    import pandas as pd

    (tmp_path / "Finance").mkdir()
    (tmp_path / "Finance" / "notes.txt").write_text("שלום — quarterly notes", encoding="utf-8")
    (tmp_path / "Finance" / "sales.csv").write_text(
        "region,amount\n" + "".join(f"r{i},{i}\n" for i in range(250)), encoding="utf-8",
    )
    # A real-world sheet: a blank leading row and column before the table, and
    # a second sheet.
    with pd.ExcelWriter(tmp_path / "Finance" / "book.xlsx") as xw:
        pd.DataFrame([[None, None, None], [None, "name", "qty"], [None, "apple", 3], [None, "pear", 5]]) \
            .to_excel(xw, sheet_name="Fruit", header=False, index=False)
        pd.DataFrame([["city"], ["Haifa"]]).to_excel(xw, sheet_name="Cities", header=False, index=False)
    (tmp_path / "Legal").mkdir()
    (tmp_path / "Legal" / "secret.txt").write_text("off-scope")
    (tmp_path.parent / f"outside-{tmp_path.name}.txt").write_text("outside the root")

    listing, headers = _agent_on_dir(
        tmp_path, create_user, login_user, whoami, create_data_source, test_client,
        include_globs="Finance/**",
    )
    return listing + "/content", headers, tmp_path


@pytest.mark.e2e
def test_preview_serves_raw_bytes_as_an_attachment(preview_agent, test_client):
    content, headers, _ = preview_agent
    r = test_client.get(content, params={"file_id": "Finance/notes.txt"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.content.decode("utf-8") == "שלום — quarterly notes"
    # Untrusted source bytes must never render on our origin if the URL is
    # opened directly — the browser builds its own typed blob instead.
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "sandbox"


def _audit_rows(org_id: str, action: str) -> list:
    import asyncio
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.ee.audit.models import AuditLog

    async def fetch():
        async with async_session_maker() as db:
            rows = (await db.execute(select(AuditLog).where(
                AuditLog.organization_id == org_id, AuditLog.action == action,
            ))).scalars().all()
            return [r.details or {} for r in rows]
    return asyncio.run(fetch())


@pytest.mark.e2e
def test_preview_denies_and_audits_off_glob_and_escaping_paths(preview_agent, test_client):
    content, headers, tmp_path = preview_agent
    escape = f"../outside-{tmp_path.name}.txt"
    for file_id, secret in (("Legal/secret.txt", "off-scope"), (escape, "outside the root")):
        r = test_client.get(content, params={"file_id": file_id}, headers=headers)
        assert r.status_code == 403, r.text
        assert secret not in r.text
        # The requested path is not echoed back — least of all a traversal.
        assert file_id not in r.text

    # Both shapes of denial land in the trail, the hostile one included (it
    # used to be a plain ValueError → 400 with no audit row).
    denied = {d.get("file_id") for d in _audit_rows(headers["X-Organization-Id"], "file.access_denied")}
    assert denied == {"Legal/secret.txt", escape}


@pytest.mark.e2e
def test_preview_rejects_oversize_files_before_reading_them(preview_agent, test_client, monkeypatch):
    content, headers, tmp_path = preview_agent
    with open(tmp_path / "Finance" / "huge.csv", "wb") as f:
        f.truncate(26 * 1024 * 1024)  # sparse: 26 MB reported, nothing on disk

    import pathlib
    real_read = pathlib.Path.read_bytes

    def guarded(self):
        if self.name == "huge.csv":
            raise AssertionError("an oversize file must be rejected from its size, not read")
        return real_read(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", guarded)
    r = test_client.get(content, params={"file_id": "Finance/huge.csv"}, headers=headers)
    assert r.status_code == 413, r.text


@pytest.mark.e2e
def test_listing_echoes_the_effective_limit_and_preview_support(preview_agent, test_client):
    content, headers, _ = preview_agent
    listing = content.rsplit("/content", 1)[0]
    body = test_client.get(listing, params={"limit": 100000}, headers=headers).json()
    assert body["limit"] == 5000, "the response must say what was actually applied"
    assert body["preview_supported"] is True


@pytest.mark.e2e
def test_preview_table_for_csv_caps_rows_but_reports_the_real_count(preview_agent, test_client):
    content, headers, _ = preview_agent
    r = test_client.get(content, params={"file_id": "Finance/sales.csv", "format": "table"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    lines = body["csv"].strip().splitlines()
    assert lines[0] == "region,amount"
    assert len(lines) == 1 + 200
    assert body["row_count"] == 250
    assert body["col_count"] == 2
    assert body["sheets"] == [] and body["sheet"] is None


@pytest.mark.e2e
def test_preview_table_for_excel_trims_blanks_and_switches_sheets(preview_agent, test_client):
    content, headers, _ = preview_agent
    body = test_client.get(content, params={"file_id": "Finance/book.xlsx", "format": "table"}, headers=headers).json()
    assert body["sheets"] == ["Fruit", "Cities"]
    assert body["sheet"] == "Fruit"
    assert body["csv"].strip().splitlines() == ["name,qty", "apple,3", "pear,5"]
    assert body["row_count"] == 2

    body = test_client.get(
        content, params={"file_id": "Finance/book.xlsx", "format": "table", "sheet": "Cities"}, headers=headers,
    ).json()
    assert body["sheet"] == "Cities"
    assert body["csv"].strip().splitlines() == ["city", "Haifa"]


@pytest.mark.e2e
def test_preview_table_rejects_non_tabular_files(preview_agent, test_client):
    content, headers, _ = preview_agent
    r = test_client.get(content, params={"file_id": "Finance/notes.txt", "format": "table"}, headers=headers)
    assert r.status_code == 400


@pytest.mark.e2e
def test_preview_converts_office_documents_to_pdf(preview_agent, test_client, monkeypatch):
    content, headers, tmp_path = preview_agent
    (tmp_path / "Finance" / "memo.docx").write_bytes(b"PK fake docx")
    seen = {}

    def fake_convert(data, name, **_):
        seen["args"] = (data, name)
        return b"%PDF-1.7 converted"

    # LibreOffice is a production-image dependency, not a test one.
    monkeypatch.setattr("app.data_sources.clients._office_convert.office_to_pdf_bytes", fake_convert)
    r = test_client.get(content, params={"file_id": "Finance/memo.docx", "format": "pdf"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.content == b"%PDF-1.7 converted"
    assert r.headers["content-type"] == "application/pdf"
    assert "memo.pdf" in r.headers["content-disposition"]
    assert seen["args"] == (b"PK fake docx", "memo.docx")

    # The converter can't run (no soffice) → a clear 422, not a 500.
    monkeypatch.setattr("app.data_sources.clients._office_convert.office_to_pdf_bytes", lambda *a, **k: None)
    r = test_client.get(content, params={"file_id": "Finance/memo.docx", "format": "pdf"}, headers=headers)
    assert r.status_code == 422

    # Only Office documents convert.
    r = test_client.get(content, params={"file_id": "Finance/notes.txt", "format": "pdf"}, headers=headers)
    assert r.status_code == 400


# --- the listing/preview contract, per connector -------------------------------
#
# Each connector derives `path` its own way (library-prefixed Graph paths, S3
# keys relative to the prefix, Documentum display paths unrelated to their
# r_object_id, SharePoint Server paths unrelated to their server-relative ids),
# and the folder tree is built from nothing else. So the contract is pinned
# per connector: scope-relative paths, nothing off-scope listed, an in-scope
# file previews byte-for-byte by the id the listing handed out, and an
# off-scope one is a 403. The route and each connector's client are real; only
# the remote each client talks to is simulated (the same fakes as its own
# unit tests). The agent the connection hangs off is an ordinary network_dir
# one — only the client construction is swapped.

from tests.unit.test_documentum_client import dctm, dctm_server  # noqa: E402,F401
from tests.unit.test_sharepoint_onprem_client import remote  # noqa: E402,F401


@pytest.fixture
def browse_agent(tmp_path, test_client, create_user, login_user, whoami, create_data_source):
    listing, headers = _agent_on_dir(tmp_path, create_user, login_user, whoami, create_data_source, test_client)
    return listing, listing + "/content", headers


def _serve(monkeypatch, make_client):
    """Every request builds a fresh client, as production does — so a client
    that only works after listing on the same instance fails here too."""
    from app.services.connection_service import ConnectionService

    async def construct(self, *args, **kwargs):
        return make_client()
    monkeypatch.setattr(ConnectionService, "construct_client", construct)


def _assert_contract(test_client, browse_agent, *, expected_paths, readable, denied):
    listing, content, headers = browse_agent
    body = test_client.get(listing, params={"limit": 5000}, headers=headers).json()
    assert sorted(f["path"] for f in body["files"]) == expected_paths
    for f in body["files"]:
        assert f["name"] == f["path"].rsplit("/", 1)[-1]
    assert body["preview_supported"] is True

    path, expected_bytes = readable
    file_id = next(f["id"] for f in body["files"] if f["path"] == path)
    r = test_client.get(content, params={"file_id": file_id}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.content == expected_bytes

    for file_id in denied:
        r = test_client.get(content, params={"file_id": file_id}, headers=headers)
        assert r.status_code == 403, (file_id, r.text)
    return body


@pytest.mark.e2e
def test_contract_sharepoint_all_libraries(browse_agent, test_client, monkeypatch):
    from tests.unit.test_graph_drive_multi_library import GRAPH, _client

    items = {
        "p1": ("drv-pol", "code_of_conduct.pdf", "/drives/drv-pol/root:", b"%PDF conduct"),
        "p2": ("drv-pol", "handbook.pdf", "/drives/drv-pol/root:/2026", b"%PDF handbook"),
        "d1": ("drv-docs", "budget.xlsx", "/drives/drv-docs/root:", b"PK budget"),
    }
    for item_id, (drive, name, parent, data) in items.items():
        monkeypatch.setitem(GRAPH, f"/drives/{drive}/items/{item_id}", {
            "id": item_id, "name": name, "size": len(data),
            "file": {"mimeType": "application/octet-stream"}, "parentReference": {"path": parent},
        })
    blobs = {f"/drives/{d}/items/{i}/content": data for i, (d, _, _, data) in items.items()}

    def make():
        c = _client("*", include_globs="Policies/**")
        c._get_bytes = lambda url, **_: blobs[url]
        return c

    _serve(monkeypatch, make)
    body = _assert_contract(
        test_client, browse_agent,
        # Library-prefixed: the tree's top level is the library.
        expected_paths=["Policies/2026/handbook.pdf", "Policies/code_of_conduct.pdf"],
        readable=("Policies/2026/handbook.pdf", b"%PDF handbook"),
        denied=["drv-docs|d1"],  # a real item, in another library, outside the glob
    )
    assert {f["id"] for f in body["files"]} == {"drv-pol|p1", "drv-pol|p2"}


@pytest.mark.e2e
def test_contract_s3(browse_agent, test_client, monkeypatch):
    from datetime import datetime, timezone
    from tests.unit.test_s3_client import _body, _make_client

    client, stub = _make_client(recursive=True, include_globs="reports/**")
    mod = datetime(2026, 1, 5, tzinfo=timezone.utc)
    stub.add_response("list_objects_v2", {
        "Contents": [
            {"Key": "docs/reports/q1.csv", "Size": 8, "LastModified": mod},
            {"Key": "docs/reports/2026/q2.csv", "Size": 8, "LastModified": mod},
            {"Key": "docs/secret.txt", "Size": 6, "LastModified": mod},
        ],
        "KeyCount": 3, "IsTruncated": False,
    }, {"Bucket": "test-bucket", "Prefix": "docs/"})
    stub.add_response("head_object", {"ContentLength": 8}, {"Bucket": "test-bucket", "Key": "docs/reports/q1.csv"})
    stub.add_response("get_object", {"Body": _body(b"a,b\n1,2\n"), "ContentLength": 8},
                      {"Bucket": "test-bucket", "Key": "docs/reports/q1.csv"})
    # The oversize object: a HEAD and nothing more — no get_object queued.
    stub.add_response("head_object", {"ContentLength": 30 * 1024 * 1024},
                      {"Bucket": "test-bucket", "Key": "docs/reports/2026/q2.csv"})
    stub.activate()
    try:
        _serve(monkeypatch, lambda: client)
        _assert_contract(
            test_client, browse_agent,
            expected_paths=["reports/2026/q2.csv", "reports/q1.csv"],
            readable=("reports/q1.csv", b"a,b\n1,2\n"),
            # Off-glob, and a key climbing out of the prefix — neither reaches S3.
            denied=["secret.txt", "../elsewhere/q9.csv"],
        )
        _, content, headers = browse_agent
        r = test_client.get(content, params={"file_id": "reports/2026/q2.csv"}, headers=headers)
        assert r.status_code == 413, r.text
        stub.assert_no_pending_responses()
    finally:
        stub.deactivate()


@pytest.mark.e2e
def test_contract_documentum(browse_agent, test_client, monkeypatch, dctm):
    from tests.unit.test_documentum_client import client as dctm_client

    unscoped = dctm_client(dctm, root_path="/Finance")
    ids = {f["path"]: f["id"] for f in unscoped.list_files()}
    q1 = unscoped.read_raw_bytes(ids["Reports/2026/Q1_revenue.csv"])[0]
    _serve(monkeypatch, lambda: dctm_client(dctm, root_path="/Finance", include_globs="**/*.csv"))
    body = _assert_contract(
        test_client, browse_agent,
        expected_paths=["Reports/2026/Q1_revenue.csv", "Reports/2026/Q2_revenue.csv"],
        readable=("Reports/2026/Q1_revenue.csv", q1),
        denied=[ids["Reports/2026/board_summary.txt"]],
    )
    # ids are r_object_ids, unrelated to the path the tree is built from.
    assert all("/" not in f["id"] for f in body["files"])


@pytest.mark.e2e
def test_contract_sharepoint_server(browse_agent, test_client, monkeypatch, remote):
    from tests.unit.test_sharepoint_onprem_client import OTHER, ROOT, client as onprem_client

    _serve(monkeypatch, lambda: onprem_client(recursive=True, include_globs="Documents/**"))
    body = _assert_contract(
        test_client, browse_agent,
        expected_paths=["Documents/O'Brien #100%.txt", "Documents/nested/notes.txt", "Documents/revenue.csv"],
        readable=("Documents/revenue.csv", remote["blobs"][ROOT + "/revenue.csv"]),
        denied=[OTHER + "/rules.txt"],  # the other library, by its real server-relative id
    )
    # ids are server-relative URLs; paths are library-prefixed and scope-relative.
    assert all(f["id"].startswith("/sites/research/") for f in body["files"])


@pytest.mark.e2e
def test_connections_without_original_bytes_offer_no_preview(browse_agent, test_client, monkeypatch):
    """Mail connectors list messages (path = subject) and have no raw reader —
    a "preview" would be a serialized message typed by its subject line."""

    class MailLike:
        async def alist_files(self, folder_id=None, recursive=False):
            return [{"id": "m1", "name": "Re: Q3 / Q4 forecast", "path": "Re: Q3 / Q4 forecast",
                     "mime_type": "message/rfc822"}]

    _serve(monkeypatch, MailLike)
    listing, content, headers = browse_agent
    body = test_client.get(listing, headers=headers).json()
    assert body["preview_supported"] is False
    assert body["files"][0]["path"] == "Re: Q3 / Q4 forecast"
    r = test_client.get(content, params={"file_id": "m1"}, headers=headers)
    assert r.status_code == 400
