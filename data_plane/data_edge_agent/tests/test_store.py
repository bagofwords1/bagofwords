"""Connection store (design C4): cleartext persistence + round-tripping.

Credentials are stored in cleartext by design — securing the file is the site
owner's responsibility — so these assert the round-trip and 0600 file mode, not
encryption.
"""

from __future__ import annotations

import json
import stat

import pytest

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


def test_upsert_then_list_round_trips(tmp_path):
    store = ConnectionStore(tmp_path / "store.json")
    store.upsert(_conn())

    got = store.list()
    assert len(got) == 1
    c = got[0]
    assert c.name == "pg" and c.type == "postgresql"
    assert c.config["host"] == "db.internal"
    assert c.credentials == {"user": "bow_reader", "password": "hunter2"}
    assert c.query_timeout_seconds == 120


def test_credentials_are_stored_in_cleartext(tmp_path):
    path = tmp_path / "store.json"
    store = ConnectionStore(path)
    store.upsert(_conn())

    row = json.loads(path.read_text())["connections"][0]
    # cleartext credentials dict, no encrypted blob
    assert row["credentials"] == {"user": "bow_reader", "password": "hunter2"}
    assert "credentials_enc" not in row


def test_store_file_is_0600(tmp_path):
    path = tmp_path / "store.json"
    ConnectionStore(path).upsert(_conn())
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_upsert_replaces_by_name(tmp_path):
    store = ConnectionStore(tmp_path / "store.json")
    store.upsert(_conn(config={"host": "old"}))
    store.upsert(_conn(config={"host": "new"}))
    got = store.list()
    assert len(got) == 1 and got[0].config["host"] == "new"


def test_delete(tmp_path):
    store = ConnectionStore(tmp_path / "store.json")
    store.upsert(_conn("a"))
    store.upsert(_conn("b"))
    assert store.delete("a") is True
    assert [c.name for c in store.list()] == ["b"]
    assert store.delete("missing") is False


def test_reopen_reads_existing_connections(tmp_path):
    path = tmp_path / "store.json"
    ConnectionStore(path).upsert(_conn())
    # a fresh instance (a restart) reads what was written, no key needed
    reopened = ConnectionStore(path)
    assert reopened.list()[0].credentials["password"] == "hunter2"


def test_empty_store_lists_nothing(tmp_path):
    assert ConnectionStore(tmp_path / "store.json").list() == []


def test_corrupt_file_raises_storeerror(tmp_path):
    path = tmp_path / "store.json"
    path.write_text("{ not json")
    with pytest.raises(StoreError):
        ConnectionStore(path).list()
