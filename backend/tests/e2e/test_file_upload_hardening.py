"""Uploaded files stay inside the uploads store and never execute on the app origin.

Contract:
  * Whatever filename a client sends, the stored bytes land directly inside
    uploads/files — a name with path separators or `..` cannot reach any
    other directory.
  * The token-gated embed endpoint (served on the app's own origin) renders
    only passive types inline; anything a browser could execute is forced to
    download, and every response carries nosniff + a sandbox CSP.
"""
import os
import uuid

import pytest


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def member(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    return token, whoami(token)["organizations"][0]["id"]


def _upload(client, token, org_id, name, body, content_type):
    r = client.post("/api/files", files={"file": (name, body, content_type)}, headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    return r.json()


def _embed(client, token, org_id, file_id):
    mint = client.get(f"/api/files/{file_id}/embed_token", headers=_h(token, org_id))
    assert mint.status_code == 200, mint.text
    return client.get(f"/api/files/{file_id}/embed", params={"token": mint.json()["token"]})


@pytest.mark.e2e
@pytest.mark.parametrize("prefix", ["../", "../../", "../../../../", "..\\..\\", "/abs/", "sub/dir/"])
def test_upload_filename_cannot_escape_the_uploads_store(test_client, member, prefix):
    token, org_id = member
    store = os.path.realpath(os.path.join(os.getcwd(), "uploads", "files"))
    leaf = f"probe-{uuid.uuid4().hex}.csv"
    body = f"a,b\n1,{uuid.uuid4().int % 1000}\n".encode()

    up = _upload(test_client, token, org_id, prefix + leaf, body, "text/csv")

    # Nothing named after the leaf exists anywhere outside the store.
    root = os.path.realpath(os.path.join(os.getcwd(), ".."))
    for dirpath, _dirs, files in os.walk(root):
        if os.path.realpath(dirpath) == store or ".git" in dirpath or "node_modules" in dirpath:
            continue
        assert not any(leaf in f for f in files), f"upload escaped into {dirpath}"

    # It is stored under a fresh uuid-prefixed name, never at a name the
    # client chose — otherwise `../../x` overwrites another stored upload.
    assert any(f.endswith("_" + leaf) for f in os.listdir(store)), os.listdir(store)

    # And the file is readable back with its bytes intact.
    content = test_client.get(f"/api/files/{up['id']}/content", headers=_h(token, org_id))
    assert content.status_code == 200
    assert content.content == body


@pytest.mark.e2e
@pytest.mark.parametrize("name,ctype", [
    ("x.html", "text/html"),
    ("x.svg", "image/svg+xml"),
    ("x.xhtml", "application/xhtml+xml"),
    ("x.js", "application/javascript"),
    ("x.bin", "application/octet-stream"),
])
def test_embed_never_renders_executable_types_inline(test_client, member, name, ctype):
    token, org_id = member
    up = _upload(test_client, token, org_id, name, b"<script>alert(document.domain)</script>", ctype)
    r = _embed(test_client, token, org_id, up["id"])
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert "sandbox" in r.headers.get("content-security-policy", "")


@pytest.mark.e2e
@pytest.mark.parametrize("name,ctype", [("x.png", "image/png"), ("x.pdf", "application/pdf"), ("x.csv", "text/csv")])
def test_embed_still_renders_passive_types_inline(test_client, member, name, ctype):
    token, org_id = member
    up = _upload(test_client, token, org_id, name, b"\x89PNG\r\n\x1a\n" + uuid.uuid4().bytes, ctype)
    r = _embed(test_client, token, org_id, up["id"])
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("inline")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert "script-src 'none'" in r.headers.get("content-security-policy", "")
