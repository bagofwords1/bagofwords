"""Object storage contract: the bucket is the source of truth, uploads/ a cache.

Every backend (S3 via moto, Azure via Azurite, and real clouds when their
credentials are set — see tests/fixtures/object_storage.py) must give a pod
that never saw a file the same bytes the writing pod stored, must not serve a
copy that another pod replaced or deleted, and must leave the default local
backend behaving exactly as plain disk.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from app.services import storage
from app.services.storage import keys
from tests.fixtures.object_storage import make_object_store


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    """An isolated uploads/ root (both anchors: backend dir and cwd)."""
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setattr(keys, "UPLOADS_ROOT", root)
    monkeypatch.setattr(storage, "UPLOADS_ROOT", root)
    monkeypatch.chdir(tmp_path)
    return root


def _write(path, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def _read(path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _new_pod(path):
    """Simulate another replica: the file is not on this pod's disk."""
    os.remove(path)


# --- contract, every backend ------------------------------------------------------


def test_a_pod_without_the_file_gets_the_stored_bytes(uploads, object_store):
    path = uploads / "files" / f"{uuid.uuid4()}_report.xlsx"
    body = os.urandom(2048)
    _write(path, body)
    assert storage.persist(path) is True

    _new_pod(path)
    assert storage.ensure_local(path) is True
    assert _read(path) == body


def test_cwd_relative_paths_map_to_the_same_object(uploads, object_store):
    name = f"{uuid.uuid4()}_data.csv"
    rel = os.path.join("uploads", "files", name)
    _write(rel, b"a,b\n1,2\n")
    storage.persist(rel)

    absolute = uploads / "files" / name
    _new_pod(absolute)
    assert storage.ensure_local(absolute) is True
    assert _read(rel) == b"a,b\n1,2\n"


def test_a_copy_replaced_on_another_pod_is_refreshed(uploads, object_store):
    path = uploads / "thumbnails" / f"{uuid.uuid4()}.png"
    _write(path, b"version-1")
    storage.persist(path)
    assert storage.ensure_local(path) is True

    # Another pod rewrites the same name (thumbnails, logos, connector copies).
    other = uploads / f"other-{uuid.uuid4()}.bin"
    _write(other, b"version-2-longer")
    object_store.upload(storage.key_for(path), str(other), "image/png")

    assert storage.ensure_local(path) is True
    assert _read(path) == b"version-2-longer"


def test_a_copy_deleted_on_another_pod_is_not_served(uploads, object_store):
    path = uploads / "branding" / f"{uuid.uuid4()}.png"
    _write(path, b"logo")
    storage.persist(path)

    object_store.delete(storage.key_for(path))

    assert storage.ensure_local(path) is False
    assert not path.exists()


def test_discard_removes_the_object_for_every_pod(uploads, object_store):
    path = uploads / "avatars" / f"{uuid.uuid4()}.png"
    _write(path, b"face")
    storage.persist(path)

    storage.discard(path)
    _new_pod(path)
    assert storage.ensure_local(path) is False


def test_a_never_stored_file_is_simply_missing(uploads, object_store):
    path = uploads / "files" / f"{uuid.uuid4()}_nothing.txt"
    assert storage.ensure_local(path) is False
    assert not path.exists()


def test_directory_trees_round_trip_and_are_removed_together(uploads, object_store):
    deck = uploads / "pptx_previews" / f"release-{uuid.uuid4()}"
    slides = {f"slide-{i:02d}.png": os.urandom(64) for i in range(1, 4)}
    for name, data in slides.items():
        _write(deck / name, data)
    assert storage.persist_tree(deck) == len(slides)

    for name in slides:
        os.remove(deck / name)
    assert storage.ensure_local_tree(deck) is True
    assert {n: _read(deck / n) for n in slides} == slides

    storage.discard_tree(deck)
    for name in slides:
        os.remove(deck / name)
    assert storage.ensure_local_tree(deck) is False


def test_trees_do_not_bleed_into_sibling_prefixes(uploads, object_store):
    base = uploads / "pptx_previews"
    _write(base / "abc" / "slide-01.png", b"mine")
    _write(base / "abcdef" / "slide-01.png", b"neighbour")
    storage.persist_tree(base / "abc")
    storage.persist_tree(base / "abcdef")

    storage.discard_tree(base / "abc")
    os.remove(base / "abcdef" / "slide-01.png")
    assert storage.ensure_local(base / "abcdef" / "slide-01.png") is True


def test_write_once_reads_can_skip_the_freshness_check(uploads, object_store):
    path = uploads / "qvd_cache" / f"{uuid.uuid4().hex}_1_v2.parquet"
    _write(path, b"PAR1")
    storage.persist(path)
    object_store.delete(storage.key_for(path))

    # fresh=False trusts the local copy (content-addressed names never change).
    assert storage.ensure_local(path, fresh=False) is True


def test_async_api_matches_sync(uploads, object_store):
    path = uploads / "files" / f"{uuid.uuid4()}_a.json"
    _write(path, b"{}")

    async def flow():
        assert await storage.apersist(path, "application/json") is True
        _new_pod(path)
        assert await storage.aensure_local(path) is True
        await storage.adiscard(path)
        _new_pod(path)
        return await storage.aensure_local(path)

    assert asyncio.run(flow()) is False


def test_startup_check_round_trips(uploads, object_store):
    assert storage.check() == object_store.describe()


@pytest.mark.parametrize("kind", ["s3-moto", "azure-azurite"])
def test_prefixes_isolate_deployments_sharing_a_bucket(uploads, kind):
    with make_object_store(kind) as a:
        b = type(a).__new__(type(a))
        b.__dict__.update(a.__dict__)
        b.prefix = a.prefix + "other/"
        path = uploads / "files" / f"{uuid.uuid4()}_x.txt"
        _write(path, b"tenant-a")
        storage.set_store(a)
        try:
            storage.persist(path)
            storage.set_store(b)
            _new_pod(path)
            assert storage.ensure_local(path) is False
        finally:
            storage.reset_store()


# --- what is (and is not) mirrored -------------------------------------------------


@pytest.mark.parametrize("area", sorted(keys.LOCAL_ONLY_AREAS - {".storage-tmp"}))
def test_local_only_areas_never_leave_the_disk(uploads, object_store, area):
    path = uploads / area / "x" / "y.bin"
    _write(path, b"local")
    assert storage.key_for(path) is None
    assert storage.persist(path) is False
    assert storage.ensure_local(path) is True


def test_paths_outside_uploads_are_left_alone(uploads, object_store, tmp_path):
    outside = tmp_path / "elsewhere" / "f.txt"
    _write(outside, b"x")
    assert storage.persist(outside) is False
    assert storage.ensure_local(outside) is True
    os.remove(outside)
    assert storage.ensure_local(outside) is False


@pytest.mark.parametrize("bad", ["uploads/../etc/passwd", "uploads", "uploads/.hidden/x", "uploads/files/.x.part"])
def test_keys_never_escape_or_include_transient_names(uploads, bad):
    assert storage.key_for(bad) is None


def test_failed_upload_raises(uploads, object_store):
    path = uploads / "files" / f"{uuid.uuid4()}_gone.txt"
    with pytest.raises(storage.StorageError):
        storage.persist(path)  # not a file


# --- default local backend ---------------------------------------------------------


def test_local_backend_is_plain_disk(uploads):
    storage.set_store(None)
    try:
        path = uploads / "files" / f"{uuid.uuid4()}_x.txt"
        assert storage.enabled() is False
        assert storage.ensure_local(path) is False
        _write(path, b"x")
        assert storage.persist(path) is False
        assert storage.ensure_local(path) is True
        storage.discard(path)
        assert path.exists()
        assert storage.check().startswith("local")
    finally:
        storage.reset_store()


# --- configuration -----------------------------------------------------------------


def test_storage_defaults_to_local(monkeypatch):
    from app.settings.bow_config import StorageConfig

    for k in list(os.environ):
        if k.startswith("BOW_STORAGE_"):
            monkeypatch.delenv(k, raising=False)
    cfg = StorageConfig()
    assert cfg.backend == "local"
    assert cfg.prefix == ""


def test_env_selects_backend_and_normalizes_prefix(monkeypatch):
    from app.settings.bow_config import StorageConfig

    monkeypatch.setenv("BOW_STORAGE_BACKEND", "S3")
    monkeypatch.setenv("BOW_STORAGE_PREFIX", "/tenant-a/bow/")
    monkeypatch.setenv("BOW_STORAGE_S3_BUCKET", "b")
    monkeypatch.setenv("BOW_STORAGE_S3_AUTH", "assume_role")
    cfg = StorageConfig()
    assert (cfg.backend, cfg.prefix, cfg.s3.bucket, cfg.s3.auth) == ("s3", "tenant-a/bow/", "b", "assume_role")


@pytest.mark.parametrize("field,value", [
    ("backend", "gcs"),
    ("prefix", "a/../b"),
])
def test_invalid_storage_config_is_rejected(field, value):
    from pydantic import ValidationError
    from app.settings.bow_config import StorageConfig

    with pytest.raises(ValidationError):
        StorageConfig(**{field: value})


@pytest.mark.parametrize("model,auth", [
    ("S3StorageConfig", "iam"),
    ("AzureStorageConfig", "account_key"),
])
def test_unknown_auth_modes_are_rejected(model, auth):
    from pydantic import ValidationError
    from app.settings import bow_config

    with pytest.raises(ValidationError):
        getattr(bow_config, model)(auth=auth)


@pytest.mark.parametrize("cfg", [
    {"bucket": ""},
    {"bucket": "b", "auth": "static"},
    {"bucket": "b", "auth": "assume_role"},
])
def test_s3_misconfiguration_names_the_missing_setting(cfg):
    from app.services.storage.s3 import S3Store
    from app.settings.bow_config import S3StorageConfig

    with pytest.raises(storage.StorageError):
        S3Store(S3StorageConfig(**cfg))


@pytest.mark.parametrize("cfg", [
    {"container": ""},
    {"container": "c", "auth": "sas", "account_url": "https://a.blob.core.windows.net"},
    {"container": "c", "auth": "connection_string"},
    {"container": "c", "auth": "service_principal", "account_url": "https://a.blob.core.windows.net"},
    {"container": "c", "auth": "default"},
])
def test_azure_misconfiguration_names_the_missing_setting(cfg):
    from app.services.storage.azure_blob import AzureBlobStore
    from app.settings.bow_config import AzureStorageConfig

    with pytest.raises(storage.StorageError):
        AzureBlobStore(AzureStorageConfig(**cfg))


def test_assume_role_uses_refreshing_sts_credentials():
    from moto import mock_aws
    from app.services.storage.s3 import build_boto3_session
    from app.settings.bow_config import S3StorageConfig

    with mock_aws():
        session = build_boto3_session(S3StorageConfig(
            bucket="b", region="us-east-1", auth="assume_role",
            access_key_id="testing", secret_access_key="testing",
            role_arn="arn:aws:iam::123456789012:role/bow-storage", external_id="ext",
        ))
        creds = session.get_credentials()
        assert creds.access_key and creds.access_key != "testing"
        assert creds.token
