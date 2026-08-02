"""Per-user agent recency (``DataSourceListItemSchema.last_used_at``).

The agent pickers lead with the agents you actually work with, which only means
something if "used" is defined carefully: a fresh report attaches every agent
the user can see, so mere attachment would stamp the whole roster as "just used"
the moment anyone opens a blank report. This validates:

  - a report with no completions never counts, however many agents it attaches
  - a report with a turn stamps its agents with the report's last_activity_at
  - the newest qualifying conversation wins
  - recency is per-user: a teammate's conversation is not your recency

No external services needed — pure DB + service logic on SQLite.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_agent_last_used.py -v -s
"""
import uuid
import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.domain_connection import domain_connection
from app.models.report import Report
from app.models.report_data_source_association import report_data_source_association
from app.models.completion import Completion
from app.services.data_source_service import DataSourceService


def _run(coro):
    return asyncio.run(coro)


async def _seed_org(agent_names):
    """One org, two users and the given public/published agents."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Recency Org {suffix}")
        db.add(org)
        await db.flush()

        users = []
        for who in ("owner", "teammate"):
            u = User(
                name=who,
                email=f"{who}-{suffix}@example.com",
                hashed_password="x",
                is_active=True,
                is_superuser=False,
                is_verified=True,
            )
            db.add(u)
            users.append(u)
        await db.flush()

        ds_ids = {}
        for name in agent_names:
            conn = Connection(
                organization_id=org.id,
                name=f"{name} conn {suffix}",
                type="postgresql",
                config={"host": "localhost", "database": "demo"},
                auth_policy="system_only",
            )
            conn.encrypt_credentials({"user": "u", "password": "p"})
            db.add(conn)
            await db.flush()

            ds = DataSource(
                name=f"{name} {suffix}",
                organization_id=org.id,
                is_active=True,
                is_public=True,
                publish_status="published",
                owner_user_id=users[0].id,
            )
            db.add(ds)
            await db.flush()
            await db.execute(domain_connection.insert().values(
                data_source_id=ds.id, connection_id=conn.id,
            ))
            ds_ids[name] = str(ds.id)

        await db.commit()
        return str(org.id), str(users[0].id), str(users[1].id), ds_ids


async def _add_report(org_id, user_id, ds_ids, *, activity_at, with_completion):
    """A report attached to `ds_ids`, optionally carrying a real turn."""
    async with async_session_maker() as db:
        report = Report(
            title="untitled report",
            slug=uuid.uuid4().hex,
            user_id=user_id,
            organization_id=org_id,
            last_activity_at=activity_at,
        )
        db.add(report)
        await db.flush()
        for ds_id in ds_ids:
            await db.execute(report_data_source_association.insert().values(
                report_id=report.id, data_source_id=ds_id,
            ))
        if with_completion:
            db.add(Completion(
                report_id=report.id,
                user_id=user_id,
                prompt={"content": "hi"},
                completion={"content": "hello"},
                role="user",
                message_type="table",
            ))
        await db.commit()
        return str(report.id)


async def _last_used(org_id, user_id):
    """{data_source_id: last_used_at} as the picker sees it."""
    svc = DataSourceService()
    async with async_session_maker() as db:
        org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        items = await svc.get_active_data_sources(db, org, user)
        return {it.id: it.last_used_at for it in items}


def test_attached_but_never_used_agents_have_no_recency():
    org_id, owner_id, _, ds = _run(_seed_org(["Sales", "Support", "Finance"]))
    # A blank report attaches everything — exactly the case that must not count.
    _run(_add_report(org_id, owner_id, list(ds.values()),
                     activity_at=datetime.utcnow(), with_completion=False))

    used = _run(_last_used(org_id, owner_id))
    assert set(used) == set(ds.values())
    assert all(v is None for v in used.values()), used
    print("[blank] a report with no turn leaves every agent unused ✓")


def test_conversation_stamps_its_agents_and_newest_wins():
    org_id, owner_id, _, ds = _run(_seed_org(["Sales", "Support", "Finance"]))
    old = datetime.utcnow() - timedelta(days=3)
    recent = datetime.utcnow() - timedelta(hours=1)

    _run(_add_report(org_id, owner_id, [ds["Sales"]], activity_at=old, with_completion=True))
    _run(_add_report(org_id, owner_id, [ds["Support"]], activity_at=recent, with_completion=True))
    # A newer blank report on Sales must not overwrite its real recency.
    _run(_add_report(org_id, owner_id, [ds["Sales"]],
                     activity_at=datetime.utcnow(), with_completion=False))

    used = _run(_last_used(org_id, owner_id))
    assert used[ds["Finance"]] is None
    assert used[ds["Sales"]] is not None and used[ds["Support"]] is not None
    # Support was used an hour ago, Sales three days ago.
    assert used[ds["Support"]] > used[ds["Sales"]]
    assert abs((used[ds["Sales"]].replace(tzinfo=None) - old).total_seconds()) < 2
    print("[used] conversations stamp their agents, blank reports don't move them ✓")


def test_recency_is_per_user():
    org_id, owner_id, teammate_id, ds = _run(_seed_org(["Sales", "Support"]))
    _run(_add_report(org_id, teammate_id, [ds["Sales"]],
                     activity_at=datetime.utcnow(), with_completion=True))

    assert _run(_last_used(org_id, owner_id))[ds["Sales"]] is None
    assert _run(_last_used(org_id, teammate_id))[ds["Sales"]] is not None
    print("[scope] a teammate's conversation is not your recency ✓")
