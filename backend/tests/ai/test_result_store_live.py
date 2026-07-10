"""Live AI test (Anthropic): create_data results above the spill floor dual-write
their FULL payload to the result store, and the agent can then slice the
complete data via read_query slice mode — not just the truncated preview.

The spill byte-floor is lowered via BOW_RESULT_STORE_FLOOR_BYTES so the spill
trigger does not depend on the LLM writing LIMIT-free SQL; the invariants
asserted are payload completeness (handle row_count == the step's true
total_rows) and end-to-end sliceability, not any specific row count.

Run:
  ANTHROPIC_API_KEY_TEST=sk-ant-... BOW_RESULT_STORE_FLOOR_BYTES=500 \
    pytest -s -m ai --db=sqlite tests/ai/test_result_store_live.py

Skips cleanly when ANTHROPIC_API_KEY_TEST is unset.
"""
import asyncio
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

_SQLITE_DB = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _all_tool_names(completions):
    names = []
    for c in completions:
        for b in c.get("completion_blocks") or []:
            te = b.get("tool_execution") or {}
            if te.get("tool_name"):
                names.append(te["tool_name"])
    return names


def _blocks_text(completions, limit=400):
    return " | ".join(
        (b.get("content") or "")[:limit]
        for c in completions for b in (c.get("completion_blocks") or [])
    )


@pytest.mark.ai
def test_create_data_spills_and_agent_slices_full_result(
    create_user, login_user, whoami, test_client,
    create_data_source, create_report, create_completion,
    tmp_path,
):
    if not os.getenv("ANTHROPIC_API_KEY_TEST"):
        pytest.skip("ANTHROPIC_API_KEY_TEST is not set")

    os.environ["BOW_RESULT_STORE_PATH"] = str(tmp_path / "results")
    # The floor is read at service import; require it to have been set low for
    # this run so any real result spills regardless of LLM-added LIMITs.
    from app.services import result_store as rs_mod
    if rs_mod.RESULT_STORE_FLOOR_BYTES > 10_000:
        rs_mod.RESULT_STORE_FLOOR_BYTES = 500  # module constant; test-scoped process

    email = f"liverf_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    org_id = whoami(token)["organizations"][0]["id"]

    resp = test_client.post(
        "/api/llm/providers",
        json={
            "name": "anthropic",
            "provider_type": "anthropic",
            "credentials": {"api_key": os.getenv("ANTHROPIC_API_KEY_TEST")},
            "models": [
                {"model_id": "claude-haiku-4-5-20251001", "name": "Claude 4.5 Haiku", "is_custom": False},
            ],
        },
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()

    ds = create_data_source(
        name="chinook", type="sqlite", config={"database": str(_SQLITE_DB)},
        credentials={}, user_token=token, org_id=org_id,
    )
    # Schema indexing is async; without tables in context the planner asks a
    # clarifying question instead of calling create_data. Poll until the
    # schema is visible (bounded), so the test exercises spill, not the race.
    # Index the schema synchronously, then ACTIVATE the Track table — freshly
    # indexed tables are inactive until selected (the UI onboarding step), and
    # the agent's context only includes active tables.
    r = test_client.get(
        f"/api/data_sources/{ds['id']}/refresh_schema", headers=_auth(token, org_id)
    )
    assert r.status_code == 200, r.text[:300]
    r = test_client.put(
        f"/api/data_sources/{ds['id']}/update_tables_status",
        json={"activate": ["Track"], "deactivate": []},
        headers=_auth(token, org_id),
    )
    assert r.status_code == 200, r.text[:300]
    sch = test_client.get(
        f"/api/data_sources/{ds['id']}/schema", headers=_auth(token, org_id)
    )
    assert sch.status_code == 200 and any(
        "track" in str(t).lower() for t in (sch.json() or [])
    ), f"Track not active in schema: {str(sch.json())[:300]}"

    report = create_report(
        title="Spill test", user_token=token, org_id=org_id, data_sources=[ds["id"]],
    )

    # --- 1: produce a dataset (Track has 3503 rows; even if the model caps it,
    # the lowered byte floor guarantees the full result spills) ---
    completions = create_completion(
        report_id=report["id"],
        prompt=(
            "Using create_data, select ALL rows from the Track table with columns "
            "TrackId and Name, ordered by TrackId. Do not use any LIMIT — I want "
            "every row in one dataset. Do not create a chart, just the table."
        ),
        user_token=token, org_id=org_id,
    )
    assert "create_data" in _all_tool_names(completions), (
        f"agent did not call create_data; tools: {_all_tool_names(completions)}; "
        f"blocks: {_blocks_text(completions)[:1200]}"
    )

    from app.dependencies import async_session_maker
    from app.models.result_file import ResultFile
    from app.models.step import Step
    from app.services.result_store import ResultStore

    async def _fetch():
        async with async_session_maker() as db:
            res = await db.execute(
                select(ResultFile).where(ResultFile.organization_id == str(org_id))
            )
            rfs = list(res.scalars().all())
            steps = {}
            for rf in rfs:
                if rf.step_id:
                    steps[rf.step_id] = (await db.get(Step, rf.step_id)).data
            return rfs, steps

    rfs, step_datas = asyncio.run(_fetch())
    assert len(rfs) >= 1, (
        "large create_data result did not spill to the result store; "
        f"blocks: {_blocks_text(completions)[:1200]}"
    )
    rf = max(rfs, key=lambda r: r.row_count)
    assert rf.producer == "create_data"
    assert rf.status == "published" and rf.wrapped_key and rf.content_sha256
    assert rf.step_id, "handle must be linked to the producing step"

    # Invariant: the payload holds the COMPLETE result — row_count equals the
    # step's true total, which is >= what the preview kept.
    preview = step_datas.get(rf.step_id) or {}
    true_total = int(((preview.get("info") or {}).get("total_rows")) or 0)
    assert rf.row_count == true_total, (
        f"payload rows ({rf.row_count}) != step's true total ({true_total})"
    )
    assert rf.row_count >= len(preview.get("rows") or []), "payload smaller than preview"

    # The stored payload itself answers slices.
    svc = ResultStore()
    s = svc.slice_sync(rf, sql="SELECT count(*) AS n FROM data")
    n_full = int(s["rows"][0][0])
    assert n_full == rf.row_count

    # --- 2: the agent slices the FULL result via read_query slice mode ---
    completions2 = create_completion(
        report_id=report["id"],
        prompt=(
            "Do NOT run create_data again and do not query the database. The previous "
            "query retained its full result (see the result_file block in the previous "
            "observation). Use the read_query tool in slice mode with that result_file_id "
            "and this sql: SELECT count(*) AS n FROM data — then tell me the exact value "
            "of n."
        ),
        user_token=token, org_id=org_id,
    )
    tools2 = _all_tool_names(completions2)
    assert "read_query" in tools2, f"agent did not call read_query; tools: {tools2}"

    blob = _blocks_text(completions2, limit=2000)
    outputs = " ".join(
        str((b.get("tool_execution") or {}).get("result_json") or "")
        for c in completions2 for b in (c.get("completion_blocks") or [])
    )
    expect = str(n_full)
    expect_commas = f"{n_full:,}"
    assert expect in blob or expect_commas in blob or expect in outputs, (
        f"agent did not surface the full-result count ({n_full}) — slice mode likely "
        f"failed; answer blob: {blob[:800]}"
    )
