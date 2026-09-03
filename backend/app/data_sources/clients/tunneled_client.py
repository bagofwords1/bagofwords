"""TunneledClient — proxies data-source client operations to an edge agent over
NATS instead of opening a local connection (design B1).

Holds no credentials. Constructed by the client-construction sites (B2) when
`Connection.tunnel_mode` is True. This slice implements the schema-discovery and
query path (get_schemas / test_connection / execute_query); other operations
raise until proxied.
"""
from __future__ import annotations

import asyncio
import logging

from app.ai.prompt_formatters import Table
from app.data_sources.clients.base import DataSourceClient
from app.services.tunnel_errors import TunnelPayloadTooLarge

logger = logging.getLogger(__name__)

# Fallbacks for the two budgets when the advertisement has not populated
# Connection.config yet (design B1).
TUNNEL_FALLBACK_TIMEOUT_SECONDS = 300
TUNNEL_FALLBACK_INDEX_SECONDS = 900


class TunneledClient(DataSourceClient):
    def __init__(self, connection, client_class, tunnel, user_credentials=None):
        self._connection = connection
        self._client_class = client_class      # real class — for capabilities
        self._tunnel = tunnel                  # TunnelClient (B3)
        self._user_credentials = user_credentials

        # Attribute surface the rest of the codebase reads off clients.
        self._bow_connection = connection
        cfg = connection.config or {}
        self._bow_connection_query_timeout = cfg.get("query_timeout_seconds")
        self._query_budget = self._bow_connection_query_timeout or TUNNEL_FALLBACK_TIMEOUT_SECONDS
        self._index_budget = cfg.get("index_timeout_seconds", TUNNEL_FALLBACK_INDEX_SECONDS)
        self._last_index_stats: dict = {}

    # -- attribute surface ---------------------------------------------------

    @property
    def capabilities(self):
        return getattr(self._client_class, "capabilities", set())

    @property
    def description(self):
        # The real description is an instance property that needs host/port we
        # deliberately do not have. A connection-level summary is enough for the
        # coder context; the source's own schema text arrives via prompt_schema.
        c = self._connection
        return (
            f"Tunneled {c.type} connection '{c.name}', served by edge agent "
            f"'{c.edge_agent_id}'."
        )

    @property
    def catalog_identity_available(self) -> bool:
        return (self._connection.config or {}).get("catalog_identity_available", True)

    def index_stats(self) -> dict:
        # Never a round trip: populated from the last get_schemas/warm_all reply,
        # which is exactly when the numbers are produced (design B1).
        return dict(self._last_index_stats)

    # -- loop bridge ---------------------------------------------------------
    #
    # The NATS connection lives on the loop TunnelClient captured at connect
    # (the uvicorn main loop). These methods are reached from other places — a
    # pool thread with no loop (execute_query), the indexing runner's own loop
    # (aget_schemas) — so every call is marshalled onto the tunnel's loop.

    async def _await_on_tunnel_loop(self, coro):
        tloop = getattr(self._tunnel, "_loop", None)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if tloop is None or running is tloop:
            return await coro
        fut = asyncio.run_coroutine_threadsafe(coro, tloop)
        return await asyncio.wrap_future(fut)

    def _run_on_tunnel_loop(self, coro, timeout: float):
        """Block the calling (worker) thread until the tunnel loop finishes."""
        tloop = getattr(self._tunnel, "_loop", None)
        if tloop is None:
            raise RuntimeError("tunnel is not connected")
        fut = asyncio.run_coroutine_threadsafe(coro, tloop)
        return fut.result(timeout + 10)

    def _invoke(self, operation, kwargs, timeout):
        return self._tunnel.invoke(
            self._connection, operation, kwargs,
            timeout=timeout, user_credentials=self._user_credentials,
        )

    @staticmethod
    def _tables(result):
        """Deserialize a get_schemas reply ({value, index_stats}) into Tables."""
        rows = result.get("value") if isinstance(result, dict) else (result or [])
        tables = []
        for d in rows or []:
            try:
                tables.append(Table.model_validate(d) if isinstance(d, dict) else d)
            except Exception:
                logger.warning("tunnel.get_schemas.bad_table", exc_info=True)
        return tables

    # -- async surface: what the schema-discovery path uses ------------------

    async def aget_schemas(self, progress_callback=None, prior_catalog=None,
                           prior_tables=None):
        kwargs = {"prior_catalog": prior_catalog, "prior_tables": prior_tables}
        try:
            result = await self._await_on_tunnel_loop(
                self._tunnel.invoke_streaming(
                    self._connection, "get_schemas", kwargs,
                    timeout=self._index_budget, progress_callback=progress_callback,
                    user_credentials=self._user_credentials,
                )
            )
        except TunnelPayloadTooLarge:
            # Prior state is an optimization, not the request (design A6/B1):
            # drop it and re-extract fully rather than fail the index.
            logger.warning("tunnel.get_schemas.prior_state_too_large",
                           extra={"connection": self._connection.name})
            result = await self._await_on_tunnel_loop(
                self._tunnel.invoke_streaming(
                    self._connection, "get_schemas", {},
                    timeout=self._index_budget, progress_callback=progress_callback,
                    user_credentials=self._user_credentials,
                )
            )
        if isinstance(result, dict):
            self._last_index_stats = result.get("index_stats") or {}
        return self._tables(result)

    async def atest_connection(self):
        return await self._await_on_tunnel_loop(self._invoke("test_connection", {}, 60.0))

    async def aexecute_query(self, sql=None, **kwargs):
        payload = {"sql": sql, **{k: v for k, v in kwargs.items() if v is not None}}
        return await self._await_on_tunnel_loop(
            self._invoke("execute_query", payload, self._query_budget))

    # -- sync abstract surface (ABC requires these to instantiate) -----------
    #
    # Reached via base's asyncio.to_thread wrappers or from the sandbox pool
    # thread; each blocks its worker thread on the tunnel loop.

    def test_connection(self):
        return self._run_on_tunnel_loop(self._invoke("test_connection", {}, 60.0), 60.0)

    def get_schemas(self):
        result = self._run_on_tunnel_loop(
            self._tunnel.invoke_streaming(
                self._connection, "get_schemas", {},
                timeout=self._index_budget, user_credentials=self._user_credentials,
            ),
            self._index_budget,
        )
        if isinstance(result, dict):
            self._last_index_stats = result.get("index_stats") or {}
        return self._tables(result)

    def get_schema(self, table_name):
        return self._run_on_tunnel_loop(
            self._invoke("get_schema", {"table_name": table_name}, self._query_budget),
            self._query_budget,
        )

    def prompt_schema(self):
        return self._run_on_tunnel_loop(
            self._invoke("prompt_schema", {}, self._query_budget), self._query_budget)

    def execute_query(self, sql=None, **kwargs):
        payload = {"sql": sql, **{k: v for k, v in kwargs.items() if v is not None}}
        return self._run_on_tunnel_loop(
            self._invoke("execute_query", payload, self._query_budget), self._query_budget)
