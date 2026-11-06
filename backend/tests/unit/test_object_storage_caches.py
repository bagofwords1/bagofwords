"""State other pods must see, beyond plain uploads: decoded PBIX caches, learned
SSH host keys, fast-mode extracts, and the one-off uploads/ migration.

"Pod B" is the same process after its local copy is wiped — exactly what a new
replica (or a restarted pod with an emptyDir) starts with.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import types
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from app.services import storage
from app.services.storage import keys


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setattr(keys, "UPLOADS_ROOT", root)
    monkeypatch.setattr(storage, "UPLOADS_ROOT", root)
    monkeypatch.chdir(tmp_path)
    return root


# --- PBIX: a decode on one pod serves every pod -----------------------------------


class _FakePBIXRay:
    def __init__(self, path):
        self.path = path

    @property
    def schema(self):
        return pd.DataFrame([
            {"TableName": "Sales", "ColumnName": "Region", "PandasDataType": "string"},
            {"TableName": "Sales", "ColumnName": "Amount", "PandasDataType": "Float64"},
        ])

    @property
    def relationships(self):
        return pd.DataFrame([])

    @property
    def dax_measures(self):
        return pd.DataFrame([])

    def get_table(self, name):
        return pd.DataFrame({"Region": ["north", "south", "south"], "Amount": [1.0, 2.0, 4.0]})


@pytest.fixture
def pbix(uploads, tmp_path, monkeypatch):
    import app.data_sources.clients.pbix_client as mod

    fake = types.ModuleType("pbixray")
    fake.PBIXRay = _FakePBIXRay
    monkeypatch.setitem(sys.modules, "pbixray", fake)
    monkeypatch.setenv("BOW_PBIX_INPROCESS_EXTRACT", "1")
    monkeypatch.setattr(mod, "_CACHE_DIR", uploads / "pbix_file_cache")
    src = tmp_path / "share" / "sales.pbix"
    src.parent.mkdir()
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("Report/Layout", "{}")
        zf.writestr("DataModel", b"\x00\x01")
    return mod, src


def test_pbix_decode_is_reused_by_a_pod_that_never_decoded(pbix, uploads, object_store):
    mod, src = pbix
    query = 'SELECT "Region", SUM("Amount") AS total FROM Sales GROUP BY 1 ORDER BY 1'
    expected = mod.PBIXClient(str(src)).execute_query(query)  # pod A decodes
    mod.PBIXClient(str(src)).get_tables()                     # and caches the schema

    shutil.rmtree(uploads / "pbix_file_cache")                # pod B: nothing local
    with patch.object(mod, "materialize_pbix_parquets", side_effect=AssertionError("re-decoded")), \
         patch.object(mod, "extract_pbix_model", side_effect=AssertionError("re-parsed schema")):
        got = mod.PBIXClient(str(src)).execute_query(query)
        tables = mod.PBIXClient(str(src)).get_tables()

    pd.testing.assert_frame_equal(got, expected)
    assert {t.name for t in tables} == {"Sales"}


def test_pbix_on_local_storage_still_decodes_on_a_cold_cache(pbix, uploads):
    mod, src = pbix
    storage.set_store(None)
    try:
        mod.PBIXClient(str(src)).execute_query("SELECT COUNT(*) AS n FROM Sales")
        shutil.rmtree(uploads / "pbix_file_cache")
        with patch.object(mod, "materialize_pbix_parquets", wraps=mod.materialize_pbix_parquets) as m:
            df = mod.PBIXClient(str(src)).execute_query("SELECT COUNT(*) AS n FROM Sales")
        assert m.called
        assert int(df.iloc[0]["n"]) == 3
    finally:
        storage.reset_store()


# --- learned SSH host keys bind every replica -------------------------------------


@pytest.fixture
def git_state(uploads, monkeypatch):
    from app.services import git_hardening

    d = uploads / "git_state"
    monkeypatch.setattr(git_hardening, "_STATE_DIR", d)
    return git_hardening, d


def _learned(d: Path) -> set[str]:
    p = d / "known_hosts.learned"
    return {ln for ln in p.read_text().splitlines() if ln} if p.exists() else set()


def test_a_host_key_learned_on_one_pod_is_trusted_by_the_next(git_state, object_store):
    gh, d = git_state
    gh.ssh_command("/tmp/key")
    (d / "known_hosts.learned").write_text("git.corp.example ssh-ed25519 AAAA-first\n")
    gh.persist_learned_hosts()

    shutil.rmtree(d)  # a new pod
    cmd = gh.ssh_command("/tmp/key")
    assert "StrictHostKeyChecking=accept-new" in cmd
    assert _learned(d) == {"git.corp.example ssh-ed25519 AAAA-first"}


def test_pods_learning_different_hosts_keep_each_others_keys(git_state, object_store):
    gh, d = git_state
    learned = d / "known_hosts.learned"
    gh.ssh_command("/tmp/key")
    learned.write_text("a.example ssh-ed25519 AAAA-a\n")
    gh.persist_learned_hosts()                        # pod A publishes host a

    shutil.rmtree(d)
    gh.ssh_command("/tmp/key")                        # pod B starts with a
    with open(learned, "a") as fh:
        fh.write("b.example ssh-ed25519 AAAA-b\n")
    gh.persist_learned_hosts()                        # pod B publishes a + b

    # Pod A (stale local copy: only a) learns c, then publishes.
    learned.write_text("a.example ssh-ed25519 AAAA-a\nc.example ssh-ed25519 AAAA-c\n")
    gh.persist_learned_hosts()

    shutil.rmtree(d)
    gh.ssh_command("/tmp/key")
    assert _learned(d) == {
        "a.example ssh-ed25519 AAAA-a", "b.example ssh-ed25519 AAAA-b", "c.example ssh-ed25519 AAAA-c",
    }


def test_host_key_sync_never_breaks_git_when_the_store_is_down(git_state):
    gh, d = git_state

    class Down(storage.ObjectStore):
        def upload(self, *a, **k): raise storage.StorageError("down")
        def download(self, *a, **k): raise storage.StorageError("down")
        def etag(self, *a, **k): raise storage.StorageError("down")
        def delete(self, *a, **k): raise storage.StorageError("down")
        def list(self, *a, **k): raise storage.StorageError("down")

    storage.set_store(Down())
    try:
        assert "UserKnownHostsFile" in gh.ssh_command("/tmp/key")
        gh.persist_learned_hosts()
    finally:
        storage.reset_store()


# --- fast-mode extracts -----------------------------------------------------------


def test_fast_extract_deleted_on_one_pod_is_gone_for_all(uploads, object_store, monkeypatch):
    from app.data_sources.fast import artifacts

    monkeypatch.setattr(artifacts, "_ARTIFACT_ROOT", uploads / "fast")
    path = artifacts.new_artifact_path(str(uuid.uuid4()))
    path.write_bytes(b"duckdb-bytes")
    storage.persist(path)

    artifacts.delete_artifact(str(path))
    assert not path.exists()
    assert storage.ensure_local(path, fresh=False) is False


# --- migrating an existing uploads/ volume ----------------------------------------


def _load_migration_script():
    p = Path(__file__).resolve().parents[2] / "scripts" / "migrate_uploads_to_object_storage.py"
    spec = importlib.util.spec_from_file_location("migrate_uploads_to_object_storage", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_uploads_every_mirrored_file_once(uploads, object_store, monkeypatch, capsys):
    durable = {
        "files/1_a.csv": b"a,b\n1,2\n",
        "thumbnails/x.png": b"png",
        "pptx_previews/d/slide-01.png": b"slide",
        "git_state/known_hosts.learned": b"h k\n",
    }
    local_only = ["repo_cache/r/README.md", "pdfs/x.pdf", "artifact_js/k.js", "filecache/c/meta.json"]
    for rel, data in durable.items():
        (uploads / rel).parent.mkdir(parents=True, exist_ok=True)
        (uploads / rel).write_bytes(data)
    for rel in local_only:
        (uploads / rel).parent.mkdir(parents=True, exist_ok=True)
        (uploads / rel).write_bytes(b"local")

    script = _load_migration_script()
    monkeypatch.setattr(sys, "argv", ["migrate"])
    assert script.main() == 0
    assert set(object_store.list("")) == set(durable)

    # Idempotent: a second run uploads nothing.
    monkeypatch.setattr(sys, "argv", ["migrate"])
    assert script.main() == 0
    assert "uploaded 0 files" in capsys.readouterr().out

    for rel, data in durable.items():
        os.remove(uploads / rel)
        assert storage.ensure_local(uploads / rel) is True
        assert (uploads / rel).read_bytes() == data


def test_migration_refuses_to_run_on_local_storage(uploads, monkeypatch):
    storage.set_store(None)
    try:
        monkeypatch.setattr(sys, "argv", ["migrate"])
        assert _load_migration_script().main() == 2
    finally:
        storage.reset_store()


# --- generated demo datasets ------------------------------------------------------


def test_generated_demo_dataset_opens_on_a_pod_that_did_not_generate_it(uploads, object_store):
    import sqlite3
    from app.data_sources.clients.sqlite_client import SqliteClient

    db = uploads / "demo_data" / str(uuid.uuid4()) / f"{uuid.uuid4()}.sqlite"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE orders (id INTEGER, amount REAL)")
    con.executemany("INSERT INTO orders VALUES (?, ?)", [(1, 9.5), (2, 20.0)])
    con.commit()
    con.close()
    storage.persist(db)

    os.remove(db)  # pod B
    df = SqliteClient(database=str(db)).execute_query("SELECT SUM(amount) AS total FROM orders")
    assert float(df.iloc[0]["total"]) == 29.5
