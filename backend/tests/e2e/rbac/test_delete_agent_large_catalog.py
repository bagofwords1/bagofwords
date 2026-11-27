"""Deleting an agent with a large catalog is set-based and leaves nothing behind.

A file agent (OneDrive, SharePoint, Drive, S3) keeps one catalog row per file —
thousands of them. The delete used to load and remove each row through the ORM
(a DELETE plus a lazy SELECT per child relationship), so its cost grew with the
catalog and deleting such an agent took minutes. These tests pin the contract:
the work does not grow with the number of catalog rows, every row hanging off
the agent is gone afterwards, and another agent's rows are untouched.
"""
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine


SEEDED_PREFIX = "folder/file_"
# Tables whose rows hang off each catalog row. A per-row delete reads or
# writes these once per catalog row; a set-based one a fixed number of times.
PER_ROW_TABLES = ("table_stats", "table_usage_events", "table_feedback_events", "metadata_resources")


async def _seed_catalog(data_source_id: str, org_id: str, count: int) -> None:
    # The API cannot produce a large catalog with per-table stats and indexing
    # history without a real file connector, so these rows are written directly.
    from app.dependencies import async_session_maker
    from app.models.datasource_table import DataSourceTable
    from app.models.table_stats import TableStats
    from app.models.metadata_indexing_job import MetadataIndexingJob
    from app.models.metadata_resource import MetadataResource

    async with async_session_maker() as db:
        job = MetadataIndexingJob(organization_id=org_id, data_source_id=data_source_id)
        db.add(job)
        await db.flush()
        for i in range(count):
            name = f"{SEEDED_PREFIX}{i}_{uuid.uuid4().hex[:6]}.xlsx"
            table = DataSourceTable(name=name, datasource_id=data_source_id, columns=[], pks=[], fks=[])
            db.add(table)
            await db.flush()
            db.add(TableStats(
                org_id=org_id, data_source_id=data_source_id,
                datasource_table_id=table.id, table_fqn=name,
            ))
            db.add(MetadataResource(
                name=name, resource_type="file", data_source_id=data_source_id,
                metadata_indexing_job_id=job.id,
            ))
        await db.commit()


async def _counts(data_source_id: str, *, seeded_only: bool = False) -> dict:
    """Rows of each kind for the agent. ``seeded_only`` counts just the rows
    _seed_catalog wrote: creating an agent starts background indexing of its
    real tables, which may still be adding catalog rows while the test runs."""
    from app.dependencies import async_session_maker
    from app.models.datasource_table import DataSourceTable
    from app.models.table_stats import TableStats
    from app.models.metadata_indexing_job import MetadataIndexingJob
    from app.models.metadata_resource import MetadataResource

    async with async_session_maker() as db:
        async def n(model, col, *extra):
            q = select(func.count()).select_from(model).where(col == data_source_id, *extra)
            return (await db.execute(q)).scalar()
        seeded_table = (DataSourceTable.name.like(f"{SEEDED_PREFIX}%"),) if seeded_only else ()
        seeded_stats = (TableStats.table_fqn.like(f"{SEEDED_PREFIX}%"),) if seeded_only else ()
        return {
            "tables": await n(DataSourceTable, DataSourceTable.datasource_id, *seeded_table),
            "stats": await n(TableStats, TableStats.data_source_id, *seeded_stats),
            "resources": await n(MetadataResource, MetadataResource.data_source_id),
            "jobs": await n(MetadataIndexingJob, MetadataIndexingJob.data_source_id),
        }


@contextmanager
def _count_statements():
    seen = []

    def _on_execute(conn, cursor, statement, parameters, context, executemany):
        # Only statements on the per-row child tables: background indexing of
        # the agents' real tables runs alongside and touches the catalog itself.
        if any(name in statement for name in PER_ROW_TABLES):
            seen.append(statement)

    event.listen(Engine, "before_cursor_execute", _on_execute)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", _on_execute)


async def _delete_and_count(test_client, sqlite_data_source, delete_data_source, token, org_id, catalog_size):
    ds = sqlite_data_source(name=f"files_{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id)
    await _seed_catalog(ds["id"], org_id, catalog_size)
    with _count_statements() as statements:
        result = delete_data_source(data_source_id=ds["id"], user_token=token, org_id=org_id)
    assert result.get("message"), result
    return ds["id"], len(statements)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_agent_delete_work_does_not_grow_with_catalog_size(
    test_client, bootstrap_admin, sqlite_data_source, delete_data_source,
):
    admin = bootstrap_admin("del_large")
    token, org_id = admin["token"], admin["org_id"]

    _, small = await _delete_and_count(test_client, sqlite_data_source, delete_data_source, token, org_id, 3)
    _, large = await _delete_and_count(test_client, sqlite_data_source, delete_data_source, token, org_id, 60)

    # 57 more catalog rows, each with a stats row and a metadata resource. A
    # per-row delete touches the child tables several times per row (hundreds
    # of statements here); a set-based one the same number of times either way.
    # A few statements of slack for incidental, size-independent differences
    # between two requests; the per-row cost would add over 200 here.
    assert large - small <= 5, (small, large)


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_size", [1, 25])
async def test_agent_delete_removes_every_catalog_row_and_only_its_own(
    catalog_size, test_client, bootstrap_admin, sqlite_data_source, delete_data_source,
):
    admin = bootstrap_admin(f"del_rows{catalog_size}")
    token, org_id = admin["token"], admin["org_id"]
    keep = sqlite_data_source(name=f"keep_{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id)
    await _seed_catalog(keep["id"], org_id, 4)
    kept_before = await _counts(keep["id"], seeded_only=True)

    gone_id, _ = await _delete_and_count(
        test_client, sqlite_data_source, delete_data_source, token, org_id, catalog_size,
    )

    assert await _counts(gone_id) == {"tables": 0, "stats": 0, "resources": 0, "jobs": 0}
    assert await _counts(keep["id"], seeded_only=True) == kept_before
    listing = test_client.get(
        "/api/data_sources",
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
    )
    assert listing.status_code == 200
    ids = {d["id"] for d in listing.json()}
    assert gone_id not in ids and keep["id"] in ids


async def _seed_git_indexed_resources(data_source_id: str, org_id: str, user_id: str, count: int) -> list:
    # A git-indexed agent (dbt / LookML) holds a repository, its indexing job
    # and the resources that job produced; the API needs a reachable repository
    # to produce them, so they are written directly.
    from app.dependencies import async_session_maker
    from app.models.git_repository import GitRepository
    from app.models.metadata_indexing_job import MetadataIndexingJob
    from app.models.metadata_resource import MetadataResource

    async with async_session_maker() as db:
        repo = GitRepository(
            provider="github", branch="main", repo_url=f"https://example.com/{uuid.uuid4().hex[:8]}.git",
            user_id=user_id, data_source_id=data_source_id, organization_id=org_id,
        )
        db.add(repo)
        await db.flush()
        job = MetadataIndexingJob(organization_id=org_id, data_source_id=data_source_id, git_repository_id=repo.id)
        db.add(job)
        await db.flush()
        resources = [
            MetadataResource(
                name=f"model_{i}", resource_type="model", data_source_id=data_source_id,
                metadata_indexing_job_id=job.id,
            )
            for i in range(count)
        ]
        db.add_all(resources)
        await db.commit()
        return [r.id for r in resources]


async def _link_instruction_to_resource(instruction_id: str, resource_id: str) -> None:
    # Provenance pointer the git sync sets; no API writes it.
    from sqlalchemy import update
    from app.dependencies import async_session_maker
    from app.models.instruction import Instruction

    async with async_session_maker() as db:
        await db.execute(
            update(Instruction).where(Instruction.id == instruction_id).values(source_metadata_resource_id=resource_id)
        )
        await db.commit()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_deleting_git_indexed_agent_keeps_instruction_shared_with_another_agent(
    test_client, bootstrap_admin, sqlite_data_source, delete_data_source,
):
    """The repository, its job and resources go with the agent; an instruction
    the agent shares with another agent survives, though it was synced from one
    of the deleted resources."""
    from app.dependencies import async_session_maker
    from app.models.git_repository import GitRepository

    admin = bootstrap_admin("del_git")
    token, org_id = admin["token"], admin["org_id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}
    gone = sqlite_data_source(name=f"dbt_{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id)
    other = sqlite_data_source(name=f"other_{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id)
    resource_ids = await _seed_git_indexed_resources(gone["id"], org_id, admin["user_id"], 6)

    resp = test_client.post(
        "/api/instructions",
        json={"text": f"shared rule {uuid.uuid4().hex[:6]}", "status": "published",
              "category": "general", "load_mode": "always", "data_source_ids": [gone["id"], other["id"]]},
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.json()
    shared_id = resp.json()["id"]
    await _link_instruction_to_resource(shared_id, resource_ids[0])

    result = delete_data_source(data_source_id=gone["id"], user_token=token, org_id=org_id)
    assert result.get("message"), result

    counts = await _counts(gone["id"])
    assert counts["resources"] == 0 and counts["jobs"] == 0
    async with async_session_maker() as db:
        repos = (await db.execute(
            select(func.count()).select_from(GitRepository).where(GitRepository.data_source_id == gone["id"])
        )).scalar()
    assert repos == 0
    instr = test_client.get(f"/api/instructions/{shared_id}", headers=headers)
    assert instr.status_code == 200, instr.json()
    assert [d["id"] for d in instr.json()["data_sources"]] == [other["id"]]
