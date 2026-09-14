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


@pytest.mark.e2e
def test_preview_denies_files_outside_the_scope(preview_agent, test_client):
    content, headers, tmp_path = preview_agent
    r = test_client.get(content, params={"file_id": "Legal/secret.txt"}, headers=headers)
    assert r.status_code == 403, r.text
    assert "off-scope" not in r.text

    r = test_client.get(content, params={"file_id": f"../outside-{tmp_path.name}.txt"}, headers=headers)
    assert r.status_code in (400, 403), r.text
    assert "outside the root" not in r.text


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
