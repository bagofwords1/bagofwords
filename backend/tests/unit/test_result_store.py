# Loop A — Result Store invariants.
# See sandbox-feedback-loop-result-store.md.
import os
import re
import uuid
from datetime import datetime, timedelta

import duckdb
import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.base import Base
from app.models.result_file import ResultFile
from app.services.result_store import (
    ResultStore,
    LocalDirStorage,
    SpillResult,
    SLICE_MAX_ROWS,
    validate_slice_sql,
)

ORG = str(uuid.uuid4())


@pytest_asyncio.fixture
async def adb(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/handles.db")
    async with eng.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=[ResultFile.__table__]
            )
        )
    Session = async_sessionmaker(eng, expire_on_commit=False)
    async with Session() as s:
        yield s
    await eng.dispose()


@pytest.fixture
def svc(tmp_path):
    return ResultStore(storage=LocalDirStorage(root=str(tmp_path / "store")))


def _df(n=5000, needle_at=4242):
    df = pd.DataFrame(
        {
            "id": range(n),
            "msg": [f"row-{i}" if i != needle_at else f"NEEDLE-{i}" for i in range(n)],
            "amount": [i * 1.5 for i in range(n)],
            "ts": pd.date_range("2026-01-01", periods=n, freq="s"),
        }
    )
    return df


async def _spill_and_persist(svc, adb, df=None, *, step_id=None, query_id=None, producer="create_data"):
    df = _df() if df is None else df
    spill = await svc.spill_dataframe(df, organization_id=ORG, producer=producer)
    artifact = await svc.persist_handle(
        adb, spill, organization_id=ORG, step_id=step_id, query_id=query_id
    )
    return artifact, df


# --- invariant 1: floor behavior --------------------------------------------

def test_floor_small_result_no_spill():
    assert ResultStore.should_spill(total_rows=500, stored_rows=500, approx_bytes=10_000) is False


def test_floor_truncated_result_spills():
    assert ResultStore.should_spill(total_rows=5000, stored_rows=1000, approx_bytes=10_000) is True


def test_floor_large_bytes_spills_even_untruncated():
    assert ResultStore.should_spill(total_rows=100, stored_rows=100, approx_bytes=10_000_000) is True


# --- invariants 2/3: publish + encryption ------------------------------------

@pytest.mark.asyncio
async def test_publish_produces_attachable_encrypted_file(svc, adb):
    artifact, df = await _spill_and_persist(svc, adb)
    assert artifact.status == "published"
    assert artifact.row_count == len(df)
    path = svc.storage.abs_path(artifact.storage_ref)
    assert os.path.exists(path)
    # no plaintext in payload
    raw = open(path, "rb").read()
    assert b"NEEDLE-4242" not in raw
    # wrong key fails closed
    with pytest.raises(Exception):
        duckdb.connect().execute(f"ATTACH '{path}' AS x (ENCRYPTION_KEY '{'0'*64}', READ_ONLY)")
    # no tmp leftovers
    tmpdir = os.path.join(svc.storage.root, ".tmp")
    assert not os.listdir(tmpdir) if os.path.isdir(tmpdir) else True


@pytest.mark.asyncio
async def test_spill_failure_is_loud_no_handle_possible(tmp_path, adb):
    # Storage root that cannot be a directory (it's a file) -> spill raises
    # (loud contract). chmod-based denial doesn't apply when running as root.
    root = tmp_path / "rofile"
    root.write_text("not a directory")
    bad = ResultStore(storage=LocalDirStorage(root=str(root)))
    with pytest.raises(Exception):
        await bad.spill_dataframe(_df(100), organization_id=ORG)


def test_shredded_artifact_fails_closed(svc):
    a = ResultFile(
        id=str(uuid.uuid4()), organization_id=ORG, storage_ref="artifacts/x.duckdb",
        wrapped_key=None, status="published", row_count=1, byte_size=1,
    )
    with pytest.raises(ValueError, match="shredded"):
        svc._attach_readonly(a)


# --- invariant 5: slice correctness ------------------------------------------

@pytest.mark.asyncio
async def test_slice_page_grep_project_window_sql(svc, adb):
    artifact, df = await _spill_and_persist(svc, adb)

    # page: exact window, deterministic
    page = svc.slice_sync(artifact, offset=10, limit=5)
    assert [r[0] for r in page["rows"]] == [10, 11, 12, 13, 14]
    assert page["total_matches"] == len(df)
    assert page["next_offset"] == 15

    # grep: finds exactly the needle
    hit = svc.slice_sync(artifact, match="NEEDLE-\\d+")
    assert hit["total_matches"] == 1
    assert any("NEEDLE-4242" in str(v) for v in hit["rows"][0])

    # grep restricted to one column
    hit2 = svc.slice_sync(artifact, match="^NEEDLE", match_column="msg")
    assert hit2["total_matches"] == 1

    # projection
    proj = svc.slice_sync(artifact, columns=["msg"], limit=1)
    assert proj["columns"] == ["msg"]

    # time window (ts column detected + sorted)
    assert artifact.ts_column == "ts"
    win = svc.slice_sync(artifact, time_from="2026-01-01 00:10:00", time_to="2026-01-01 00:11:00")
    assert win["total_matches"] == 61

    # SELECT-only sql aggregate matches pandas ground truth
    agg = svc.slice_sync(artifact, sql="SELECT count(*) AS n, sum(amount) AS s FROM data")
    assert agg["rows"][0][0] == len(df)
    assert abs(agg["rows"][0][1] - df["amount"].sum()) < 1e-6


# --- invariant 6: SQL jail ----------------------------------------------------

ATTACKS = [
    "ATTACH ':memory:' AS evil",
    "COPY data TO '/tmp/out.csv'",
    "INSTALL httpfs",
    "LOAD httpfs",
    "PRAGMA database_list",
    "CREATE TABLE x AS SELECT 1",
    "INSERT INTO data VALUES (1)",
    "DELETE FROM data",
    "UPDATE data SET id=0",
    "DROP TABLE data",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM data; SELECT * FROM data",
    "SET enable_external_access=true",
    "EXPORT DATABASE '/tmp/x'",
    "SELECT getenv('HOME')",
]


@pytest.mark.parametrize("attack", ATTACKS)
def test_sql_jail_rejects(attack):
    assert validate_slice_sql(attack) is not None


def test_sql_jail_allows_plain_select():
    assert validate_slice_sql("SELECT count(*) FROM data GROUP BY 1") is None
    assert validate_slice_sql("WITH t AS (SELECT * FROM data) SELECT count(*) FROM t") is None


@pytest.mark.asyncio
async def test_sql_jail_enforced_end_to_end(svc, adb):
    artifact, _ = await _spill_and_persist(svc, adb)
    for attack in ATTACKS:
        with pytest.raises(Exception):
            svc.slice_sync(artifact, sql=attack)


# --- invariant 7: privacy gate -------------------------------------------------

@pytest.mark.asyncio
async def test_privacy_gate_no_raw_rows(svc, adb):
    artifact, _ = await _spill_and_persist(svc, adb)
    res = svc.slice_sync(artifact, match="NEEDLE", allow_llm_see_data=False)
    assert "rows" not in res
    assert res["rows_hidden"] is True
    assert res["total_matches"] == 1
    # raw SELECT rejected in privacy mode; aggregate allowed
    with pytest.raises(ValueError):
        svc.slice_sync(artifact, sql="SELECT * FROM data", allow_llm_see_data=False)
    agg = svc.slice_sync(artifact, sql="SELECT count(*) FROM data", allow_llm_see_data=False)
    assert agg is not None


# --- invariant 8: bounded page --------------------------------------------------

@pytest.mark.asyncio
async def test_bounded_page_and_cursor_walk(svc, adb):
    artifact, df = await _spill_and_persist(svc, adb)
    # oversized limit is capped server-side
    big = svc.slice_sync(artifact, limit=10_000)
    assert len(big["rows"]) <= SLICE_MAX_ROWS
    assert big["truncated"] is True
    # cursor walk reconstructs everything exactly once
    seen, offset = [], 0
    while offset is not None:
        page = svc.slice_sync(artifact, offset=offset, limit=500)
        seen.extend(r[0] for r in page["rows"])
        offset = page["next_offset"]
    assert seen == list(range(len(df)))


# --- invariant 9: retention state machine ---------------------------------------

@pytest.mark.asyncio
async def test_retention_shreds_expired_but_never_cited_or_held(svc, adb):
    a1, _ = await _spill_and_persist(svc, adb)
    a2, _ = await _spill_and_persist(svc, adb)
    a3, _ = await _spill_and_persist(svc, adb)
    past = datetime.utcnow() - timedelta(days=1)
    a1.expires_at = past                       # expired, unprotected -> shred
    a2.expires_at = past; a2.cited = True      # expired but cited -> survives
    a3.expires_at = past; a3.legal_hold = True # expired but held -> survives
    for a in (a1, a2, a3):
        adb.add(a)
    await adb.commit()

    n = await svc.purge_expired(adb)
    assert n == 1
    await adb.refresh(a1); await adb.refresh(a2); await adb.refresh(a3)
    assert a1.status == "tombstoned" and a1.wrapped_key is None
    assert not svc.storage.exists(a1.storage_ref)
    assert a2.status == "published" and svc.storage.exists(a2.storage_ref)
    assert a3.status == "published" and svc.storage.exists(a3.storage_ref)
    # tombstoned handle answers with a clear error, not a 500
    with pytest.raises(ValueError):
        svc._attach_readonly(a1)


# --- invariant 10: rerun immutability --------------------------------------------

@pytest.mark.asyncio
async def test_rerun_supersedes_never_overwrites(svc, adb):
    step_id = str(uuid.uuid4())
    old, old_df = await _spill_and_persist(svc, adb, step_id=step_id)
    old_sha = old.content_sha256
    # rerun: new data, same step
    new_df = _df(6000, needle_at=1)
    new, _ = await _spill_and_persist(svc, adb, df=new_df, step_id=step_id, producer="rerun")

    await adb.refresh(old)
    assert old.superseded_by == new.id
    assert new.superseded_by is None
    # the old payload is frozen and still slices identically
    assert old.content_sha256 == old_sha
    page = svc.slice_sync(old, offset=0, limit=3)
    assert page["total_matches"] == len(old_df)
    # latest_for_step resolves to the new artifact
    latest = await svc.latest_for_step(adb, ORG, step_id=step_id)
    assert latest.id == new.id


# --- misc: storage_ref traversal guard --------------------------------------------

def test_storage_ref_traversal_rejected(svc):
    with pytest.raises(ValueError):
        svc.storage.abs_path("../../etc/passwd")
    with pytest.raises(ValueError):
        svc.storage.abs_path("/etc/passwd")


# --- read_full: complete payload for sandbox load_step ----------------------------

@pytest.mark.asyncio
async def test_read_full_returns_complete_payload(svc, adb):
    """load_step depends on read_full returning EVERY row — not a slice-capped
    page — so cross-source joins in the sandbox run on full fidelity."""
    df = _df(7000, needle_at=6999)
    artifact, _ = await _spill_and_persist(svc, adb, df=df)
    out = svc.read_full_sync(artifact)
    assert len(out) == len(df)
    assert list(out.columns) == list(df.columns)
    # the tail row (beyond any slice page cap) is present and intact
    assert "NEEDLE" in str(out.iloc[-1].to_dict())
