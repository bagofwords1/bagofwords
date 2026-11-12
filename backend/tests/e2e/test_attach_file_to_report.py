"""POST /files/attach — the prompt-box picker's "attach without re-uploading"
endpoint.

Two shapes:
  - {file_id}: link a file the user already uploaded (no second copy, no second
    upload; another user's file id is a 404, never attached).
  - {data_source_id, connection_id, ref}: copy a picked Drive/OneDrive file in
    as a normal upload — it must land as source_kind='upload' (so the prompt box
    shows it as a chip) while recording where it came from, and it must honor
    the connection's size cap and sign-in state.

`report_id` is optional (omitted on the home screen, before a report exists).
The connection path mocks the external driver at the client boundary (the same
boundary the browse/search tests mock); everything else runs real.
"""
import asyncio

import pytest
from fastapi import HTTPException

from tests.e2e.test_connection_file_browse import _agent_on_dir, _serve
from tests.e2e.rbac.conftest import invite_user_to_org as invite_user_to_org  # noqa: F401


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _file_row(file_id):
    """Read origin columns the API doesn't expose (source_connection_id /
    source_ref) straight from the DB to pin the bridge's bookkeeping."""
    from app.dependencies import async_session_maker
    from app.models.file import File

    async def _go():
        async with async_session_maker() as db:
            f = await db.get(File, file_id)
            return None if f is None else {
                "source_kind": f.source_kind,
                "source_connection_id": f.source_connection_id,
                "source_ref": f.source_ref,
                "filename": f.filename,
            }
    return asyncio.run(_go())


class _OneFileClient:
    """A connection client that hands back one file's original bytes."""

    def __init__(self, content=b"region,amount\nNA,10\n", name="picked.csv", mime="text/csv", raiser=None):
        self._content, self._name, self._mime, self._raiser = content, name, mime, raiser

    def read_raw_bytes(self, file_id, *, max_bytes=None):
        if self._raiser is not None:
            raise self._raiser
        return self._content, self._name, self._mime


# --- Path 1: attach a file the user already owns -----------------------------


@pytest.fixture
def owner(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return token, org_id


@pytest.mark.e2e
def test_attach_existing_file_links_without_re_uploading(
    owner, test_client, upload_csv_file, create_report,
):
    token, org_id = owner
    f = upload_csv_file(user_token=token, org_id=org_id, filename="keep.csv")
    report = create_report(title="R", user_token=token, org_id=org_id)

    r = test_client.post("/api/files/attach",
                        json={"file_id": f["id"], "report_id": report["id"]}, headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    # Same file, not a new copy.
    assert r.json()["id"] == f["id"]

    listed = test_client.get(f"/api/reports/{report['id']}/files", headers=_h(token, org_id)).json()
    assert [x["id"] for x in listed].count(f["id"]) == 1

    # Idempotent: attaching again doesn't duplicate the association.
    test_client.post("/api/files/attach",
                     json={"file_id": f["id"], "report_id": report["id"]}, headers=_h(token, org_id))
    listed = test_client.get(f"/api/reports/{report['id']}/files", headers=_h(token, org_id)).json()
    assert [x["id"] for x in listed].count(f["id"]) == 1


@pytest.mark.e2e
def test_attach_existing_file_without_a_report_just_returns_the_chip(owner, test_client, upload_csv_file):
    """On the home screen there is no report yet — attach validates ownership
    and hands the file back so the composer can show it as a chip."""
    token, org_id = owner
    f = upload_csv_file(user_token=token, org_id=org_id, filename="home.csv")
    r = test_client.post("/api/files/attach", json={"file_id": f["id"]}, headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    assert r.json()["id"] == f["id"]


@pytest.mark.e2e
def test_cannot_attach_another_users_file(
    owner, test_client, upload_csv_file, create_report, invite_user_to_org,
):
    token, org_id = owner
    report = create_report(title="R", user_token=token, org_id=org_id)
    member = invite_user_to_org(org_id=org_id, admin_token=token)
    theirs = upload_csv_file(user_token=member["token"], org_id=org_id, filename="theirs.csv")

    r = test_client.post("/api/files/attach",
                        json={"file_id": theirs["id"], "report_id": report["id"]}, headers=_h(token, org_id))
    assert r.status_code == 404, r.text
    listed = test_client.get(f"/api/reports/{report['id']}/files", headers=_h(token, org_id)).json()
    assert all(x["id"] != theirs["id"] for x in listed)


@pytest.mark.e2e
def test_attach_with_neither_shape_is_a_400(owner, test_client):
    token, org_id = owner
    r = test_client.post("/api/files/attach", json={}, headers=_h(token, org_id))
    assert r.status_code == 400, r.text


# --- Path 2: copy a picked Drive/OneDrive file in ----------------------------


@pytest.fixture
def connection(tmp_path, test_client, create_user, login_user, whoami, create_data_source):
    listing, headers = _agent_on_dir(tmp_path, create_user, login_user, whoami, create_data_source, test_client)
    # listing == /api/data_sources/{ds}/connections/{conn}/files
    parts = listing.split("/")
    ds_id, conn_id = parts[parts.index("data_sources") + 1], parts[parts.index("connections") + 1]
    return ds_id, conn_id, headers


@pytest.mark.e2e
def test_attach_picked_drive_file_copies_it_in_as_an_upload(
    connection, test_client, monkeypatch, create_report,
):
    ds_id, conn_id, headers = connection
    token = headers["Authorization"].split()[1]
    org_id = headers["X-Organization-Id"]
    report = create_report(title="R", user_token=token, org_id=org_id)
    _serve(monkeypatch, lambda: _OneFileClient(name="budget.xlsx", mime="application/vnd.ms-excel"))

    r = test_client.post(
        "/api/files/attach",
        json={"data_source_id": ds_id, "connection_id": conn_id, "ref": "drive-file-123",
              "report_id": report["id"]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filename"] == "budget.xlsx"

    # It shows as a normal upload chip, and records where it came from.
    row = _file_row(body["id"])
    assert row["source_kind"] == "upload"
    assert row["source_connection_id"] == str(conn_id)
    assert row["source_ref"] == "drive-file-123"

    listed = test_client.get(f"/api/reports/{report['id']}/files", headers=headers).json()
    assert any(x["id"] == body["id"] for x in listed)


@pytest.mark.e2e
def test_attach_rejects_an_oversize_drive_file(connection, test_client, monkeypatch):
    from app.data_sources.clients._file_source_common import FileTooLargeError
    ds_id, conn_id, headers = connection
    _serve(monkeypatch, lambda: _OneFileClient(raiser=FileTooLargeError("too big")))

    r = test_client.post(
        "/api/files/attach",
        json={"data_source_id": ds_id, "connection_id": conn_id, "ref": "huge"},
        headers=headers,
    )
    assert r.status_code == 413, r.text


@pytest.mark.e2e
def test_attach_from_unlinked_drive_surfaces_the_connect_state(connection, test_client, monkeypatch):
    ds_id, conn_id, headers = connection
    from app.services.connection_service import ConnectionService

    async def not_connected(self, *a, **k):
        raise HTTPException(status_code=403, detail="Connect your account to use this connection")
    monkeypatch.setattr(ConnectionService, "construct_client", not_connected)

    r = test_client.post(
        "/api/files/attach",
        json={"data_source_id": ds_id, "connection_id": conn_id, "ref": "x"},
        headers=headers,
    )
    assert r.status_code == 403, r.text
