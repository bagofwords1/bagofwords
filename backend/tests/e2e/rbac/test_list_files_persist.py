"""list_files persist=true — the whole listing saved as a CSV file in the report.

Contracts:
- The file holds EXACTLY the catalog's matching set (folder_id, recursive,
  name_pattern, dates, group_by), in sort order — the same rows a paged
  catalog listing returns across every page. limit only sizes the preview.
- It is loadable by create_data's file path (source_file_ids → excel_files →
  pd.read_csv), in the same turn and in a later turn, and joined with an Agent
  List table it yields exactly the folders that have no row yet.
- Persist never crawls live and never pages: cursor, source='live' and an
  unindexed connection are refused without writing a file. A row cap cuts
  the file and says so.
- Per-user connections are refused: a report file is readable by admins and
  anyone the conversation is shared with, so one user's listing would leak.
- The saved file follows the report's file access: its owner can download it,
  a member who cannot open the report cannot download or load it.

Seeding goes through the API (users, agent, list, reports, index refresh);
the tool runs as AgentV2 runs it, with a runtime ctx built from the DB. The
filesystem under the network_dir connection is the only "external" system.
"""
import asyncio
import io
import os
import uuid

import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.e2e

CASES = 23  # deliberately not a round number


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _db_url():
    return (os.environ["TEST_DATABASE_URL"]
            .replace("sqlite://", "sqlite+aiosqlite://", 1)
            .replace("postgresql://", "postgresql+asyncpg://", 1))


def run_async(fn):
    async def _run():
        engine = create_async_engine(_db_url())
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                return await fn(db, maker)
        finally:
            await engine.dispose()
    return asyncio.run(_run())


def _case_name(i):
    # Mixed Hebrew / Latin case folders, like the customer's share.
    return f"תיק-{i:04d}" if i % 3 == 0 else f"Case {i:04d}"


def _build_tree(root):
    """CASES case folders; case i holds (i % 4) + 1 files at the top and, for
    every other case, a nested Correspondence/ subfolder. Plus loose files."""
    expected = {}
    for i in range(CASES):
        case = root / _case_name(i)
        case.mkdir()
        files = []
        for j in range(i % 4 + 1):
            ext = "pdf" if j % 2 == 0 else "docx"
            p = case / f"doc-{j}.{ext}"
            p.write_text(f"case {i} doc {j}")
            files.append(p)
        if i % 2 == 0:
            sub = case / "Correspondence"
            sub.mkdir()
            p = sub / "מכתב.txt"
            p.write_text(f"letter {i}")
            files.append(p)
        # Spread modification times over 2024 (odd cases) and 2026 (even).
        ts = (1_780_000_000 if i % 2 == 0 else 1_710_000_000) + i * 3600
        for p in files:
            os.utime(p, (ts, ts))
        expected[_case_name(i)] = len(files)
    (root / "index.xlsx").write_text("loose")
    (root / "readme.txt").write_text("loose")
    return expected


@pytest.fixture
def world(tmp_path, bootstrap_admin, invite_user_to_org, create_data_source, grant_resource,
          create_report, test_client):
    share = tmp_path / "share"
    share.mkdir()
    expected = _build_tree(share)
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    ds = create_data_source(name=f"Cases {uuid.uuid4().hex[:4]}", type="network_dir",
                            config={"root_path": str(share)}, credentials={"auth_type": "none"},
                            user_token=admin["token"], org_id=org_id)
    # Index the share into the catalog through the real refresh route.
    r = test_client.get(f"/api/data_sources/{ds['id']}/refresh_schema", headers=_h(admin["token"], org_id))
    assert r.status_code == 200, r.text
    conns = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=_h(admin["token"], org_id)).json()

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    outsider = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    for who in (member, outsider):
        g = grant_resource(resource_type="data_source", resource_id=ds["id"], principal_type="user",
                           principal_id=who["user_id"], permissions=["view"], user_token=admin["token"], org_id=org_id)
        assert g.status_code in (200, 201), g.text

    lst = test_client.post(f"/api/data_sources/{ds['id']}/lists", headers=_h(admin["token"], org_id), json={
        "name": "Case review", "description": "One row per case folder",
        "fields": [{"name": "case_id", "type": "string", "required": True, "description": "Case folder name"},
                   {"name": "summary", "type": "string"}],
        "key_field": "case_id", "require_evidence": False,
    })
    assert lst.status_code == 201, lst.text

    reports = {}
    for key, who in (("admin", admin), ("member", member), ("outsider", outsider)):
        reports[key] = create_report(title=f"Review {key}", user_token=who["token"], org_id=org_id,
                                     data_sources=[ds["id"]])
    return {"org_id": org_id, "admin": admin, "member": member, "outsider": outsider, "ds": ds,
            "conn_id": conns[0]["id"], "list": lst.json(), "reports": reports, "expected": expected,
            "share": share}


async def _ctx(db, world, actor, report_key=None):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models.data_source import DataSource
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    org = await db.get(Organization, world["org_id"])
    report = (await db.execute(
        select(Report).where(Report.id == world["reports"][report_key or actor]["id"])
        .options(selectinload(Report.data_sources).selectinload(DataSource.connections),
                 selectinload(Report.files))
    )).scalar_one()
    # Each report here is run by its owner (completions are owner-only).
    user = await db.get(User, report.user_id)
    # As AgentV2 hands it to tools: the report's files are the loadable set.
    return {"db": db, "organization": org, "user": user, "report": report, "excel_files": list(report.files)}


async def _uid(db, world, actor):
    from sqlalchemy import select

    from app.models.report import Report
    return (await db.execute(select(Report.user_id).where(Report.id == world["reports"][actor]["id"]))).scalar_one()


def list_files(world, actor="admin", report_key=None, **inputs):
    """Run list_files and, when a file was saved, load it the way create_data
    does (source_file_ids → excel_files → pd.read_csv)."""
    from app.ai.tools.implementations._source_files import resolve_source_files
    from app.ai.tools.implementations.list_files import ListFilesTool

    async def _fn(db, maker):
        ctx = await _ctx(db, world, actor, report_key)
        last = None
        async for ev in ListFilesTool().run_stream({"connection_id": world["conn_id"], **inputs}, ctx):
            last = ev
        await db.commit()
        out = last.payload
        df = None
        if out["output"].get("file_id"):
            scoped, _, missing = resolve_source_files(ctx, [out["output"]["file_id"]])
            assert not missing and len(scoped) == 1
            df = pd.read_csv(scoped[0].path, keep_default_na=False, dtype=str)
        return out, df
    return run_async(_fn)


def catalog_rows(world, **inputs):
    """Every row of the non-persisted catalog listing, across all pages."""
    rows, cursor = [], None
    while True:
        out, _ = list_files(world, source="catalog", limit=7, **inputs, **({"cursor": cursor} if cursor else {}))
        out = out["output"]
        assert out["success"], out
        rows += out["folders"] if inputs.get("group_by") == "folder" else out["files"]
        cursor = out["next_cursor"]
        if not cursor:
            return rows


def report_file_count(world, actor="admin"):
    from sqlalchemy import func, select

    from app.models.report_file_association import report_file_association

    async def _fn(db, maker):
        return (await db.execute(select(func.count()).select_from(report_file_association).where(
            report_file_association.c.report_id == world["reports"][actor]["id"]))).scalar_one()
    return run_async(_fn)


# ── the file holds exactly the catalog's matching set ─────────────────────

@pytest.mark.parametrize("filters", [
    {"recursive": True},
    {"recursive": True, "sort_by": "modified_at", "sort_order": "desc"},
    {"recursive": True, "name_pattern": "*.pdf"},
    {"recursive": True, "modified_after": "2026-01-01"},
    {"folder_id": "Case 0004", "recursive": True},
    {"folder_id": "Case 0004"},  # not recursive: the subfolder's file is excluded
])
def test_persisted_files_equal_the_catalog_matching_set(world, filters):
    expected = catalog_rows(world, **filters)
    assert expected, filters
    out, df = list_files(world, persist=True, **filters)
    o = out["output"]
    assert o["success"] and o["persisted"] and o["source"] == "catalog", o
    assert o["row_count"] == len(df) == len(expected) == o["matched"]
    assert list(df["path"]) == [f["path"] for f in expected]  # same rows, same order
    assert list(df["id"]) == [f["id"] for f in expected]
    assert o["truncated"] is False and o["indexed_at"] and set(df["indexed_at"]) == {o["indexed_at"]}
    # Folder levels let code group without parsing paths.
    for row in df.to_dict("records"):
        parts = row["path"].split("/")
        assert row["folder"] == "/".join(parts[:-1]) and row["name"] == parts[-1]
        for i, seg in enumerate(parts[:-1], start=1):
            assert row[f"folder_{i}"] == seg
        assert row["ext"] == (parts[-1].rsplit(".", 1)[-1].lower() if "." in parts[-1] else "")
    assert {"id", "path", "folder", "folder_1", "name", "ext", "size", "modified_at", "created_at",
            "web_url", "indexed_at"} <= set(df.columns) == set(o["columns"])
    assert o["folder_count"] == len({p.rsplit("/", 1)[0] for p in df["path"] if "/" in p})


def test_whole_share_round_trips_every_indexed_file(world):
    out, df = list_files(world, persist=True, recursive=True)
    total = sum(world["expected"].values()) + 2  # + the two loose files
    assert out["output"]["row_count"] == total == out["output"]["catalog_files"]
    per_case = df[df["folder_1"] != ""].groupby("folder_1").size().to_dict()
    assert per_case == world["expected"]


@pytest.mark.parametrize("depth", [1, 2])
def test_persisted_folders_equal_the_folder_listing(world, depth):
    expected = catalog_rows(world, group_by="folder", depth=depth)
    out, df = list_files(world, persist=True, group_by="folder", depth=depth)
    o = out["output"]
    assert o["success"] and o["group_by"] == "folder"
    assert o["row_count"] == o["folder_count"] == len(df) == len(expected)
    assert list(df["path"]) == [f["path"] for f in expected]
    assert [int(c) for c in df["file_count"]] == [f["file_count"] for f in expected]
    assert {int(d) for d in df["depth"]} == {depth}
    assert {"path", "name", "parent", "depth", "folder_1", "file_count", "latest_modified_at",
            "earliest_created_at", "indexed_at"} <= set(df.columns)
    if depth == 1:
        assert dict(zip(df["path"], df["file_count"].astype(int))) == world["expected"]


def test_preview_size_follows_limit_and_defaults_small(world):
    out, df = list_files(world, persist=True, recursive=True, limit=3)
    assert len(out["output"]["files"]) == 3 and len(df) > 3
    assert out["observation"]["details"].count("\n") == 3  # 3 rows + the "more in file" line
    out, df = list_files(world, persist=True, recursive=True)
    n = len(out["output"]["files"])
    assert 0 < n < len(df) and n <= 20
    # The observation stays small: it names the file instead of carrying rows.
    obs = out["observation"]
    assert obs["file_id"] == out["output"]["file_id"] in obs["summary"]
    assert len(obs["details"].splitlines()) == n + 1


def test_non_recursive_folder_listing_says_what_it_left_out(world):
    case = _case_name(4)  # holds a Correspondence/ subfolder
    out, df = list_files(world, persist=True, folder_id=case)
    assert out["output"]["row_count"] == len(df) == world["expected"][case] - 1
    assert "recursive=true" in out["observation"]["summary"]
    out, df = list_files(world, persist=True, folder_id=case, recursive=True)
    assert len(df) == world["expected"][case] and "recursive=true" not in out["observation"]["summary"]


# ── refusals: no partial files, no live crawls ────────────────────────────

def test_cursor_live_and_unindexed_are_refused_without_writing_a_file(world, create_data_source, test_client,
                                                                     create_report):
    before = report_file_count(world)
    page, _ = list_files(world, source="catalog", recursive=True, limit=5)
    cursor = page["output"]["next_cursor"]
    assert cursor
    for inputs in ({"cursor": cursor, "recursive": True}, {"source": "live", "recursive": True}):
        out, df = list_files(world, persist=True, **inputs)
        assert out["output"]["success"] is False and df is None, inputs
        assert not out["output"].get("file_id")

    # A connection without a catalog cannot be persisted either.
    admin = world["admin"]
    bare = create_data_source(name=f"Unindexed {uuid.uuid4().hex[:4]}", type="network_dir",
                              config={"root_path": str(world["share"]), "index_mode": "none"},
                              credentials={"auth_type": "none"}, user_token=admin["token"], org_id=world["org_id"])
    conn_id = test_client.get(f"/api/data_sources/{bare['id']}/connections",
                              headers=_h(admin["token"], world["org_id"])).json()[0]["id"]
    world["reports"]["bare"] = create_report(title="bare", user_token=admin["token"], org_id=world["org_id"],
                                             data_sources=[bare["id"]])
    world_bare = {**world, "conn_id": conn_id}
    out, _ = list_files(world_bare, actor="admin", report_key="bare", persist=True, recursive=True)
    assert out["output"]["success"] is False and "catalog" in out["output"]["error"]
    # The live listing itself still works there.
    out, _ = list_files(world_bare, actor="admin", report_key="bare", recursive=True)
    assert out["output"]["success"] and out["output"]["file_count"] > 0
    assert report_file_count(world) == before


def test_row_cap_truncates_and_flags(world, monkeypatch):
    from app.ai.tools.implementations import _file_catalog

    monkeypatch.setattr(_file_catalog, "PERSIST_MAX_ROWS", 5)
    out, df = list_files(world, persist=True, recursive=True)
    o = out["output"]
    assert o["truncated"] is True and o["row_count"] == 5 == len(df) and o["matched"] > 5
    assert "TRUNCATED" in out["observation"]["summary"]
    full = catalog_rows(world, recursive=True)
    assert list(df["path"]) == [f["path"] for f in full[:5]]


# ── the work queue: folders without a list row, now and after a restart ──

def _submit(world, case_ids):
    from app.ai.tools.implementations.submit_list import SubmitListTool

    async def _fn(db, maker):
        ctx = await _ctx(db, world, "admin")
        ctx.update(session_maker=maker, tool_call_id=str(uuid.uuid4()))
        records = [{"row_id": None, "fields": {
            "case_id": {"value": c, "status": "found", "evidence": [], "note": None},
            "summary": {"value": f"reviewed {c}", "status": "found", "evidence": [], "note": None}}}
            for c in case_ids]
        last = None
        async for ev in SubmitListTool().run_stream({"list_id": world["list"]["id"], "records": records}, ctx):
            last = ev
        await db.commit()
        return last.payload
    out = run_async(_fn)
    assert out["output"].get("success", True) is not False, out


def _list_df(world):
    from app.models.organization import Organization
    from app.models.user import User
    from app.schemas.bow_source_schema import BowQuery
    from app.services.agent_lists.bow_lists import query_list

    async def _fn(db, maker):
        user = await db.get(User, await _uid(db, world, "admin"))
        org = await db.get(Organization, world["org_id"])
        return await query_list(db, org, user, BowQuery.model_validate(
            {"dataset": "list", "list_id": world["list"]["id"]}))
    return run_async(_fn)


def _load_in_new_turn(world, file_id, actor="admin", report_key=None):
    """A later turn: fresh session, report re-read from the DB."""
    from app.ai.tools.implementations._source_files import resolve_source_files

    async def _fn(db, maker):
        ctx = await _ctx(db, world, actor, report_key)
        scoped, _, missing = resolve_source_files(ctx, [file_id])
        return (pd.read_csv(scoped[0].path, keep_default_na=False, dtype=str) if scoped else None), missing
    return run_async(_fn)


def test_work_queue_is_exactly_the_folders_without_a_row(world):
    out, folders = list_files(world, persist=True, group_by="folder")
    file_id = out["output"]["file_id"]
    done = [_case_name(i) for i in (0, 3, 4, 11, 22)]
    _submit(world, done)

    # What the generated create_data code does with the two inputs.
    def queue(folders_df):
        rows = _list_df(world)
        return sorted(set(folders_df["path"]) - set(rows["case_id"]))

    assert queue(folders) == sorted(set(world["expected"]) - set(done))

    # Interrupted turn → "continue": the file is still on the report and the
    # queue shrinks by exactly the rows saved since.
    more = [_case_name(i) for i in (1, 2)]
    _submit(world, more)
    reloaded, missing = _load_in_new_turn(world, file_id)
    assert not missing and reloaded.equals(folders)
    assert queue(reloaded) == sorted(set(world["expected"]) - set(done) - set(more))


# ── permissions ──────────────────────────────────────────────────────────

def test_member_persists_in_own_report_and_others_cannot_read_it(world, test_client):
    out, df = list_files(world, actor="member", persist=True, recursive=True)
    assert out["output"]["success"], out
    file_id = out["output"]["file_id"]
    org = world["org_id"]

    got = test_client.get(f"/api/files/{file_id}/content", headers=_h(world["member"]["token"], org))
    assert got.status_code == 200
    assert pd.read_csv(io.BytesIO(got.content), keep_default_na=False, dtype=str).equals(df)

    # A member who cannot open the report can neither download the file nor
    # load it from their own report by id.
    denied = test_client.get(f"/api/files/{file_id}/content", headers=_h(world["outsider"]["token"], org))
    assert denied.status_code in (403, 404)
    loaded, missing = _load_in_new_turn(world, file_id, actor="outsider")
    assert loaded is None and missing == [file_id]
    listed = test_client.get(f"/api/reports/{world['reports']['member']['id']}/files",
                             headers=_h(world["outsider"]["token"], org))
    assert listed.status_code in (403, 404)


def test_per_user_connection_is_refused_for_admin_and_member(world):
    from sqlalchemy import select

    from app.models.connection import Connection

    async def _flip(db, maker):
        # Direct write: a network_dir connection cannot be created per-user via
        # the API; the contract under test is list_files' auth_policy gate.
        conn = (await db.execute(select(Connection).where(Connection.id == world["conn_id"]))).scalar_one()
        conn.auth_policy = "user_required"
        await db.commit()
    run_async(_flip)
    for actor in ("admin", "member"):
        before = report_file_count(world, actor)
        out, df = list_files(world, actor=actor, persist=True, recursive=True)
        assert out["output"]["success"] is False and df is None, (actor, out)
        for mode in ({"group_by": "folder"}, {"source": "catalog"}):
            out, df = list_files(world, actor=actor, persist=True, **mode)
            assert out["output"]["success"] is False and df is None
        assert report_file_count(world, actor) == before
