"""The per-user overlay sync must crawl EVERY connection on the agent.

`_upsert_user_overlay` reconciles across the whole data source and revokes any
table the snapshot omits. The sync used to build its snapshot from
`construct_client`, which is documented "a single client for the first
connection" — so on an agent with several connections it saw only
`connections[0]`'s tables and revoked all the others. They vanished from the
tables selector and from the LLM's schema context, and only an agent-level
Reload (a separate path that already unions every connection) brought them
back — until the next plain page load quietly broke it again.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.prompt_formatters import Table, TableColumn
from app.services.data_source_service import DataSourceService


def _table(name):
    return Table(
        name=name,
        columns=[TableColumn(name="id", dtype="int")],
        pks=[], fks=[], is_active=True,
    )


def _conn(cid, ctype="postgresql", auth_policy="user_required"):
    return SimpleNamespace(id=cid, type=ctype, auth_policy=auth_policy, config={})


def _svc_with_connections(conns):
    """A service whose overlay write is captured, plus the matching data source."""
    svc = DataSourceService()
    svc._upsert_user_overlay = AsyncMock()
    # Must never be reached on the multi-connection path — it is the
    # single-connection helper whose use caused the bug.
    svc.construct_client = AsyncMock(
        side_effect=AssertionError("multi-connection sync must not use the first-connection client")
    )
    ds = MagicMock(id="ds-uuid")
    ds.connections = conns
    return svc, ds


def _client_returning(tables):
    client = MagicMock()
    client.aget_schemas = AsyncMock(return_value=tables)
    return client


@pytest.mark.asyncio
async def test_snapshot_spans_every_connection():
    """The regression: with three connections the snapshot must contain all of
    their tables, not just the first connection's."""
    conns = [_conn("c1"), _conn("c2"), _conn("c3")]
    svc, ds = _svc_with_connections(conns)

    by_conn = {
        "c1": [_table("pbi/Sales")],
        "c2": [_table("pg/customers"), _table("pg/orders")],
        "c3": [_table("sf/Account")],
    }

    async def _construct(db, conn, user):
        return _client_returning(by_conn[conn.id])

    with patch("app.services.connection_service.ConnectionService") as CS:
        CS.return_value.construct_client = AsyncMock(side_effect=_construct)
        out = await svc.get_user_data_source_schema(
            db=MagicMock(), data_source=ds, user=MagicMock(id="u"),
        )

    normalized = svc._upsert_user_overlay.await_args.kwargs["normalized"]
    assert set(normalized) == {"pbi/Sales", "pg/customers", "pg/orders", "sf/Account"}
    # Every connection was crawled exactly once.
    assert CS.return_value.construct_client.await_count == 3
    assert {t.name for t in out} == set(normalized)


@pytest.mark.asyncio
async def test_single_connection_still_uses_the_data_source_client():
    """One connection keeps the original call path, so the data-source-scoped
    credential override (keyed by data source, not connection) still applies."""
    svc = DataSourceService()
    svc._upsert_user_overlay = AsyncMock()
    svc.construct_client = AsyncMock(return_value=_client_returning([_table("Live/Table")]))
    ds = MagicMock(id="ds")
    ds.connections = [_conn("only")]

    out = await svc.get_user_data_source_schema(
        db=MagicMock(), data_source=ds, user=MagicMock(id="u"),
    )

    svc.construct_client.assert_awaited_once()
    assert [t.name for t in out] == ["Live/Table"]


@pytest.mark.asyncio
async def test_one_failing_connection_aborts_instead_of_revoking():
    """A partial snapshot is indistinguishable from "the user lost access", and
    would revoke the very tables the failure means we know nothing about. So a
    failing connection must abort the sync, leaving the overlay untouched."""
    conns = [_conn("c1"), _conn("c2")]
    svc, ds = _svc_with_connections(conns)

    async def _construct(db, conn, user):
        if conn.id == "c2":
            raise RuntimeError("upstream timeout")
        return _client_returning([_table("pbi/Sales")])

    with patch("app.services.connection_service.ConnectionService") as CS:
        CS.return_value.construct_client = AsyncMock(side_effect=_construct)
        with pytest.raises(RuntimeError, match="upstream timeout"):
            await svc.get_user_data_source_schema(
                db=MagicMock(), data_source=ds, user=MagicMock(id="u"),
            )

    svc._upsert_user_overlay.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_connections_are_not_crawled_for_schema():
    """MCP / Custom API connections carry a tool list, not a schema — crawling
    them reaches an aget_schemas that does not exist."""
    conns = [_conn("c1"), _conn("mcp1", ctype="mcp"), _conn("c2")]
    svc, ds = _svc_with_connections(conns)

    crawled = []

    async def _construct(db, conn, user):
        crawled.append(conn.id)
        return _client_returning([_table(f"{conn.id}/t")])

    with patch("app.services.connection_service.ConnectionService") as CS:
        CS.return_value.construct_client = AsyncMock(side_effect=_construct)
        await svc.get_user_data_source_schema(
            db=MagicMock(), data_source=ds, user=MagicMock(id="u"),
        )

    assert crawled == ["c1", "c2"]


@pytest.mark.asyncio
async def test_prefetched_snapshot_still_skips_all_crawling():
    """Reload already unions every connection and hands the result in; that must
    keep bypassing the live crawl entirely."""
    conns = [_conn("c1"), _conn("c2")]
    svc, ds = _svc_with_connections(conns)

    with patch("app.services.connection_service.ConnectionService") as CS:
        CS.return_value.construct_client = AsyncMock(
            side_effect=AssertionError("must not re-crawl when prefetched")
        )
        out = await svc.get_user_data_source_schema(
            db=MagicMock(), data_source=ds, user=MagicMock(id="u"),
            prefetched_tables=[_table("pbi/Sales"), _table("pg/customers")],
        )

    assert {t.name for t in out} == {"pbi/Sales", "pg/customers"}
