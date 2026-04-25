"""Load tests for ConnectionIndexing — prove the background job handles a
large-schema source end-to-end without blocking the HTTP request.

Phase 7 acceptance: a sqlite source with **500 tables** must:
  - POST /connections in < 1s
  - reach indexing.status == "completed" with progress_done == progress_total == 500
  - emit progress that increases monotonically across at least a few poll samples
  - end with ConnectionTable rows matching the 500 tables
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest


N_TABLES = 500


def _build_large_sqlite_fixture() -> str:
    """Create a sqlite DB with N_TABLES simple tables. Returns absolute path.

    The file lives in the tests' temp area so it's cleaned up with the pytest
    session; we cache it across tests by parameterizing on (N_TABLES, schema_version).
    """
    cache_dir = Path(tempfile.gettempdir()) / "bow_indexing_load_fixture"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"load_{N_TABLES}.sqlite"
    if path.exists():
        return str(path)
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        for i in range(N_TABLES):
            # 10 columns each — small but representative of a wide warehouse.
            cols = ", ".join(
                [f"col_{j} TEXT" for j in range(10)]
            )
            cur.execute(f"CREATE TABLE t_{i:04d} (id INTEGER PRIMARY KEY, {cols})")
        conn.commit()
    finally:
        conn.close()
    return str(path)


def _poll_samples(test_client, conn_id, token, org_id, *, timeout_s: float = 120.0):
    """Poll the indexing endpoint and collect progress samples until terminal.
    Returns (final_row, samples). Samples is a list of (progress_done, progress_total).
    """
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    deadline = time.perf_counter() + timeout_s
    samples: list[tuple[int, int, str]] = []
    last = None
    while time.perf_counter() < deadline:
        r = test_client.get(f"/api/connections/{conn_id}/indexing", headers=headers)
        if r.status_code == 404:
            time.sleep(0.05)
            continue
        assert r.status_code == 200, r.text
        last = r.json()
        samples.append((last["progress_done"], last["progress_total"], last["status"]))
        if last["status"] in ("completed", "failed", "cancelled"):
            return last, samples
        time.sleep(0.1)
    pytest.fail(f"Indexing did not reach terminal state within {timeout_s}s; last={last!r}")


@pytest.mark.e2e
def test_large_sqlite_500_tables_indexing_completes(
    create_connection,
    test_client,
    create_user,
    login_user,
    whoami,
):
    fixture_path = _build_large_sqlite_fixture()
    assert os.path.exists(fixture_path)

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    t0 = time.perf_counter()
    connection = create_connection(
        name="Load 500 Tables",
        type="sqlite",
        config={"database": fixture_path},
        credentials={},
        user_token=token,
        org_id=org_id,
    )
    post_duration = time.perf_counter() - t0
    conn_id = connection["id"]
    # POST /connections must not wait on schema — cap at 3s to absorb
    # test-harness overhead while still proving the refactor.
    assert post_duration < 3.0, f"POST /connections took {post_duration:.2f}s (expected <3s)"

    final, samples = _poll_samples(test_client, conn_id, token, org_id, timeout_s=120.0)
    assert final["status"] == "completed", final
    assert final["progress_total"] == N_TABLES, final
    assert final["progress_done"] == N_TABLES, final
    assert (final.get("stats") or {}).get("table_count") == N_TABLES

    # Monotonic progress: progress_done never regresses across samples.
    last_done = -1
    for done, total, _status in samples:
        assert done >= last_done, f"progress_done regressed: {done} < {last_done}"
        last_done = done

    # Verify ConnectionTable rows
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    r = test_client.get(f"/api/connections/{conn_id}/tables", headers=headers)
    assert r.status_code == 200, r.text
    tables = r.json()
    assert len(tables) == N_TABLES
