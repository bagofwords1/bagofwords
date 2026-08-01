"""Regression: concurrent table resolution must not collide on the shared session.

Production failure class (traced from a real report): the planner emitted a
parallel batch of ``create_data`` calls in one turn. Each resolves its tables
via ``schema_builder.build()``, which runs ``await self.db.execute(...)`` on the
agent's ONE long-lived SQLAlchemy ``AsyncSession``. That session is not safe for
concurrent use, so the overlapping reads raised ("another operation is in
progress"); ``_resolve_active_tables`` swallowed the exception and the tool
failed with the generic "No active tables matched" — the *same* table names that
failed in the batch succeeded when re-run serially.

These tests reproduce the race deterministically with a fake builder that mimics
the session's no-concurrent-ops rule, and prove that passing the agent's
``_tool_db_lock`` (``db_lock=``) serializes the reads so every call resolves.
"""
import asyncio

import pytest

from app.ai.tools.implementations.create_data import (
    CreateDataTool,
    _RESOLUTION_INTERNAL_ERROR_MARKER,
)


class _Info:
    def __init__(self, id):
        self.id = id


class _Tbl:
    def __init__(self, name):
        self.name = name


class _DS:
    def __init__(self, id, names):
        self.info = _Info(id)
        self.tables = [_Tbl(n) for n in names]


class _Ctx:
    def __init__(self, data_sources):
        self.data_sources = data_sources


class _Group:
    """Stand-in for a TablesBySource entry (attribute access, like the real one)."""

    def __init__(self, data_source_id, tables):
        self.data_source_id = data_source_id
        self.tables = tables


class SharedSessionSchemaBuilder:
    """Fake SchemaContextBuilder bound to a single AsyncSession.

    Reproduces the exact hazard: if two ``build()`` calls overlap — as they do
    under ``asyncio.gather`` on one session — the second raises the way
    SQLAlchemy does for concurrent operations. Serialized callers never collide.
    """

    def __init__(self, ds_id, table_names):
        self.ds_id = ds_id
        self.table_names = list(table_names)
        self._active = 0
        self.max_concurrency = 0

    async def build(self, *, with_stats=False, data_source_ids=None,
                    name_patterns=None, **kwargs):
        self._active += 1
        self.max_concurrency = max(self.max_concurrency, self._active)
        try:
            if self._active > 1:
                raise RuntimeError(
                    "another operation is in progress (concurrent AsyncSession use)"
                )
            await asyncio.sleep(0.005)  # widen the overlap window
            return _Ctx([_DS(self.ds_id, self.table_names)])
        finally:
            self._active -= 1


_TABLES = ["dbo.Reports", "dbo.Employees"]


@pytest.mark.asyncio
async def test_concurrent_resolution_races_without_lock():
    """Without the lock, the shared builder is entered concurrently and at least
    one call fails to resolve — surfacing the INTERNAL-ERROR marker rather than a
    silent, misleading name-mismatch. (Guards that the fix is actually needed.)"""
    builder = SharedSessionSchemaBuilder("ds-1", _TABLES)
    groups = [_Group("ds-1", list(_TABLES))]

    results = await asyncio.gather(*[
        CreateDataTool._resolve_active_tables(groups, builder)  # no db_lock
        for _ in range(6)
    ])

    assert builder.max_concurrency > 1, "expected the reads to overlap on one session"
    all_warnings = [w for _resolved, warns in results for w in warns]
    assert any(_RESOLUTION_INTERNAL_ERROR_MARKER in w for w in all_warnings), (
        "a raised resolution must be tagged as an internal error, not swallowed "
        "into a plain 'no active tables matched'"
    )
    assert any(len(resolved) == 0 for resolved, _warns in results), (
        "at least one concurrent resolution should have failed"
    )


@pytest.mark.asyncio
async def test_concurrent_resolution_serialized_with_lock():
    """With the agent's _tool_db_lock, build() runs strictly serially, so every
    concurrent call resolves its tables and none hits the internal-error path."""
    builder = SharedSessionSchemaBuilder("ds-1", _TABLES)
    groups = [_Group("ds-1", list(_TABLES))]
    lock = asyncio.Lock()

    results = await asyncio.gather(*[
        CreateDataTool._resolve_active_tables(groups, builder, db_lock=lock)
        for _ in range(6)
    ])

    assert builder.max_concurrency == 1, "the lock must keep the session reads serial"
    for resolved, warns in results:
        assert not any(_RESOLUTION_INTERNAL_ERROR_MARKER in w for w in warns)
        assert resolved and resolved[0]["tables"] == _TABLES


@pytest.mark.asyncio
async def test_no_lock_single_call_still_resolves():
    """Sanity: db_lock is optional — a lone call (tests / direct callers with no
    shared session) resolves exactly as before."""
    builder = SharedSessionSchemaBuilder("ds-1", _TABLES)
    groups = [_Group("ds-1", list(_TABLES))]

    resolved, warns = await CreateDataTool._resolve_active_tables(groups, builder)

    assert resolved == [{"data_source_id": "ds-1", "tables": _TABLES}]
    assert warns == []
