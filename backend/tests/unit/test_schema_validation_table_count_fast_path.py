"""Connection-test schema validation prefers a catalog count over introspection.

For tabular sources the pre-save test only uses len(get_schemas()). On Oracle
10g that call reads every column, comment and foreign key — minutes of
dictionary scans whose output the test discards. A client that exposes
count_tables() is counted from its catalog instead; clients without it keep
the full path, so no other connector changes behaviour.
"""
from __future__ import annotations

import pytest

from app.services.connection_service import (
    ConnectionService,
    _acount_tables_for_validation,
)
from app.services.data_source_service import DataSourceService


class CountingClient:
    """Tabular client with a cheap catalog count; introspection is the bug."""

    def __init__(self, count):
        self._count = count

    def count_tables(self):
        return self._count

    async def aget_schemas(self):
        raise AssertionError("connection test must not run full schema introspection")

    def get_tables(self):
        raise AssertionError("connection test must not run full schema introspection")


class IntrospectingClient:
    """Ordinary tabular client: no count_tables(), validated via get_schemas()."""

    def __init__(self, tables):
        self._tables = tables

    async def aget_schemas(self):
        return list(self._tables)


@pytest.mark.asyncio
async def test_count_helper_returns_none_without_count_tables():
    assert await _acount_tables_for_validation(IntrospectingClient(["a"])) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 4347])
async def test_data_source_service_counts_without_introspection(count):
    status = await DataSourceService()._avalidate_schema_access(CountingClient(count))
    assert status["success"] is True
    assert status["table_count"] == count


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 4347])
async def test_connection_service_counts_without_introspection(count):
    status = await ConnectionService()._avalidate_schema_access(CountingClient(count))
    assert status["success"] is True
    assert status["table_count"] == count


@pytest.mark.asyncio
async def test_connection_service_zero_count_still_fails_like_empty_catalog():
    """The 'no tables found' check applies to the counted path as well."""
    status = await ConnectionService()._avalidate_schema_access(CountingClient(0))
    assert status["success"] is False
    assert status["table_count"] == 0


@pytest.mark.asyncio
async def test_clients_without_count_keep_full_validation():
    tables = ["t1", "t2", "t3"]
    ds = await DataSourceService()._avalidate_schema_access(IntrospectingClient(tables))
    cs = await ConnectionService()._avalidate_schema_access(IntrospectingClient(tables))
    assert ds["success"] is True and ds["table_count"] == len(tables)
    assert cs["success"] is True and cs["table_count"] == len(tables)
