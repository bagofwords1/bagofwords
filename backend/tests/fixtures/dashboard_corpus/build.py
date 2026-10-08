"""Build the whole corpus, in the order the phase-0 spec fixes.

1. The test database is already at head (`run_migrations`).
2. People, groups, roles, projects and data sources -- through the API.
3. Back to MAIN_BASE_REVISION (no-op until this branch adds a migration).
4. Conversations and dashboards in their old shape -- SQLAlchemy Core.
5. Upgrade to head: this branch's migrations run over every kind.
6. What only the API can create on top: live dashboard resources.
"""
from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path

from tests.fixtures.dashboard_corpus.alembic_at import (
    downgrade_test_db_to_base,
    upgrade_test_db_to_head,
)
from tests.fixtures.dashboard_corpus.cast import build_cast
from tests.fixtures.dashboard_corpus.kinds import Corpus, seed_corpus
from tests.fixtures.dashboard_corpus.rows import Rows

# Where ThumbnailService reads and writes (relative to the backend package).
THUMBNAILS_DIR = str(Path(__file__).resolve().parents[3] / "uploads" / "thumbnails")


def build_corpus(fixtures: dict, rows: Rows) -> Corpus:
    client = fixtures["test_client"]
    cast = build_cast(rows=rows, **fixtures)
    admin = cast.people["admin"]

    def put_setting(name: str) -> None:
        resp = client.put("/api/organization/settings", headers=admin.headers(cast.org_id),
                          json={"config": {name: {"value": True}}})
        assert resp.status_code == 200, resp.text

    put_setting("enable_load_step")

    downgrade_test_db_to_base()
    written: list[str] = []
    try:
        corpus = seed_corpus(rows, cast, THUMBNAILS_DIR, written)
        upgrade_test_db_to_head()
        _add_live_resources(client, cast, corpus, put_setting)
    except BaseException:
        _remove(written)
        raise
    return corpus


def _add_live_resources(client, cast, corpus, put_setting) -> None:
    put_setting("enable_artifact_resources")
    owner = cast.people["owner"]
    base = f"/api/artifacts/{corpus.ids['live_resources/dash']}/runtime"
    resp = client.post(base + "/resources", headers=owner.headers(cast.org_id), json={
        "action": "create", "idempotency_key": str(uuid.uuid4()),
        "definition": {"name": "entries", "fields": {
            "title": {"type": "string", "required": True},
            "status": {"type": "string", "default": "draft", "indexed": True}}},
    })
    assert resp.status_code == 200, resp.text
    for title in ("בקשה ראשונה", "בקשה שנייה", "בקשה שלישית"):
        resp = client.post(base + "/collections/entries/records", headers=owner.headers(cast.org_id),
                           json={"action": "create", "data": {"title": title},
                                 "idempotency_key": str(uuid.uuid4())})
        assert resp.status_code == 200, resp.text


def _remove(paths: list[str]) -> None:
    for path in paths:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


def remove_corpus_files(corpus: Corpus) -> None:
    _remove(corpus.thumbnail_files)
