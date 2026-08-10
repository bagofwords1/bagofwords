"""Delete an agent / connection with the full child set, foreign keys ENFORCED.

Every FK bug on these paths (#786, #787) shared a root cause and a reason it
reached production: the app never sets ``PRAGMA foreign_keys=ON``, so on SQLite
the DB silently tolerates dangling child rows and the leak only surfaces on
Postgres. Assertion-style tests catch the tables you already thought of; this
one makes the *database* do the checking, so a forgotten FK fails here instead.

Under ``--db=postgres`` / ``--db=external`` these run against real Postgres and
need no help. Under the default SQLite they force the pragma on, which is what
gives them teeth — without it both tests pass no matter how much is left behind.

The fixture deliberately over-attaches: tables, stats, metadata resources, a git
repo, memberships, instructions, prompts, a report, and both Webhook scopes
(standalone trigger and report-bound). Add to it rather than writing a new test
when a new child table starts hanging off a data source.
"""
import uuid
import pytest
import pytest_asyncio
from sqlalchemy import event, select

from app.dependencies import async_session_maker, engine
from app.models.organization import Organization
from app.models.user import User
from app.models.membership import Membership
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.report import Report
from app.models.webhook import Webhook


@pytest_asyncio.fixture
async def fk_enforced():
    """Force foreign_keys=ON for every new SQLite connection in this test."""
    if engine.dialect.name != "sqlite":
        yield  # Postgres enforces FKs natively; nothing to force.
        return

    def _on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    event.listen(engine.sync_engine, "connect", _on)
    await engine.dispose()  # drop pooled connections so new ones get the pragma
    try:
        yield
    finally:
        event.remove(engine.sync_engine, "connect", _on)
        await engine.dispose()


async def _seed_agent_with_trigger():
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"FK Org {suffix}")
        db.add(org); await db.flush()

        admin = User(name=f"Admin {suffix}", email=f"admin-{suffix}@example.com",
                     hashed_password="x", is_active=True, is_verified=True)
        db.add(admin); await db.flush()
        db.add(Membership(user_id=admin.id, organization_id=org.id, role="admin"))

        conn = Connection(name=f"wh-{suffix}", type="postgresql", config="{}",
                          organization_id=org.id, is_active=True)
        db.add(conn); await db.flush()

        ds = DataSource(name=f"Agent {suffix}", organization_id=org.id, is_active=True)
        db.add(ds); await db.flush()
        from sqlalchemy import insert as _ins
        from app.models.domain_connection import domain_connection
        await db.execute(_ins(domain_connection).values(
            data_source_id=ds.id, connection_id=conn.id))

        report = Report(title="R", slug=f"r-{suffix}", user_id=admin.id,
                        organization_id=org.id)
        db.add(report); await db.flush()

        wh = Webhook(report_id=None, organization_id=str(org.id), user_id=str(admin.id),
                     name=f"Trigger {suffix}", token=Webhook.generate_token())
        wh.set_secret(Webhook.generate_secret())
        db.add(wh); await db.flush()
        from app.models.webhook_data_source_association import webhook_data_source_association
        await db.execute(_ins(webhook_data_source_association).values(
            webhook_id=wh.id, data_source_id=ds.id))

        # A report-bound webhook too (the other Webhook scope).
        wh2 = Webhook(report_id=str(report.id), organization_id=str(org.id),
                      user_id=str(admin.id), name=f"Bound {suffix}",
                      token=Webhook.generate_token())
        wh2.set_secret(Webhook.generate_secret())
        db.add(wh2)

        # Everything else a real agent accumulates.
        from app.models.datasource_table import DataSourceTable
        from app.models.table_stats import TableStats
        from app.models.metadata_resource import MetadataResource
        from app.models.git_repository import GitRepository
        from app.models.data_source_membership import DataSourceMembership
        from app.models.instruction import Instruction
        from app.models.prompt import Prompt

        tbl = DataSourceTable(name="orders", datasource_id=ds.id, is_active=True,
                              columns=[{"name": "id", "dtype": "int"}], pks=[], fks=[])
        db.add(tbl); await db.flush()
        db.add(TableStats(org_id=org.id, data_source_id=ds.id, table_fqn="orders",
                          datasource_table_id=tbl.id))
        db.add(MetadataResource(name="dbt_model", resource_type="model",
                                data_source_id=ds.id, is_active=True))
        db.add(GitRepository(provider="github", repo_url="https://example.com/x.git",
                             data_source_id=ds.id, organization_id=org.id, is_active=True,
                             user_id=admin.id))
        db.add(DataSourceMembership(data_source_id=ds.id, principal_type="user",
                                    principal_id=str(admin.id)))

        instr = Instruction(text="always filter by org", user_id=admin.id,
                            organization_id=org.id, status="published")
        db.add(instr)
        prompt = Prompt(title="Weekly", text="summarize", user_id=admin.id,
                        organization_id=org.id)
        db.add(prompt)
        await db.flush()

        # Link the M2Ms by raw insert — assigning the ORM collections here would
        # lazy-load under async and raise MissingGreenlet.
        from sqlalchemy import insert
        from app.models.instruction import instruction_data_source_association
        from app.models.prompt_data_source_association import prompt_data_source_association
        from app.models.report_data_source_association import report_data_source_association
        await db.execute(insert(instruction_data_source_association).values(
            instruction_id=instr.id, data_source_id=ds.id))
        await db.execute(insert(prompt_data_source_association).values(
            prompt_id=prompt.id, data_source_id=ds.id))
        await db.execute(insert(report_data_source_association).values(
            report_id=report.id, data_source_id=ds.id))

        await db.commit()
        return {"org": org.id, "admin": admin.id, "ds": str(ds.id),
                "conn": str(conn.id), "wh": str(wh.id), "report": str(report.id)}


@pytest.mark.asyncio
async def test_delete_data_source_under_enforced_fks(fk_enforced):
    from app.services.data_source_service import DataSourceService

    ids = await _seed_agent_with_trigger()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        admin = await db.get(User, ids["admin"])
        await DataSourceService().delete_data_source(db, ids["ds"], org, admin)
        assert (await db.execute(
            select(DataSource).where(DataSource.id == ids["ds"])
        )).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_delete_connection_under_enforced_fks(fk_enforced):
    from app.services.connection_service import ConnectionService

    ids = await _seed_agent_with_trigger()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        admin = await db.get(User, ids["admin"])
        await ConnectionService().delete_connection(
            db=db, connection_id=ids["conn"], organization=org, current_user=admin)
        assert (await db.execute(
            select(Connection).where(Connection.id == ids["conn"])
        )).scalar_one_or_none() is None
