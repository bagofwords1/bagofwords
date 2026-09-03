"""Credential store (design C4): encryption at rest + round-tripping."""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from ..config import ConnectionConfig
from ..store import ConnectionStore, StoreError


def _conn(name="pg", **over):
    base = dict(
        name=name, type="postgresql", label="Prod",
        config={"host": "db.internal", "port": 5432, "database": "analytics"},
        credentials={"user": "bow_reader", "password": "hunter2"},
        query_timeout_seconds=120,
    )
    base.update(over)
    return ConnectionConfig(**base)


@pytest.fixture
def key():
    return Fernet.generate_key().decode()


def test_upsert_then_list_round_trips(tmp_path, key):
    store = ConnectionStore(tmp_path / "store.json", key)
    store.upsert(_conn())

    got = store.list()
    assert len(got) == 1
    c = got[0]
    assert c.name == "pg" and c.type == "postgresql"
    assert c.config["host"] == "db.internal"
    assert c.credentials == {"user": "bow_reader", "password": "hunter2"}
    assert c.query_timeout_seconds == 120


def test_credentials_are_encrypted_on_disk(tmp_path, key):
    path = tmp_path / "store.json"
    store = ConnectionStore(path, key)
    store.upsert(_conn())

    raw = path.read_text()
    # The secret must not appear in cleartext; config detail may.
    assert "hunter2" not in raw
    assert "bow_reader" not in raw
    assert "db.internal" in raw  # config is intentionally clear
    row = json.loads(raw)["connections"][0]
    assert "credentials_enc" in row and "credentials" not in row


def test_upsert_replaces_by_name(tmp_path, key):
    store = ConnectionStore(tmp_path / "store.json", key)
    store.upsert(_conn(config={"host": "old"}))
    store.upsert(_conn(config={"host": "new"}))
    got = store.list()
    assert len(got) == 1 and got[0].config["host"] == "new"


def test_delete(tmp_path, key):
    store = ConnectionStore(tmp_path / "store.json", key)
    store.upsert(_conn("a"))
    store.upsert(_conn("b"))
    assert store.delete("a") is True
    assert [c.name for c in store.list()] == ["b"]
    assert store.delete("missing") is False


def test_wrong_key_is_a_loud_error_not_silent_credential_loss(tmp_path, key):
    path = tmp_path / "store.json"
    ConnectionStore(path, key).upsert(_conn())
    other = ConnectionStore(path, Fernet.generate_key().decode())
    with pytest.raises(StoreError):
        other.list()


def test_generated_key_file_persists_across_instances(tmp_path):
    path = tmp_path / "store.json"
    ConnectionStore(path).upsert(_conn())  # generates <store>.key
    keyfile = path.with_suffix(path.suffix + ".key")
    assert keyfile.is_file()
    # A fresh instance with no explicit key reuses the generated key file.
    reopened = ConnectionStore(path)
    assert reopened.list()[0].credentials["password"] == "hunter2"


def test_empty_store_lists_nothing(tmp_path, key):
    assert ConnectionStore(tmp_path / "store.json", key).list() == []
