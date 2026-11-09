"""With object storage on, any replica serves what another replica stored.

"Another replica" is simulated by deleting the local copy under uploads/ after
the write: the request then has to come back from the bucket/container.
Runs once per backend in tests/fixtures/object_storage.py (moto, Azurite, and
real clouds when their credentials are set).
"""
import io
import os
import uuid

import pytest
from PIL import Image

from app.services import storage


def _h(token, org_id=None):
    h = {"Authorization": f"Bearer {token}"}
    if org_id:
        h["X-Organization-Id"] = str(org_id)
    return h


def _png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def admin(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    return token, whoami(token)["organizations"][0]["id"]


def _local_upload_path(stored_basename: str) -> str:
    return os.path.join(os.getcwd(), "uploads", "files", stored_basename)


def _stored_name(test_client, token, org_id, file_id) -> str:
    rows = test_client.get("/api/files", headers=_h(token, org_id)).json()
    row = next(r for r in rows if r["id"] == file_id)
    return os.path.basename(row["path"])


@pytest.mark.e2e
@pytest.mark.parametrize("name,ctype", [("sales.csv", "text/csv"), ("notes.bin", "application/octet-stream")])
def test_uploaded_file_is_served_by_a_pod_that_never_saw_it(test_client, admin, object_store, name, ctype):
    token, org_id = admin
    body = (
        b"region,amount\nnorth,10\nsouth,%d\n" % (uuid.uuid4().int % 997)
        if name.endswith(".csv") else os.urandom(1500)
    )
    up = test_client.post("/api/files", files={"file": (name, body, ctype)}, headers=_h(token, org_id))
    assert up.status_code == 200, up.text
    stored = _stored_name(test_client, token, org_id, up.json()["id"])

    assert storage.key_for(_local_upload_path(stored)) in set(object_store.list("files/"))
    os.remove(_local_upload_path(stored))

    r = test_client.get(f"/api/files/{up.json()['id']}/content", headers=_h(token, org_id))
    assert r.status_code == 200
    assert r.content == body


class _UnreachableStore(storage.ObjectStore):
    name = "unreachable"

    def upload(self, key, local_path, content_type=None):
        raise storage.StorageError("connection refused")

    def download(self, key, local_path):
        raise storage.StorageError("connection refused")

    def etag(self, key):
        raise storage.StorageError("connection refused")

    def delete(self, key):
        raise storage.StorageError("connection refused")

    def list(self, prefix):
        raise storage.StorageError("connection refused")


@pytest.mark.e2e
def test_upload_fails_loudly_when_the_store_is_unreachable(test_client, admin):
    token, org_id = admin
    before = {r["id"] for r in test_client.get("/api/files", headers=_h(token, org_id)).json()}
    leaf = f"probe-{uuid.uuid4().hex}.csv"
    storage.set_store(_UnreachableStore())
    try:
        r = test_client.post("/api/files", files={"file": (leaf, b"a\n1\n", "text/csv")}, headers=_h(token, org_id))
    finally:
        storage.reset_store()

    assert r.status_code == 503
    after = test_client.get("/api/files", headers=_h(token, org_id)).json()
    assert {x["id"] for x in after} == before
    # No orphan bytes left behind on this pod either.
    assert not any(f.endswith(leaf) for f in os.listdir(os.path.join(os.getcwd(), "uploads", "files")))


@pytest.mark.e2e
def test_avatar_is_served_by_any_pod_and_removed_everywhere(test_client, admin, object_store):
    token, _ = admin
    up = test_client.post("/api/users/me/avatar",
                          files={"avatar": ("me.png", _png("teal"), "image/png")}, headers=_h(token))
    assert up.status_code == 200, up.text
    url = up.json()["image_url"]
    local = os.path.join(os.getcwd(), "uploads", "avatars", url.rsplit("/", 1)[-1])

    os.remove(local)
    r = test_client.get(url)
    assert r.status_code == 200
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    assert test_client.delete("/api/users/me/avatar", headers=_h(token)).status_code == 200
    assert storage.key_for(local) not in set(object_store.list("avatars/"))
    assert test_client.get(url).status_code == 404


@pytest.mark.e2e
def test_org_icon_is_served_by_any_pod_and_removed_everywhere(test_client, admin, object_store):
    token, org_id = admin
    up = test_client.post("/api/organization/general/icon",
                          files={"icon": ("logo.png", _png("navy"), "image/png")}, headers=_h(token, org_id))
    assert up.status_code == 200, up.text
    general = up.json()["config"]["general"]
    local = os.path.join(os.getcwd(), "uploads", "branding", general["icon_key"])

    os.remove(local)
    r = test_client.get(general["icon_url"])
    assert r.status_code == 200
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    assert test_client.delete("/api/organization/general/icon", headers=_h(token, org_id)).status_code == 200
    assert storage.key_for(local) not in set(object_store.list("branding/"))
    assert test_client.get(general["icon_url"]).status_code == 404
