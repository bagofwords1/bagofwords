"""Custom-table limits: how settings become limits, and the refresh paths
that have no HTTP caller.

The API-reachable paths (create, manual refresh, increment, rebuild, the
settings endpoint and its permission) are in
tests/e2e/rbac/test_custom_table_limits_api.py. Here: the clamp that protects
against a stored value outside the range, and the scheduled refresh, which
runs with no user and no request and so cannot get limits from either.
"""
import sqlite3
import uuid
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio

from app.data_sources.fast import artifacts
from app.data_sources.fast import limits as fast_limits
from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import KIND_BOW, ConnectionTable
from app.models.organization import Organization
from app.models.organization_settings import OrganizationSettings
from app.services.custom_query_service import CustomQueryService

GIB = fast_limits.GIB


def _settings(**config):
    return SimpleNamespace(config=config)


def test_an_organization_that_sets_nothing_gets_the_shipped_limits():
    for unset in (None, _settings(), SimpleNamespace(config=None)):
        limits = fast_limits.resolve(unset)
        assert (limits.max_rows, limits.max_bytes, limits.max_scan_bytes, limits.max_requests) == (
            5_000_000, 2 * GIB, 100 * GIB, 512)


@pytest.mark.parametrize("key,attr,unit", [
    ("custom_table_max_rows", "max_rows", 1),
    ("custom_table_max_gb", "max_bytes", GIB),
    ("custom_table_max_scan_gb", "max_scan_bytes", GIB),
    ("custom_table_max_requests", "max_requests", 1),
])
def test_a_stored_value_is_used_inside_its_range_and_clamped_outside_it(key, attr, unit):
    bound = fast_limits.SETTINGS[key]
    inside = (bound.low + bound.high) // 2

    def stored(value, shape):
        return getattr(fast_limits.resolve(_settings(**{key: shape(value)})), attr)

    for shape in (lambda v: {"value": v, "name": "x"}, lambda v: v):   # FeatureConfig dict, bare value
        assert stored(inside, shape) == inside * unit
        # A value no write would accept (set before a range changed, or
        # written to the database directly) still cannot lift the limit.
        assert stored(bound.high * 1000, shape) == bound.high * unit
        assert stored(0, shape) == bound.low * unit
        assert stored(-1, shape) == bound.low * unit
        for junk in (None, "unlimited", True, [1]):
            assert stored(junk, shape) == bound.default * unit


@pytest.mark.parametrize("key", sorted(fast_limits.SETTINGS))
def test_validation_accepts_the_range_and_nothing_else(key):
    bound = fast_limits.SETTINGS[key]
    for good in (bound.low, bound.default, bound.high):
        assert fast_limits.validate_setting(key, good) == good
    for bad in (bound.low - 1, bound.high + 1, True, 2.0, "5", None):
        with pytest.raises(ValueError):
            fast_limits.validate_setting(key, bad)


# -- the scheduled refresh ---------------------------------------------------

ROWS = fast_limits.SETTINGS["custom_table_max_rows"].low + 250


@pytest_asyncio.fixture
async def scheduled(tmp_path, db_backend):
    source = tmp_path / "source.sqlite"
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
        db.executemany("INSERT INTO orders VALUES (?)", [(i,) for i in range(ROWS)])
    with patch.object(artifacts, "_ARTIFACT_ROOT", tmp_path / "fast"), \
            patch("app.core.scheduler.claim_scheduled_run", return_value=True):
        async with async_session_maker() as db:
            # Seeded directly: the scheduler's entry point takes ids, not a
            # request, and the API always loads a table while creating it.
            org = Organization(name=f"Limits {uuid.uuid4().hex[:8]}")
            db.add(org)
            await db.flush()
            connection = Connection(name="Source", type="sqlite", config={"database": str(source)},
                                    organization_id=org.id)
            db.add(connection)
            await db.flush()
            table = ConnectionTable(connection_id=connection.id, name="orders_copy", kind=KIND_BOW,
                                    definition_sql="SELECT * FROM orders", columns=[], pks=[], fks=[])
            db.add(table)
            await db.commit()
            ids = SimpleNamespace(org=org.id, connection=connection.id, table=table.id)

        async def set_row_limit(value):
            async with async_session_maker() as db:
                db.add(OrganizationSettings(organization_id=ids.org,
                                            config={"custom_table_max_rows": {"value": value}}))
                await db.commit()

        async def run():
            await CustomQueryService().scheduled_refresh(ids.connection, ids.table)
            async with async_session_maker() as db:
                return await db.get(ConnectionTable, ids.table)

        yield SimpleNamespace(set_row_limit=set_row_limit, run=run)


@pytest.mark.db
@pytest.mark.asyncio
async def test_a_scheduled_refresh_is_held_to_the_organizations_row_limit(scheduled):
    await scheduled.set_row_limit(ROWS - 250)
    table = await scheduled.run()
    assert table.last_refresh_status == "error" and not table.artifact_path
    assert f"{ROWS - 250:,}" in (table.last_refresh_error or "")


@pytest.mark.db
@pytest.mark.asyncio
async def test_a_scheduled_refresh_loads_what_the_organizations_limit_allows(scheduled):
    await scheduled.set_row_limit(ROWS)
    table = await scheduled.run()
    assert table.last_refresh_status == "ok" and table.no_rows == ROWS
