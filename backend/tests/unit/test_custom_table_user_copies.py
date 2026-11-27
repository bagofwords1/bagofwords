"""Per-user copies of a custom table, with real sessions, extraction and files.

On a connection where every person queries with their own credentials, a
custom table is materialized once per person, with that person's credentials.
What these hold the service to:

  * a person is only ever served rows read with their own login;
  * a copy stops being served the moment what it was built from changes
    (the query, the login) or anyone asks for it to go;
  * a build that finishes after that does not bring the data back;
  * files are deleted when nobody should hold them any more.

Runs on the harness database (a fresh, migrated schema per test on whichever
``--db`` CI selects) with real SQLite sources — each "login" is a different
database file, which is exactly what two logins under row-level security look
like to an extraction: same query, different rows. Only boundaries are
replaced: the artifact root (a tmp dir) and, where a test needs to hold a
build mid-flight, the connector's connect call.
"""
import asyncio
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.data_sources.clients.sqlite_client import SqliteClient
from app.data_sources.fast import artifacts
from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import KIND_BOW, ConnectionTable
from app.models.custom_table_user_copy import CustomTableUserCopy
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.organization_settings import OrganizationSettings
from app.models.user import User
from app.services.custom_query_service import CustomQueryService
from app.services.custom_table_user_copy_service import CustomTableUserCopyService

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

QUERY = "SELECT * FROM orders"


@dataclass
class Person:
    id: str
    source: Path                      # the rows this login can see
    builds: list = field(default_factory=list)

    @property
    def credentials(self) -> dict:
        return {"database": str(self.source)}


@dataclass
class Sandbox:
    root: Path
    org_id: str
    connection_id: str
    table_id: str
    service: CustomTableUserCopyService
    alice: Person
    bob: Person
    tmp: Path

    def files(self) -> set:
        return set(self.root.rglob("*.db"))

    async def ask(self, person: Person, *, wait: bool = True, credentials: dict = None, db=None) -> dict:
        """What `construct_clients` does for a per-user connection: the copies
        this person may be served, built with their own login when missing."""
        creds = credentials or person.credentials

        async def client_for(_table):
            person.builds.append(creds["database"])
            return SqliteClient(database=creds["database"])

        async def run(session):
            connection = await session.get(Connection, self.connection_id)
            table = await session.get(ConnectionTable, self.table_id)
            user = await session.get(User, person.id)
            return await self.service.serve(
                session, connection=connection, tables=[table], user=user,
                credentials=creds, client_factory=client_for, wait=wait)

        if db is not None:
            return await run(db)
        async with async_session_maker() as session:
            return await run(session)

    async def rows(self, person: Person, **kw) -> list:
        """The `value` column of the copy this person is served, or None."""
        refs = await self.ask(person, **kw)
        return self.read(refs)

    def read(self, refs: dict):
        ref = refs.get(self.table_id)
        if ref is None:
            return None
        con = artifacts.connect_encrypted(ref.artifact_path, ref.artifact_key, read_only=True)
        try:
            return sorted(r[0] for r in con.execute('SELECT value FROM orders_copy').fetchall())
        finally:
            con.close()

    async def copy(self, person: Person) -> CustomTableUserCopy:
        async with async_session_maker() as db:
            return (await db.execute(
                select(CustomTableUserCopy).where(
                    CustomTableUserCopy.connection_table_id == self.table_id,
                    CustomTableUserCopy.user_id == person.id)
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()

    async def settle(self) -> None:
        await self.service.wait_for_builds(30)

    async def set_copy(self, person: Person, **values) -> None:
        # No API moves a copy's clock; ageing one is the only way to reach
        # the expiry and idle rules without sleeping for days.
        async with async_session_maker() as db:
            await db.execute(update(CustomTableUserCopy).where(
                CustomTableUserCopy.connection_table_id == self.table_id,
                CustomTableUserCopy.user_id == person.id).values(**values))
            await db.commit()

    async def set_table(self, **values) -> None:
        async with async_session_maker() as db:
            await db.execute(update(ConnectionTable).where(
                ConnectionTable.id == self.table_id).values(**values))
            await db.commit()

    async def configure(self, **settings) -> None:
        async with async_session_maker() as db:
            row = (await db.execute(select(OrganizationSettings).where(
                OrganizationSettings.organization_id == self.org_id))).scalars().first()
            config = {k: {"value": v} for k, v in settings.items()}
            if row is None:
                db.add(OrganizationSettings(organization_id=self.org_id, config=config))
            else:
                row.config = {**(row.config or {}), **config}
            await db.commit()


def _source(path: Path, values: list) -> Path:
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE orders (value INTEGER)")
        db.executemany("INSERT INTO orders VALUES (?)", [(v,) for v in values])
    return path


@pytest_asyncio.fixture
async def sandbox(tmp_path):
    root = tmp_path / "fast"
    with patch.object(artifacts, "_ARTIFACT_ROOT", root):
        async with async_session_maker() as db:
            # The API cannot give two members different rows from one SQLite
            # file, nor age a copy; seed the minimal real model state.
            org = Organization(name=f"Copies {uuid.uuid4().hex[:8]}")
            db.add(org)
            await db.flush()
            people = []
            for name, values in (("alice", [1, 2, 3]), ("bob", [10, 20])):
                user = User(name=name, email=f"{name}-{uuid.uuid4().hex[:8]}@example.test",
                            hashed_password="x", is_active=True)
                db.add(user)
                await db.flush()
                db.add(Membership(user_id=user.id, organization_id=org.id, role="member",
                                  email=user.email))
                people.append(Person(str(user.id), _source(tmp_path / f"{name}.sqlite", values)))
            connection = Connection(name="Source", type="sqlite", config={},
                                    organization_id=org.id, auth_policy="user_required",
                                    allowed_user_auth_modes=["userpass"])
            db.add(connection)
            await db.flush()
            table = ConnectionTable(connection_id=connection.id, name="orders_copy", kind=KIND_BOW,
                                    definition_sql=QUERY, columns=[], pks=[], fks=[],
                                    refresh_schedule_mode="interval", refresh_interval_minutes=60)
            db.add(table)
            await db.commit()
            ids = (str(org.id), str(connection.id), str(table.id))
        box = Sandbox(root, *ids, CustomTableUserCopyService(), people[0], people[1], tmp_path)
        yield box
        await box.settle()


@contextmanager
def held_source():
    """Hold every extraction at the connector I/O boundary until released."""
    entered, release = threading.Event(), threading.Event()
    original = SqliteClient.extraction_connect

    @contextmanager
    def slow_source(client):
        entered.set()
        if not release.wait(20):
            raise TimeoutError("sandbox source was never released")
        with original(client) as connection:
            yield connection

    with patch.object(SqliteClient, "extraction_connect", slow_source):
        try:
            yield entered, release
        finally:
            release.set()


async def _until(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 20), "the build never reached the source"


# -- whose rows ----------------------------------------------------------------

async def test_each_person_is_served_only_rows_read_with_their_own_login(sandbox):
    assert await sandbox.rows(sandbox.alice) == [1, 2, 3]
    assert await sandbox.rows(sandbox.bob) == [10, 20]
    # Two people, two files; neither is the other's.
    alice, bob = await sandbox.copy(sandbox.alice), await sandbox.copy(sandbox.bob)
    assert alice.artifact_path != bob.artifact_path
    assert {Path(alice.artifact_path), Path(bob.artifact_path)} == sandbox.files()
    assert (alice.row_count, bob.row_count) == (3, 2)


async def test_a_good_copy_is_reused_instead_of_asking_the_source_again(sandbox):
    await sandbox.rows(sandbox.alice)
    first = (await sandbox.copy(sandbox.alice)).artifact_path
    for _ in range(3):
        assert await sandbox.rows(sandbox.alice) == [1, 2, 3]
    assert len(sandbox.alice.builds) == 1
    assert (await sandbox.copy(sandbox.alice)).artifact_path == first


async def test_nobody_signed_in_is_served_nothing(sandbox):
    async with async_session_maker() as db:
        connection = await db.get(Connection, sandbox.connection_id)
        table = await db.get(ConnectionTable, sandbox.table_id)

        async def never(_table):
            raise AssertionError("no client may be built without a user")

        assert await sandbox.service.serve(
            db, connection=connection, tables=[table], user=None,
            credentials={}, client_factory=never) == {}
    assert sandbox.files() == set()


async def test_the_cached_session_holds_a_persons_copy_and_never_a_shared_one(sandbox, tmp_path):
    """A shared artifact left on the row by a change of auth policy holds rows
    read with the system login. On a per-user connection it must not be what
    the DuckDB session attaches."""
    refs = await sandbox.ask(sandbox.bob)
    async with async_session_maker() as db:
        table = await db.get(ConnectionTable, sandbox.table_id)
        # Make the row look like it still carries a shared artifact.
        shared = await sandbox.ask(sandbox.alice)
        table.artifact_path = shared[sandbox.table_id].artifact_path
        table.artifact_key_enc = artifacts.encrypt_key(shared[sandbox.table_id].artifact_key)

        client = CustomQueryService.build_fast_client([table], "Source", user_copies_by_table=refs)
        assert sorted(client.execute_query("SELECT value FROM orders_copy")["value"]) == [10, 20]
        # A person with no copy gets no cached relation at all — not the shared one.
        assert CustomQueryService.build_fast_client([table], "Source", user_copies_by_table={}) is None


async def test_a_persons_cached_session_cannot_reach_another_persons_copy(sandbox):
    """The model writes arbitrary SQL against the cached session. Whatever it
    writes, bob's file is not something alice's session can open: it is not
    attached, attaching is switched off, and its key is not alice's."""
    import duckdb

    alice = await sandbox.ask(sandbox.alice)
    bob = await sandbox.ask(sandbox.bob)
    bob_file = bob[sandbox.table_id].artifact_path
    async with async_session_maker() as db:
        table = await db.get(ConnectionTable, sandbox.table_id)
        client = CustomQueryService.build_fast_client([table], "Source", user_copies_by_table=alice)

    assert sorted(client.execute_query("SELECT value FROM orders_copy")["value"]) == [1, 2, 3]
    hostile = [
        f"ATTACH '{bob_file}' AS other (READ_ONLY)",
        f"ATTACH '{bob_file}' AS other (ENCRYPTION_KEY '{alice[sandbox.table_id].artifact_key}', READ_ONLY)",
        f"SELECT * FROM '{bob_file}'",
        f"SELECT * FROM read_parquet('{bob_file}')",
        f"SELECT * FROM read_csv_auto('{bob_file}')",
        "SET enable_external_access = true",
        f"COPY (SELECT 1) TO '{sandbox.tmp}/out.csv'",
    ]
    for sql in hostile:
        with pytest.raises((duckdb.Error, ValueError, PermissionError, RuntimeError)):
            client.execute_query(sql)
    # Nothing the session can list holds bob's rows.
    every_value = client.execute_query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema NOT IN ('information_schema')")
    assert set(every_value["table_name"]) <= {"orders_copy"}
    assert sorted(client.execute_query("SELECT value FROM orders_copy")["value"]) == [1, 2, 3]


# -- what a copy is tied to ----------------------------------------------------

async def test_editing_the_query_stops_the_old_copy_and_builds_the_new_one(sandbox):
    assert await sandbox.rows(sandbox.alice) == [1, 2, 3]
    old = Path((await sandbox.copy(sandbox.alice)).artifact_path)
    await sandbox.set_table(definition_sql="SELECT * FROM orders WHERE value > 1")
    assert await sandbox.rows(sandbox.alice) == [2, 3]
    assert not old.exists(), "the replaced copy's file must not linger"


async def test_a_person_who_changes_login_is_not_served_rows_read_as_the_previous_one(sandbox):
    assert await sandbox.rows(sandbox.alice) == [1, 2, 3]
    # Alice now signs in to the source as the login that sees bob's rows.
    assert await sandbox.rows(sandbox.alice, credentials=sandbox.bob.credentials) == [10, 20]
    # ... and back: still never the other login's rows.
    assert await sandbox.rows(sandbox.alice) == [1, 2, 3]


async def test_a_changed_login_is_visible_to_readers_that_cannot_check_credentials(sandbox):
    """The schema context asks `servable()` without resolving credentials. A
    copy built as another login must not be advertised there either."""
    await sandbox.rows(sandbox.alice)
    with held_source() as (entered, release):
        # The new login's build is held, so only the stale copy exists.
        refs = await sandbox.ask(sandbox.alice, credentials=sandbox.bob.credentials, wait=False)
        assert refs == {}
        async with async_session_maker() as db:
            table = await db.get(ConnectionTable, sandbox.table_id)
            assert await sandbox.service.servable(db, sandbox.alice.id, [table]) == {}
        release.set()
    await sandbox.settle()


async def test_a_copy_past_the_maximum_age_is_rebuilt_before_it_is_served_again(sandbox):
    await sandbox.rows(sandbox.alice)
    _source_path = sandbox.alice.source
    with closing(sqlite3.connect(_source_path)) as db, db:
        db.execute("INSERT INTO orders VALUES (4)")
    await sandbox.set_copy(sandbox.alice, refreshed_at=datetime.utcnow() - timedelta(hours=25))
    assert await sandbox.rows(sandbox.alice) == [1, 2, 3, 4]


async def test_a_copy_past_the_tables_schedule_is_served_while_it_is_rebuilt(sandbox):
    await sandbox.rows(sandbox.alice)
    with closing(sqlite3.connect(sandbox.alice.source)) as db, db:
        db.execute("INSERT INTO orders VALUES (4)")
    # Older than the table's 60-minute interval, well inside the maximum age.
    await sandbox.set_copy(sandbox.alice, refreshed_at=datetime.utcnow() - timedelta(hours=2))
    with held_source() as (entered, release):
        assert await sandbox.rows(sandbox.alice) == [1, 2, 3], "the stale copy is still served"
        await _until(entered)
        release.set()
    await sandbox.settle()
    assert await sandbox.rows(sandbox.alice) == [1, 2, 3, 4]


async def test_a_chat_already_reading_a_copy_keeps_it_while_the_rebuild_replaces_it(sandbox):
    """A question is handed a copy's file; a rebuild started by that very
    question finishes while the answer is still being worked out. The file
    must still open — and must not be kept forever either."""
    await sandbox.rows(sandbox.alice)
    await sandbox.set_copy(sandbox.alice, refreshed_at=datetime.utcnow() - timedelta(hours=2))
    held = await sandbox.ask(sandbox.alice)          # served the stale copy; rebuild started
    await sandbox.settle()                           # ... and has now replaced it
    replacement = await sandbox.ask(sandbox.alice)
    assert held[sandbox.table_id].artifact_path != replacement[sandbox.table_id].artifact_path
    assert sandbox.read(held) == [1, 2, 3], "the file a running chat holds was deleted under it"

    # With no grace period the replaced file goes as soon as the rebuild lands.
    sandbox.service.retire_replaced_after = 0
    await sandbox.set_copy(sandbox.alice, refreshed_at=datetime.utcnow() - timedelta(hours=2))
    stale = await sandbox.ask(sandbox.alice)
    await sandbox.settle()
    assert not Path(stale[sandbox.table_id].artifact_path).exists()


async def test_a_sign_in_keeps_serving_the_copy_and_rebuilds_it_behind(sandbox):
    await sandbox.rows(sandbox.alice)
    with closing(sqlite3.connect(sandbox.alice.source)) as db, db:
        db.execute("INSERT INTO orders VALUES (4)")
    async with async_session_maker() as db:
        assert await sandbox.service.request_refresh(db, user_id=sandbox.alice.id) == 1
    assert await sandbox.rows(sandbox.alice) == [1, 2, 3]
    await sandbox.settle()
    assert await sandbox.rows(sandbox.alice) == [1, 2, 3, 4]
    # Bob did not sign in; nothing of his was touched.
    assert sandbox.bob.builds == []


async def test_invalidating_stops_a_copy_until_it_is_rebuilt(sandbox):
    await sandbox.rows(sandbox.alice)
    await sandbox.rows(sandbox.bob)
    async with async_session_maker() as db:
        table = await db.get(ConnectionTable, sandbox.table_id)
        assert await sandbox.service.invalidate(db, table_id=sandbox.table_id) == 2
        assert await sandbox.service.servable(db, sandbox.alice.id, [table]) == {}
    assert await sandbox.rows(sandbox.alice) == [1, 2, 3]
    assert len(sandbox.alice.builds) == 2


# -- removal -------------------------------------------------------------------

async def test_purging_deletes_the_files_and_a_person_gets_theirs_back_when_they_ask(sandbox):
    await sandbox.rows(sandbox.alice)
    await sandbox.rows(sandbox.bob)
    async with async_session_maker() as db:
        done = await sandbox.service.purge(db, table_id=sandbox.table_id, user_id=sandbox.alice.id)
    assert done["copies"] == 1 and done["reclaimed_bytes"] > 0
    # Only alice's file went.
    assert sandbox.files() == {Path((await sandbox.copy(sandbox.bob)).artifact_path)}
    assert (await sandbox.copy(sandbox.alice)).status == "evicted"
    assert await sandbox.rows(sandbox.alice) == [1, 2, 3]


async def test_a_purge_must_name_a_scope(sandbox):
    async with async_session_maker() as db:
        with pytest.raises(ValueError):
            await sandbox.service.purge(db)


async def test_a_build_that_finishes_after_a_purge_does_not_bring_the_data_back(sandbox):
    with held_source() as (entered, release):
        assert await sandbox.ask(sandbox.alice, wait=False) == {}
        await _until(entered)
        async with async_session_maker() as db:
            await sandbox.service.purge(db, user_id=sandbox.alice.id, connection_id=sandbox.connection_id)
        release.set()
    await sandbox.settle()
    assert sandbox.files() == set()
    assert (await sandbox.copy(sandbox.alice)).artifact_path is None


async def test_a_build_that_finishes_after_its_table_was_deleted_leaves_nothing(sandbox):
    with held_source() as (entered, release):
        await sandbox.ask(sandbox.alice, wait=False)
        await _until(entered)
        async with async_session_maker() as db:
            connection = await db.get(Connection, sandbox.connection_id)
            table = await db.get(ConnectionTable, sandbox.table_id)
            await CustomQueryService().delete(db, connection, table)
        release.set()
    await sandbox.settle()
    assert sandbox.files() == set()
    assert await sandbox.copy(sandbox.alice) is None


async def test_deleting_the_table_deletes_everyones_copy(sandbox):
    await sandbox.rows(sandbox.alice)
    await sandbox.rows(sandbox.bob)
    async with async_session_maker() as db:
        connection = await db.get(Connection, sandbox.connection_id)
        table = await db.get(ConnectionTable, sandbox.table_id)
        await CustomQueryService().delete(db, connection, table)
    assert sandbox.files() == set()
    assert await sandbox.copy(sandbox.alice) is None and await sandbox.copy(sandbox.bob) is None


# -- building ------------------------------------------------------------------

async def test_two_questions_at_once_share_one_build(sandbox):
    with held_source() as (entered, release):
        first = asyncio.ensure_future(sandbox.rows(sandbox.alice))
        await _until(entered)
        second = asyncio.ensure_future(sandbox.rows(sandbox.alice))
        await asyncio.sleep(0)
        release.set()
        assert await first == [1, 2, 3] and await second == [1, 2, 3]
    assert len(sandbox.alice.builds) == 1
    assert len(sandbox.files()) == 1


async def test_a_slow_build_does_not_hold_the_question_and_finishes_behind_it(sandbox):
    await sandbox.configure(custom_table_user_copy_wait_seconds=1)
    with held_source() as (entered, release):
        started = time.monotonic()
        assert await sandbox.ask(sandbox.alice) == {}, "no copy yet: answered from the live source"
        assert time.monotonic() - started < 10
        await _until(entered)
        assert (await sandbox.copy(sandbox.alice)).status == "building"
        release.set()
    await sandbox.settle()
    assert await sandbox.rows(sandbox.alice) == [1, 2, 3]
    assert len(sandbox.alice.builds) == 1


async def test_a_failed_build_is_recorded_and_not_retried_on_every_question(sandbox):
    await sandbox.set_table(definition_sql="SELECT * FROM no_such_table")
    assert await sandbox.rows(sandbox.alice) is None
    copy = await sandbox.copy(sandbox.alice)
    assert copy.status == "error" and "no_such_table" in (copy.error or "")
    assert await sandbox.rows(sandbox.alice) is None
    assert len(sandbox.alice.builds) == 1, "the same failure must not be re-run for every question"
    assert sandbox.files() == set()
    # Fixing the query is a new definition: it builds straight away.
    await sandbox.set_table(definition_sql=QUERY)
    assert await sandbox.rows(sandbox.alice) == [1, 2, 3]


async def test_a_person_without_credentials_gets_no_copy_and_no_error(sandbox):
    async with async_session_maker() as db:
        connection = await db.get(Connection, sandbox.connection_id)
        table = await db.get(ConnectionTable, sandbox.table_id)
        user = await db.get(User, sandbox.alice.id)

        async def no_login(_table):
            raise PermissionError("User credentials required for this connection")

        assert await sandbox.service.serve(
            db, connection=connection, tables=[table], user=user,
            credentials={}, client_factory=no_login) == {}
    assert await sandbox.copy(sandbox.alice) is None


async def test_a_query_over_the_row_limit_is_refused_before_it_fills_the_disk(sandbox):
    with closing(sqlite3.connect(sandbox.alice.source)) as db, db:
        db.executemany("INSERT INTO orders VALUES (?)", [(i,) for i in range(2000)])
    await sandbox.configure(custom_table_max_rows=1000)
    assert await sandbox.rows(sandbox.alice) is None
    copy = await sandbox.copy(sandbox.alice)
    assert copy.status == "error" and copy.error
    assert sandbox.files() == set()


# -- cleanup -------------------------------------------------------------------

async def test_cleanup_removes_what_nobody_should_hold_and_keeps_the_rest(sandbox):
    await sandbox.rows(sandbox.alice)
    await sandbox.rows(sandbox.bob)
    kept = Path((await sandbox.copy(sandbox.alice)).artifact_path)
    # Bob has not used his for longer than the idle period.
    long_ago = datetime.utcnow() - timedelta(days=30)
    await sandbox.set_copy(sandbox.bob, last_used_at=long_ago)
    done = await sandbox.service.cleanup()
    assert done["copies"] == 1
    assert sandbox.files() == {kept}
    assert await sandbox.rows(sandbox.alice) == [1, 2, 3]
    assert len(sandbox.alice.builds) == 1, "a copy in use is left alone"


async def test_cleanup_removes_the_copy_of_someone_who_left_the_organization(sandbox):
    await sandbox.rows(sandbox.alice)
    await sandbox.rows(sandbox.bob)
    async with async_session_maker() as db:
        membership = (await db.execute(select(Membership).where(
            Membership.user_id == sandbox.bob.id))).scalar_one()
        await db.delete(membership)
        await db.commit()
    await sandbox.service.cleanup()
    assert sandbox.files() == {Path((await sandbox.copy(sandbox.alice)).artifact_path)}
    assert await sandbox.copy(sandbox.bob) is None


async def test_cleanup_removes_every_copy_once_the_connection_uses_shared_credentials(sandbox):
    await sandbox.rows(sandbox.alice)
    async with async_session_maker() as db:
        connection = await db.get(Connection, sandbox.connection_id)
        connection.auth_policy = "system_only"
        await db.commit()
    await sandbox.service.cleanup()
    assert sandbox.files() == set()


async def test_the_storage_limit_evicts_the_least_recently_used_copy_first(sandbox):
    await sandbox.rows(sandbox.alice)
    await sandbox.rows(sandbox.bob)
    await sandbox.set_copy(sandbox.alice, last_used_at=datetime.utcnow() - timedelta(hours=3))
    bob_file = Path((await sandbox.copy(sandbox.bob)).artifact_path)
    one_copy = bob_file.stat().st_size
    done = await sandbox.service.enforce_quota(sandbox.org_id, max_total_bytes=one_copy)
    assert done["copies"] == 1
    assert sandbox.files() == {bob_file}


async def test_the_orphan_sweep_never_deletes_a_file_a_copy_points_at(sandbox):
    import os

    from app.services.fast_artifact_maintenance import purge_orphaned_fast_artifacts

    await sandbox.rows(sandbox.alice)
    served = Path((await sandbox.copy(sandbox.alice)).artifact_path)
    debris = served.parent / f"{uuid.uuid4().hex}.db"
    debris.write_bytes(b"left by a crash")
    old = time.time() - 2 * 86400
    for path in (served, debris):
        os.utime(path, (old, old))
    await purge_orphaned_fast_artifacts()
    assert served.exists() and not debris.exists()
    assert await sandbox.rows(sandbox.alice) == [1, 2, 3]


# -- no clock ------------------------------------------------------------------

async def test_no_refresh_is_scheduled_for_a_table_cached_per_person(sandbox):
    """Nobody is signed in at 3am: a tick has no credentials to use. A shared
    table gets a job; a per-person one must not, and a tick left over from a
    change of auth policy removes itself."""
    from app.core.scheduler import scheduler
    from app.services.custom_query_service import job_id_for

    service = CustomQueryService()
    async with async_session_maker() as db:
        table = await db.get(ConnectionTable, sandbox.table_id)
        job = job_id_for(table.id)
        try:
            service._schedule(sandbox.connection_id, table, per_user=False)
            assert scheduler.get_job(job) is not None
            service._schedule(sandbox.connection_id, table, per_user=True)
            assert scheduler.get_job(job) is None
            # A job from before the connection became per-user fires once more.
            service._schedule(sandbox.connection_id, table, per_user=False)
            await service.scheduled_refresh(sandbox.connection_id, str(table.id))
            assert scheduler.get_job(job) is None
        finally:
            service._unschedule(table.id)
    assert sandbox.files() == set(), "a tick must not build anything"


# -- what the admin sees --------------------------------------------------------

async def test_the_admins_listing_names_each_person_and_what_they_hold(sandbox):
    await sandbox.rows(sandbox.alice)
    await sandbox.rows(sandbox.bob)
    async with async_session_maker() as db:
        listing = await sandbox.service.list_for_table(db, sandbox.table_id)
        summary = await sandbox.service.summary_for_tables(db, [sandbox.table_id])
    by_user = {row["user_id"]: row for row in listing}
    assert set(by_user) == {sandbox.alice.id, sandbox.bob.id}
    assert by_user[sandbox.alice.id]["row_count"] == 3 and by_user[sandbox.bob.id]["row_count"] == 2
    assert all(row["status"] == "ok" and row["user_email"] for row in listing)
    assert summary[sandbox.table_id]["copies"] == 2
    assert summary[sandbox.table_id]["bytes"] == sum(row["artifact_bytes"] for row in listing)


async def test_the_listing_does_not_call_a_copy_ready_when_it_is_about_to_be_rebuilt(sandbox):
    await sandbox.rows(sandbox.alice)
    await sandbox.rows(sandbox.bob)
    async with async_session_maker() as db:
        await sandbox.service.invalidate(db, user_id=sandbox.alice.id, table_id=sandbox.table_id)
        listing = {row["user_id"]: row["status"]
                   for row in await sandbox.service.list_for_table(db, sandbox.table_id)}
    assert listing[sandbox.alice.id] != "ok" and listing[sandbox.bob.id] == "ok"
    await sandbox.rows(sandbox.alice)
    async with async_session_maker() as db:
        listing = {row["user_id"]: row["status"]
                   for row in await sandbox.service.list_for_table(db, sandbox.table_id)}
    assert listing[sandbox.alice.id] == "ok"
