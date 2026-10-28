"""Google Drive shared mode: one service account for everyone on the connection.

The contract:
  - a service-account key authenticates every Drive call on its own (no user
    token), and only a well-formed key with Google's token endpoint is taken;
  - with no folder configured, the catalog is what was shared with the service
    account (sharedWithMe) plus the Shared Drives it belongs to, each file once;
  - the variant is "shared": admin-side catalog, no per-user sign-in — while a
    Drive connection on the OAuth client stays per-user.

Google is faked at the HTTP boundary (`FakeDrive`) and the token endpoint by
stubbing the credential refresh; the client and registry run for real.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from app.data_sources.clients import google_drive_client as gdc
from app.data_sources.clients.google_drive_client import GoogleDriveClient

SA_TOKEN = "sa-access-token"


def service_account_key(**overrides) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode()
    info = {
        "type": "service_account",
        "project_id": "proj",
        "private_key_id": "kid",
        "private_key": pem,
        "client_email": "bow-reader@proj.iam.gserviceaccount.com",
        "client_id": "123",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    info.update(overrides)
    return json.dumps(info)


class _Resp:
    def __init__(self, status: int, body=None, content: bytes = b""):
        self.status_code = status
        self._body = body
        self.content = content
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        return self._body


class FakeDrive:
    """Just enough of Drive v3 for listing: `files` answers by its `q`,
    `drives` lists Shared Drives, `about` names the caller. Records the bearer
    token of every call so tests can check who the client authenticated as."""

    def __init__(self, *, shared_with_me, folders, shared_drives=(), blobs=None):
        self.shared_with_me = list(shared_with_me)
        self.folders = {k: list(v) for k, v in folders.items()}
        self.shared_drives = list(shared_drives)
        self.blobs = blobs or {}
        self.tokens: set[str] = set()

    def _record(self, headers):
        self.tokens.add((headers or {}).get("Authorization", ""))

    def get(self, url, headers=None, params=None, **_):
        self._record(headers)
        params = params or {}
        if url.endswith("/about"):
            return _Resp(200, {"user": {"emailAddress": "bow-reader@proj.iam.gserviceaccount.com"}})
        if url.endswith("/drives"):
            return _Resp(200, {"drives": self.shared_drives})
        if url.endswith("/files"):
            q = params.get("q", "")
            if "sharedWithMe" in q:
                return _Resp(200, {"files": self.shared_with_me})
            m = re.match(r"'([^']+)' in parents", q)
            if m:
                return _Resp(200, {"files": self.folders.get(m.group(1), [])})
            return _Resp(200, {"files": []})
        m = re.search(r"/files/([^/?]+)$", url)
        if m:
            fid = unquote(m.group(1))
            if params.get("alt") == "media":
                return _Resp(200, content=self.blobs[fid])
            for entry in self.shared_with_me + [e for v in self.folders.values() for e in v]:
                if entry["id"] == fid:
                    return _Resp(200, {"id": fid, "name": entry["name"], "mimeType": entry["mimeType"], "size": str(len(self.blobs.get(fid, b"")))})
            return _Resp(404, {"error": "not found"})
        return _Resp(404, {"error": f"unexpected {url}"})

    def install(self, monkeypatch):
        fake = self

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, headers=None, params=None, **_):
                return fake.get(url, headers=headers, params=params)

        monkeypatch.setattr(gdc, "httpx", SimpleNamespace(get=self.get, Client=_Client))

        def refresh(creds, request):
            creds.token = SA_TOKEN
        monkeypatch.setattr("google.oauth2.service_account.Credentials.refresh", refresh)
        return self


def _file(fid, name, mime="text/csv"):
    return {"id": fid, "name": name, "mimeType": mime}


def _folder(fid, name):
    return {"id": fid, "name": name, "mimeType": gdc.GOOGLE_FOLDER_MIME}


def sample_drive() -> FakeDrive:
    """A folder and a loose file shared with the service account, a Shared
    Drive it belongs to, and one file reachable two ways (shared directly AND
    inside the shared folder)."""
    return FakeDrive(
        shared_with_me=[_file("d1", "direct.csv"), _folder("F1", "Finance")],
        folders={
            "F1": [_file("b1", "budget.csv"), _file("d1", "direct.csv")],
            "SD1": [_file("r1", "report.csv")],
        },
        shared_drives=[{"id": "SD1", "name": "Team"}],
        blobs={"d1": b"a,b\n1,2\n", "b1": b"x,y\n3,4\n", "r1": b"k,v\n5,6\n"},
    )


# ------------------------------------------------------------------ client


def test_service_account_lists_what_was_shared_with_it(monkeypatch):
    drive = sample_drive().install(monkeypatch)
    client = GoogleDriveClient(service_account_json=service_account_key())

    files = client.list_files()

    assert sorted(f["path"] for f in files) == ["Finance/budget.csv", "Team/report.csv", "direct.csv"]
    ids = [f["id"] for f in files]
    assert len(ids) == len(set(ids)), "a file shared two ways must be listed once"
    assert drive.tokens == {f"Bearer {SA_TOKEN}"}


def test_service_account_reads_a_file_and_passes_the_connection_test(monkeypatch):
    sample_drive().install(monkeypatch)
    client = GoogleDriveClient(service_account_json=service_account_key())

    assert client.test_connection()["success"] is True
    df = client.read_file("b1")
    cells = {str(v) for v in df.astype(str).values.ravel()}
    assert {"3", "4"} <= cells  # the file's data, read with the service-account token


def test_configured_folder_scopes_a_service_account_to_that_folder(monkeypatch):
    sample_drive().install(monkeypatch)
    client = GoogleDriveClient(service_account_json=service_account_key(), folder_id="SD1")

    assert [f["path"] for f in client.list_files()] == ["report.csv"]


def test_user_token_wins_over_a_service_account_key(monkeypatch):
    drive = sample_drive().install(monkeypatch)
    client = GoogleDriveClient(access_token="user-token", service_account_json=service_account_key())

    client.list_files()

    assert drive.tokens == {"Bearer user-token"}


@pytest.mark.parametrize("bad", [
    "not json",
    json.dumps({"type": "authorized_user", "client_id": "x"}),
])
def test_malformed_service_account_key_is_rejected(bad):
    with pytest.raises(ValueError):
        GoogleDriveClient(service_account_json=bad)


def test_service_account_key_must_use_googles_token_endpoint():
    from app.core.url_safety import UnsafeURLError

    with pytest.raises(UnsafeURLError):
        GoogleDriveClient(service_account_json=service_account_key(token_uri="https://attacker.example/token"))


# ---------------------------------------------------------------- registry


def test_service_account_variant_is_shared_and_admin_only():
    from app.schemas.data_source_registry import REGISTRY, catalog_ownership_for

    variant = REGISTRY["google_drive"].credentials_auth.by_auth["service_account"]
    assert variant.scopes == ["system"]
    assert catalog_ownership_for("google_drive", {"auth_type": "service_account"}) == "shared"
    # The serialized config a Connection row carries resolves the same way.
    assert catalog_ownership_for("google_drive", json.dumps({"auth_type": "service_account"})) == "shared"


@pytest.mark.parametrize("config", [{}, {"auth_type": "oauth_app"}, None])
def test_drive_on_the_oauth_client_stays_per_user(config):
    from app.schemas.data_source_registry import catalog_ownership_for

    assert catalog_ownership_for("google_drive", config) == "per_user"


def test_only_the_service_account_variant_runs_without_a_signed_in_user():
    from app.services.connection_identity import management_requires_user_auth

    def conn(config):
        return SimpleNamespace(type="google_drive", config=json.dumps(config))

    assert management_requires_user_auth(conn({"auth_type": "service_account"})) is False
    assert management_requires_user_auth(conn({})) is True
    assert management_requires_user_auth(conn({"auth_type": "oauth_app"})) is True
