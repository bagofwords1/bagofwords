"""E2E: reindexing an UNCHANGED network_dir must not re-extract file content.

Drives the real stack — POST /connections (kicks background indexing) →
ConnectionIndexingService runner → ConnectionService.refresh_schema →
NetworkDirClient.get_schemas — and asserts the second run (POST /reindex on an
unchanged directory) reuses the stored keywords via the `prior_catalog` skip
instead of re-reading every file. Before the incremental fix the second run
re-extracted everything, which on a PDF-heavy share meant hours of pypdf per
scheduled reindex.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.data_sources.clients.network_dir_client import NetworkDirClient


def _poll_until_terminal(test_client, connection_id, headers, *, timeout_s: float = 15.0):
    deadline = time.perf_counter() + timeout_s
    last = None
    while time.perf_counter() < deadline:
        r = test_client.get(f"/api/connections/{connection_id}/indexing", headers=headers)
        if r.status_code == 404:
            time.sleep(0.05)
            continue
        assert r.status_code == 200, r.text
        last = r.json()
        if last["status"] in ("completed", "failed", "cancelled"):
            return last
        time.sleep(0.1)
    pytest.fail(f"Indexing did not reach terminal state within {timeout_s}s; last={last!r}")


async def _catalog_meta(conn_id: str) -> dict:
    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.connection_table import ConnectionTable

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(ConnectionTable).where(ConnectionTable.connection_id == str(conn_id))
        )).scalars().all()
        return {r.name: (r.metadata_json or {}).get("network_dir", {}) for r in rows}


@pytest.mark.e2e
def test_reindex_unchanged_dir_skips_extraction(
    create_connection,
    test_client,
    create_user,
    login_user,
    whoami,
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "alpha.txt").write_text("alpha renewal clause budget\n")
    (tmp_path / "docs" / "beta.txt").write_text("beta indemnity headcount\n")
    (tmp_path / "gamma.md").write_text("# gamma\narbitration forecast\n")

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    connection = create_connection(
        name="Incremental Share",
        type="network_dir",
        config={"root_path": str(tmp_path), "index_mode": "content"},
        credentials={},
        user_token=token,
        org_id=org_id,
    )
    conn_id = connection["id"]

    first = _poll_until_terminal(test_client, conn_id, headers)
    assert first["status"] == "completed", first
    # get_schemas now ticks per file, so the indexing row shows real progress
    # (3 files) instead of the 0/0 it reported before.
    assert first["progress_total"] == 3, first
    assert first["progress_done"] == 3, first

    meta1 = asyncio.run(_catalog_meta(conn_id))
    assert len(meta1) == 3
    assert all(m.get("keywords") for m in meta1.values()), meta1

    # From here on, count real content extractions. The indexing runner lives
    # on a daemon thread in this same process, so the patch reaches it.
    calls: list[str] = []
    original = NetworkDirClient._file_text

    def counting(self, path, max_chars=200_000):
        calls.append(Path(path).name)
        return original(self, path, max_chars)

    monkeypatch.setattr(NetworkDirClient, "_file_text", counting)

    r = test_client.post(f"/api/connections/{conn_id}/reindex", headers=headers)
    assert r.status_code == 200, r.text
    second = _poll_until_terminal(test_client, conn_id, headers)
    assert second["status"] == "completed", second

    # The whole point: an unchanged directory re-indexes without reading a
    # single file's content...
    assert calls == [], f"reindex re-extracted {calls}"
    # ...and the catalog keeps its keywords/hashes.
    meta2 = asyncio.run(_catalog_meta(conn_id))
    assert {n: m.get("keywords") for n, m in meta2.items()} == \
           {n: m.get("keywords") for n, m in meta1.items()}
    assert {n: m.get("content_hash") for n, m in meta2.items()} == \
           {n: m.get("content_hash") for n, m in meta1.items()}

    # A changed file IS re-extracted on the next reindex — and only that file.
    import os
    target = tmp_path / "docs" / "alpha.txt"
    target.write_text("alpha totally different retention wording\n")
    st = target.stat()
    os.utime(target, (st.st_atime, st.st_mtime + 5))

    r = test_client.post(f"/api/connections/{conn_id}/reindex", headers=headers)
    assert r.status_code == 200, r.text
    third = _poll_until_terminal(test_client, conn_id, headers)
    assert third["status"] == "completed", third
    assert calls == ["alpha.txt"], calls

    meta3 = asyncio.run(_catalog_meta(conn_id))
    assert "retention" in (meta3["docs/alpha.txt"].get("keywords") or [])
    assert meta3["docs/alpha.txt"]["content_hash"] != meta1["docs/alpha.txt"]["content_hash"]
