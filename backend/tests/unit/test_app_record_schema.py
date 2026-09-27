"""The `app_records` table (artifact app persistence, migration apprec01).

Contract (design spec section 5): one row per app record, keyed by a fixed
envelope the server filters on (organization, report, artifact, collection,
user, version) and a free `data` JSON the app defines. The schema must be the
same on SQLite and Postgres: generic types only, the two composite lookup
indexes, and a foreign key for every envelope reference.
"""
import uuid

import pytest
import sqlalchemy as sa

from app.dependencies import async_session_maker


async def _inspect(fn):
    async with async_session_maker() as session:
        conn = await session.connection()
        return await conn.run_sync(lambda sync_conn: fn(sa.inspect(sync_conn)))


@pytest.mark.asyncio
async def test_app_records_table_has_envelope_columns_indexes_and_foreign_keys():
    tables = await _inspect(lambda insp: insp.get_table_names())
    assert "app_records" in tables

    columns = await _inspect(lambda insp: {c["name"]: c for c in insp.get_columns("app_records")})
    assert {
        "id", "created_at", "updated_at", "deleted_at",
        "organization_id", "report_id", "artifact_id", "collection",
        "user_id", "version", "data",
    } <= set(columns)
    for required in ("organization_id", "report_id", "artifact_id", "collection", "user_id", "version"):
        assert columns[required]["nullable"] is False, required
    assert isinstance(columns["version"]["type"], sa.Integer)
    # Portable JSON, never a dialect-specific JSONB.
    assert "JSONB" not in type(columns["data"]["type"]).__name__.upper()

    indexes = await _inspect(lambda insp: {i["name"]: list(i["column_names"]) for i in insp.get_indexes("app_records")})
    assert indexes.get("ix_app_records_artifact_collection") == ["artifact_id", "collection"]
    assert indexes.get("ix_app_records_artifact_collection_user") == ["artifact_id", "collection", "user_id"]
    single_column = {tuple(cols) for cols in indexes.values() if len(cols) == 1}
    assert ("organization_id",) in single_column
    assert ("report_id",) in single_column

    fks = await _inspect(lambda insp: {
        (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
        for fk in insp.get_foreign_keys("app_records")
    })
    assert {
        (("organization_id",), "organizations", ("id",)),
        (("report_id",), "reports", ("id",)),
        (("artifact_id",), "artifacts", ("id",)),
        (("user_id",), "users", ("id",)),
    } <= fks


async def _seed_parents(session) -> dict:
    """Minimal organization/user/report/artifact rows for the FK envelope.

    Direct table inserts on purpose: this is a schema test of a model with no
    service or route yet, and it must run identically on both dialects.
    """
    from app.models.artifact import Artifact
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    ids = {k: str(uuid.uuid4()) for k in ("org", "user", "report", "artifact")}
    await session.execute(sa.insert(Organization.__table__).values(id=ids["org"], name=f"org-{ids['org']}"))
    await session.execute(sa.insert(User.__table__).values(
        id=ids["user"], name="Record Author", email=f"{ids['user']}@example.com", hashed_password="x",
    ))
    await session.execute(sa.insert(Report.__table__).values(
        id=ids["report"], title="Revenue", slug=f"r-{ids['report']}",
        user_id=ids["user"], organization_id=ids["org"],
    ))
    await session.execute(sa.insert(Artifact.__table__).values(
        id=ids["artifact"], report_id=ids["report"], organization_id=ids["org"], created_by=ids["user"],
    ))
    await session.commit()
    return ids


@pytest.mark.asyncio
async def test_app_record_round_trips_through_the_orm_with_version_defaulting_to_one():
    from app.models.app_record import AppRecord

    payload = {"country": "France", "text": "check the drop", "tags": ["q3", 2], "done": False}
    async with async_session_maker() as session:
        ids = await _seed_parents(session)
        record = AppRecord(
            organization_id=ids["org"], report_id=ids["report"], artifact_id=ids["artifact"],
            collection="notes", user_id=ids["user"], data=payload,
        )
        session.add(record)
        await session.commit()
        record_id = record.id

    async with async_session_maker() as session:
        rows = (await session.execute(
            sa.select(AppRecord).where(
                AppRecord.artifact_id == ids["artifact"],
                AppRecord.collection == "notes",
                AppRecord.user_id == ids["user"],
            )
        )).scalars().all()

    assert [r.id for r in rows] == [record_id]
    stored = rows[0]
    assert stored.version == 1
    assert stored.data == payload
    assert stored.organization_id == ids["org"]
    assert stored.report_id == ids["report"]
    assert stored.created_at is not None
    assert stored.deleted_at is None


@pytest.mark.asyncio
async def test_app_record_version_server_default_applies_to_raw_inserts():
    """Rows written without the ORM default still start at version 1."""
    from app.models.app_record import AppRecord

    async with async_session_maker() as session:
        ids = await _seed_parents(session)
        rid = str(uuid.uuid4())
        await session.execute(sa.text(
            "INSERT INTO app_records (id, organization_id, report_id, artifact_id, collection, user_id, data) "
            "VALUES (:id, :org, :report, :artifact, 'selections', :user, :data)"
        ), {"id": rid, "org": ids["org"], "report": ids["report"], "artifact": ids["artifact"],
            "user": ids["user"], "data": '{"genre": "Jazz"}'})
        await session.commit()

    async with async_session_maker() as session:
        row = (await session.execute(sa.select(AppRecord).where(AppRecord.id == rid))).scalar_one()
    assert row.version == 1
    assert row.created_at is not None
