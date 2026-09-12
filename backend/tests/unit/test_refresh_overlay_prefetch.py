"""The manual-Reload overlay sync must reuse the catalog the shared refresh
just fetched with the SAME user's credentials, instead of re-crawling the
source. On tabular OBO sources (Power BI / Fabric) each crawl is a full
tenant walk — the duplicate fetch doubled every "Reload tables" click.

The sync fans out over every per-user connection on the agent, so these
exercise the per-connection catalog seam (`_construct_user_catalog_client`)
rather than the old single-client `construct_client`.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.prompt_formatters import Table, TableColumn
from app.services.data_source_service import DataSourceService


def _table(name="SalesModel/Customers"):
    return Table(
        name=name,
        columns=[TableColumn(name="id", dtype="int")],
        pks=[], fks=[], is_active=True,
        metadata_json={"powerbi": {"datasetId": "ds-1", "tableName": name.split("/")[-1]}},
    )


def _delegated_ds(conn_id="conn-uuid", ds_id="ds-uuid"):
    """An agent with exactly one delegated connection — the shape that makes
    a legacy plain-list `prefetched_tables` attributable."""
    conn = MagicMock(id=conn_id, type="powerbi", auth_policy="user_required")
    return MagicMock(id=ds_id, connections=[conn]), conn


@pytest.mark.asyncio
async def test_prefetched_tables_skip_live_fetch():
    """With prefetched_tables provided, no client is constructed and no live
    aget_schemas call happens — the overlay is built from the handed-in list."""
    svc = DataSourceService()
    svc._construct_user_catalog_client = AsyncMock(
        side_effect=AssertionError("must not re-crawl the source")
    )
    svc.construct_client = AsyncMock(side_effect=AssertionError("must not re-crawl the source"))
    svc._upsert_user_overlay = AsyncMock()

    ds, _conn = _delegated_ds()
    user = MagicMock(id="user-uuid")

    out = await svc.get_user_data_source_schema(
        db=MagicMock(), data_source=ds, user=user,
        prefetched_tables=[_table()],
    )

    svc._upsert_user_overlay.assert_awaited_once()
    normalized = svc._upsert_user_overlay.await_args.kwargs["normalized"]
    assert set(normalized.keys()) == {"SalesModel/Customers"}
    assert [t.name for t in out] == ["SalesModel/Customers"]


@pytest.mark.asyncio
async def test_prefetch_keyed_by_connection_skips_live_fetch():
    """The fan-out form: a dict keyed by connection id is reused for that
    connection without re-crawling."""
    svc = DataSourceService()
    svc._construct_user_catalog_client = AsyncMock(
        side_effect=AssertionError("must not re-crawl the source")
    )
    svc._upsert_user_overlay = AsyncMock()

    ds, conn = _delegated_ds()

    out = await svc.get_user_data_source_schema(
        db=MagicMock(), data_source=ds, user=MagicMock(id="u"),
        prefetched_tables={conn.id: [_table("Keyed/Table")]},
    )

    assert [t.name for t in out] == ["Keyed/Table"]


@pytest.mark.asyncio
async def test_no_prefetch_falls_back_to_live_fetch():
    svc = DataSourceService()
    client = MagicMock()
    client.aget_schemas = AsyncMock(return_value=[_table("Live/Table")])
    svc._construct_user_catalog_client = AsyncMock(return_value=client)
    svc._upsert_user_overlay = AsyncMock()

    ds, _conn = _delegated_ds()

    out = await svc.get_user_data_source_schema(
        db=MagicMock(), data_source=ds, user=MagicMock(id="u"),
    )

    svc._construct_user_catalog_client.assert_awaited_once()
    assert [t.name for t in out] == ["Live/Table"]


@pytest.mark.asyncio
async def test_empty_prefetch_returns_empty_without_live_fetch():
    """An empty prefetched list means the user's own crawl saw nothing —
    same handling as a live fetch returning nothing (no fallback re-crawl)."""
    svc = DataSourceService()
    svc._construct_user_catalog_client = AsyncMock(
        side_effect=AssertionError("must not re-crawl the source")
    )
    svc._upsert_user_overlay = AsyncMock()

    ds, _conn = _delegated_ds()

    out = await svc.get_user_data_source_schema(
        db=MagicMock(), data_source=ds, user=MagicMock(id="u"),
        prefetched_tables=[],
    )
    assert out == []


@pytest.mark.asyncio
async def test_no_per_user_connections_returns_empty():
    """A system_only agent has one shared catalog — nothing to sync per user."""
    svc = DataSourceService()
    svc._construct_user_catalog_client = AsyncMock(
        side_effect=AssertionError("must not re-crawl the source")
    )
    ds = MagicMock(id="ds", connections=[MagicMock(id="c", type="postgresql", auth_policy="system_only")])

    out = await svc.get_user_data_source_schema(
        db=MagicMock(), data_source=ds, user=MagicMock(id="u"),
    )
    assert out == []
