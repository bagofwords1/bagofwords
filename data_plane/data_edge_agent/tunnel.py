"""NATS transport for the data edge agent.

Phase 1 scope: connect, subscribe to this agent's subjects, and log what
arrives. Dispatch to real clients is a later phase — until then every request
gets an explicit "not implemented" reply rather than silence, so a caller using
`nc.request()` sees a clear error instead of a timeout it has to interpret.

Subjects follow docs/design/secure-data-tunnel-v4-nats.md (A3):

    tunnel.<org_id>.<edge_agent_id>.conn.<connection_name>
    tunnel.<org_id>.<edge_agent_id>.control
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from nats.aio.client import Client as NATSClient
from nats.aio.msg import Msg

from . import __version__
from .audit import AuditLog
from .config import AgentConfig, ConnectionConfig
from .data_sources.registry import construct_client

logger = logging.getLogger(__name__)

# JSON-RPC: -32601 is "method not found". Phase 1 answers every operation with
# it, which is honest — the operation exists in the protocol, not yet here.
_NOT_IMPLEMENTED = -32601
_RPC_ERROR = -32000
# A query that exceeded its budget. The control plane translates this back into
# QueryTimeoutError so the codegen retry loop behaves as in direct mode (B3).
_QUERY_TIMEOUT = -32001
# The subject named a connection this agent does not serve. Distinct from
# "method not found": the operation is fine, the target is not, and the control
# plane must not confuse the two (an unknown connection is a registration
# mismatch; an unimplemented operation is a version mismatch).
_UNKNOWN_CONNECTION = -32002
# An in-flight operation was cancelled through the control subject (A9/C3).
_CANCELLED = -32003

# Operations the edge agent executes against the real client. Others still get
# a JSON-RPC "not implemented" so a caller sees an error, not a timeout.
_SUPPORTED_OPERATIONS = frozenset({
    "get_schemas", "get_schema", "test_connection", "prompt_schema", "execute_query",
})


class _NotImplemented(Exception):
    """Raised for an operation the agent does not execute; becomes a JSON-RPC
    'not implemented' rather than a generic error."""


class _QueryTimeout(Exception):
    """execute_query exceeded its budget; carries the budget and SQL so the
    control plane can rebuild QueryTimeoutError."""

    def __init__(self, timeout_s: float, sql: str | None):
        super().__init__(f"query exceeded {timeout_s}s")
        self.timeout_s = timeout_s
        self.sql = sql


class _UnknownConnection(Exception):
    """The request addressed a connection this agent does not serve."""

    def __init__(self, connection_name: str):
        super().__init__(f"unknown connection {connection_name!r}")
        self.connection_name = connection_name


class _Cancelled(Exception):
    """The operation was cancelled via the control subject before it finished."""

# Shutdown must complete even if the transport will not cooperate.
_SHUTDOWN_TIMEOUT = 5.0
_RECONNECT_SECONDS = 5.0
_CONNECT_TIMEOUT = 5.0


class EdgeAgentTunnel:
    """Owns the agent's NATS connection and its subscriptions."""

    def __init__(self, config: AgentConfig, audit: Optional[AuditLog] = None) -> None:
        self._config = config
        # Local audit trail (C4/C5). Defaults to an in-memory-only log so the
        # tunnel is usable without one; main wires a file-backed one.
        self._audit = audit if audit is not None else AuditLog(None)
        self._nc: Optional[NATSClient] = None
        self._subscriptions: list[Any] = []
        # Per-connection subscription, keyed by name, so the admin UI can
        # unsubscribe exactly one connection when it is deleted (C4) without
        # tearing down the rest.
        self._conn_subs: dict[str, Any] = {}
        # Real data-source clients, one per connection (system credentials).
        # Building a client opens a pool, so it is cached and reused.
        self._clients: dict[str, Any] = {}
        # In-flight cancellable operations, keyed by request id: a callable that
        # cancels the running statement (A9/C3).
        self._in_flight: dict[Any, Any] = {}

    @property
    def is_connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected

    async def connect(self, stop: Optional[asyncio.Event] = None) -> None:
        """Open the outbound connection to NATS, retrying until it succeeds.

        The retry loop is ours rather than nats-py's, and that is deliberate.
        Letting the library retry internally means the only way to abort a
        shutdown-while-the-broker-is-down is to cancel `connect()` mid-flight —
        which leaves a half-initialised client whose transport session has no
        owner, and whose `close()` then blocks on tasks that will never finish.
        Retrying here means we are never inside a connect we have to cancel:
        between attempts there is simply nothing to clean up.

        `stop`, when given, ends the loop cleanly instead of raising.
        """
        cfg = self._config
        attempt = 0

        # Built once (a cert/key load is file I/O; re-reading it on every
        # reconnect is wasted work) and before the loop so a missing client
        # cert fails fast here rather than silently retrying. mTLS is the only
        # auth path, so this must succeed for the agent to connect at all.
        ssl_ctx = cfg.tls_context()

        while True:
            attempt += 1
            logger.info(
                "edge_agent.nats.connecting",
                extra={
                    "url": cfg.tunnel_endpoint_url,
                    "edge_agent_id": cfg.edge_agent_id,
                    "attempt": attempt,
                },
            )
            client = NATSClient()
            try:
                await client.connect(
                    servers=[cfg.tunnel_endpoint_url],
                    # mTLS is the sole authentication: the client cert in this
                    # context is what the broker maps to a scoped user (A11).
                    # The endpoint must be TLS-bearing (tls:// or wss://) for the
                    # cert to be exchanged in the handshake.
                    tls=ssl_ctx,
                    reconnect_time_wait=_RECONNECT_SECONDS,
                    # Bounded on purpose. With -1 the library retries the
                    # *initial* connect internally and never returns, so this
                    # loop never regains control and a SIGTERM issued while the
                    # broker is down is simply never observed. (Cancelling the
                    # task instead leaves a half-built client that leaks its
                    # transport session; closing it from outside does not abort
                    # the retry either — both were measured.) Fail fast here,
                    # then restore infinite reconnection below.
                    max_reconnect_attempts=1,
                    connect_timeout=_CONNECT_TIMEOUT,
                    error_cb=self._on_error,
                    disconnected_cb=self._on_disconnected,
                    reconnected_cb=self._on_reconnected,
                    closed_cb=self._on_closed,
                )
            except Exception as e:
                await _close_quietly(client)
                logger.error(
                    "edge_agent.nats.connect_failed",
                    extra={
                        "url": cfg.tunnel_endpoint_url,
                        "attempt": attempt,
                        "retry_in": _RECONNECT_SECONDS,
                        "error": str(e),
                    },
                )
                if stop is not None and await _sleep_or_stop(stop, _RECONNECT_SECONDS):
                    logger.info("edge_agent.nats.connect_abandoned")
                    return
                if stop is None:
                    await asyncio.sleep(_RECONNECT_SECONDS)
                continue

            # Now that we are connected, hand reconnection back to the library
            # and let it retry forever: an unattended agent must survive a
            # broker restart without anyone logging in. `options` is read at
            # reconnect time, not cached at connect time.
            client.options["max_reconnect_attempts"] = -1

            self._nc = client
            logger.info(
                "edge_agent.nats.connected",
                extra={
                    "url": cfg.tunnel_endpoint_url,
                    "max_payload": client.max_payload,
                    "edge_agent_id": cfg.edge_agent_id,
                },
            )
            return

    async def subscribe(self) -> None:
        """Subscribe to one subject per served connection, plus control.

        Where the broker scopes the credential to `tunnel.<org>.<edge_agent_id>.>`
        a permissions violation surfaces here rather than at connect, so a wrong
        org_id or edge_agent_id fails loudly instead of quietly serving nobody.
        A bare token carries no such scoping (see A11): under token auth this
        subscribe succeeds whatever the ids say, and a misconfigured agent goes
        undetected until it serves nothing.
        """
        cfg = self._config
        if self._nc is None:
            raise RuntimeError("connect() must be called before subscribe()")

        for conn in cfg.connections:
            subject = cfg.connection_subject(conn.name)
            sub = await self._nc.subscribe(
                subject,
                cb=self._make_request_handler(conn.name),
            )
            self._subscriptions.append(sub)
            self._conn_subs[conn.name] = sub
            logger.info(
                "edge_agent.nats.subscribed",
                extra={"subject": subject, "connection": conn.name, "type": conn.type},
            )

        sub = await self._nc.subscribe(cfg.control_subject, cb=self._handle_control)
        self._subscriptions.append(sub)
        logger.info("edge_agent.nats.subscribed", extra={"subject": cfg.control_subject})

        if not cfg.connections:
            logger.warning(
                "edge_agent.no_connections",
                extra={"hint": "config lists no connections; only the control subject is served"},
            )

    def _make_request_handler(self, connection_name: str):
        async def handler(msg: Msg) -> None:
            await self._handle_request(msg, connection_name)

        return handler

    async def _handle_request(self, msg: Msg, connection_name: str) -> None:
        """Log the request, then answer it.

        Nothing here may raise: an exception inside a NATS callback is swallowed
        by the client library, and the caller learns nothing until its request
        times out. Every path ends in a reply or an explicit log.
        """
        request_id: Any = None
        operation = "<unparsed>"
        started = time.monotonic()
        # Filled by _dispatch (e.g. row_count) so the audit line can report it.
        audit_meta: dict[str, Any] = {}
        sql: Optional[str] = None
        try:
            payload = json.loads(msg.data)
            request_id = payload.get("id")
            params = payload.get("params") or {}
            operation = params.get("operation", "<missing>")
            if operation == "execute_query":
                sql = (params.get("kwargs") or {}).get("sql")

            logger.info(
                "edge_agent.request.received",
                extra={
                    "subject": msg.subject,
                    "connection": connection_name,
                    "operation": operation,
                    "request_id": request_id,
                    "bytes": len(msg.data),
                },
            )
            logger.debug(
                "edge_agent.request.body",
                # kwargs can carry SQL; user_credentials must never be logged,
                # so the body goes to DEBUG and credentials are stripped.
                extra={"params": _redact(params)},
            )
            # The JSON-RPC id lives on the envelope, not in params: it is what
            # the control plane's cancel names (A9), so it is what the
            # in-flight registry is keyed by.
            result = await self._dispatch(
                connection_name, operation, params, audit_meta, request_id=request_id
            )
        except json.JSONDecodeError as e:
            logger.warning(
                "edge_agent.request.malformed",
                extra={"subject": msg.subject, "error": str(e), "bytes": len(msg.data)},
            )
            self._audit_record(connection_name, operation, started, "error",
                               request_id, error="malformed request")
            await self._respond_error(msg, request_id, operation, "malformed request")
            return
        except _NotImplemented:
            self._audit_record(connection_name, operation, started, "not_implemented",
                               request_id)
            await self._respond_not_implemented(msg, request_id, operation)
            return
        except _QueryTimeout as e:
            self._audit_record(connection_name, operation, started, "timeout",
                               request_id, error=str(e), sql=sql)
            await self._respond_error(
                msg, request_id, operation, str(e),
                code=_QUERY_TIMEOUT,
                data={"kind": "query_timeout", "timeout_s": e.timeout_s, "sql": e.sql},
            )
            return
        except _UnknownConnection as e:
            self._audit_record(connection_name, operation, started, "error",
                               request_id, error=str(e))
            await self._respond_error(
                msg, request_id, operation, str(e),
                code=_UNKNOWN_CONNECTION,
                data={"kind": "unknown_connection", "connection": e.connection_name},
            )
            return
        except _Cancelled:
            self._audit_record(connection_name, operation, started, "cancelled",
                               request_id, sql=sql)
            await self._respond_error(
                msg, request_id, operation, "cancelled by control plane",
                code=_CANCELLED, data={"kind": "cancelled"},
            )
            return
        except Exception as e:  # any failure becomes a JSON-RPC error, never a timeout
            logger.warning(
                "edge_agent.request.failed",
                extra={"connection": connection_name, "operation": operation, "error": str(e)},
            )
            self._audit_record(connection_name, operation, started, "error",
                               request_id, error=str(e), sql=sql)
            await self._respond_error(msg, request_id, operation, str(e))
            return

        self._audit_record(connection_name, operation, started, "ok", request_id,
                           row_count=audit_meta.get("row_count"), sql=sql)
        await self._respond_result(msg, request_id, operation, result)

    def _audit_record(self, connection: str, operation: str, started: float,
                      outcome: str, request_id: Any, *, row_count: Optional[int] = None,
                      error: Optional[str] = None, sql: Optional[str] = None) -> None:
        self._audit.record(
            connection=connection,
            operation=operation,
            outcome=outcome,
            duration_ms=int((time.monotonic() - started) * 1000),
            row_count=row_count,
            request_id=request_id,
            error=error,
            sql=sql,
        )

    # -- operation dispatch --------------------------------------------------

    def _get_client(self, connection_name: str, user_credentials: dict | None):
        """Build (and cache) the real data-source client for a connection.

        System-credential clients are cached — constructing one opens a pool.
        Per-user credentials are ephemeral: never cached, never stored.
        """
        conn = self.get_connection(connection_name)
        if conn is None:
            raise _UnknownConnection(connection_name)

        if user_credentials:
            return construct_client(conn.type, {**conn.config, **user_credentials})

        client = self._clients.get(connection_name)
        if client is None:
            client = construct_client(conn.type, conn.client_params())
            self._clients[connection_name] = client
        return client

    def _budget(self, conn: ConnectionConfig, operation: str, asked_ms) -> float:
        """Seconds the operation may take (A5/C3).

        get_schemas gets the agent's index budget; everything else the
        connection's own query budget, falling back to the agent default — the
        same number the advertisement publishes, so the control plane sizes its
        wait against what is actually enforced. The caller's timeout_ms may
        lower the budget, never raise it.
        """
        cfg = self._config
        if operation in ("get_schemas", "warm_all"):
            effective = cfg.index_timeout_seconds
        else:
            effective = conn.effective_query_timeout(cfg.default_query_timeout_seconds)
        return min(asked_ms / 1000, effective) if asked_ms else effective

    async def _dispatch(self, connection_name: str, operation: str, params: dict,
                        audit_meta: Optional[dict] = None, request_id: Any = None):
        """Run one operation against the real client and return its result value.

        Blocking work (connect, query, schema crawl) goes through the client's
        async wrappers, which use asyncio.to_thread — never on this loop, which
        also serves the control subject and NATS keepalives.

        `audit_meta`, when given, is filled with countable outcomes (row_count)
        for the audit trail — it is an out-parameter, not an input.

        `request_id` is the JSON-RPC envelope id. While the operation runs it is
        registered in `_in_flight` with a cancel callable, so a cancel on the
        control subject naming that id (A9/C3) stops the operation: the task is
        cancelled and, for execute_query, the statement is cancelled on the
        source as well.
        """
        if audit_meta is None:
            audit_meta = {}
        if operation not in _SUPPORTED_OPERATIONS:
            raise _NotImplemented(operation)

        conn = self.get_connection(connection_name)
        if conn is None:
            raise _UnknownConnection(connection_name)

        client = await asyncio.to_thread(
            self._get_client, connection_name, params.get("user_credentials")
        )
        budget = self._budget(conn, operation, params.get("timeout_ms"))

        # Source-side cancel hook, filled by execute_query once it holds a raw
        # connection; other operations only have the task to cancel.
        cancel_box: dict = {}
        task = asyncio.ensure_future(
            self._run_operation(client, operation, params, budget, audit_meta, cancel_box)
        )
        cancelled_by_control = False

        def _cancel() -> None:
            nonlocal cancelled_by_control
            cancelled_by_control = True
            _fire(cancel_box)
            task.cancel()

        if request_id is not None:
            self._in_flight[request_id] = _cancel
        try:
            return await task
        except asyncio.CancelledError:
            if cancelled_by_control:
                raise _Cancelled(operation)
            raise  # the handler itself is being torn down; propagate
        finally:
            if request_id is not None:
                self._in_flight.pop(request_id, None)

    async def _run_operation(self, client, operation: str, params: dict, budget: float,
                             audit_meta: dict, cancel_box: dict):
        kwargs = params.get("kwargs") or {}

        if operation == "get_schemas":
            tables = await asyncio.wait_for(client.aget_schemas(), budget)
            audit_meta["row_count"] = len(tables)  # tables discovered
            # Shape matches TunneledClient._invoke_streaming: {value, index_stats}.
            return {
                "value": [t.model_dump() for t in tables],
                "index_stats": {},
            }
        if operation == "get_schema":
            tables = await asyncio.wait_for(client.aget_schemas(), budget)
            audit_meta["row_count"] = len(tables)
            return [t.model_dump() for t in tables]
        if operation == "test_connection":
            return await asyncio.wait_for(client.atest_connection(), budget)
        if operation == "prompt_schema":
            return await asyncio.wait_for(client.aprompt_schema(), budget)
        if operation == "execute_query":
            import base64 as _b64
            import io as _io
            sql = kwargs.get("sql")
            qkwargs = {k: v for k, v in kwargs.items() if k != "sql"}

            def _on_connect(raw):
                # psycopg2 connection.cancel() is thread-safe: callable from the
                # loop thread while the query runs in the worker thread.
                cancel_box["cancel"] = raw.cancel

            # `_on_connect` is the source-side statement-cancel hook (A9/C3).
            # Only the edge agent's own clients implement it; a reused backend
            # client (MysqlClient, …) has no such parameter and would reject it.
            # When it's unsupported we still get cancel-on-timeout by abandoning
            # the wait below — we just can't stop the statement on the source.
            import inspect as _inspect
            try:
                _params = _inspect.signature(client.execute_query).parameters
                _accepts_on_connect = "_on_connect" in _params or any(
                    p.kind is _inspect.Parameter.VAR_KEYWORD for p in _params.values()
                )
            except (TypeError, ValueError):
                _accepts_on_connect = False

            def _run():
                if _accepts_on_connect:
                    return client.execute_query(sql, _on_connect=_on_connect, **qkwargs)
                return client.execute_query(sql, **qkwargs)

            try:
                df = await asyncio.wait_for(asyncio.to_thread(_run), budget)
            except asyncio.TimeoutError:
                _fire(cancel_box)  # abandon the wait AND stop the statement on the source
                raise _QueryTimeout(budget, sql)
            audit_meta["row_count"] = len(df)  # rows returned
            buf = _io.BytesIO()
            df.to_parquet(buf, index=False)
            return {"dataframe_b64": _b64.b64encode(buf.getvalue()).decode()}
        raise _NotImplemented(operation)  # unreachable: guarded above

    async def _respond_result(self, msg: Msg, request_id: Any, operation: str, result) -> None:
        if not msg.reply:
            return
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "edge_agent_id": self._config.edge_agent_id,
            "result": result,
        }
        try:
            await msg.respond(json.dumps(body).encode())
            logger.info(
                "edge_agent.response.sent",
                extra={"operation": operation, "request_id": request_id},
            )
        except Exception as e:  # pragma: no cover
            logger.error("edge_agent.response.failed", extra={"error": str(e)})

    async def _respond_error(self, msg: Msg, request_id: Any, operation: str, message: str,
                             code: int = _RPC_ERROR, data: dict | None = None) -> None:
        if not msg.reply:
            return
        err_data = {"operation": operation}
        if data:
            err_data.update(data)
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "edge_agent_id": self._config.edge_agent_id,
            "error": {"code": code, "message": message, "data": err_data},
        }
        try:
            await msg.respond(json.dumps(body).encode())
        except Exception as e:  # pragma: no cover
            logger.error("edge_agent.response.failed", extra={"error": str(e)})

    async def _handle_control(self, msg: Msg) -> None:
        try:
            cmd = json.loads(msg.data)
            logger.info(
                "edge_agent.control.received",
                extra={
                    "subject": msg.subject,
                    "method": cmd.get("method"),
                    "params": cmd.get("params"),
                },
            )
            if cmd.get("method") == "cancel":
                ref_id = (cmd.get("params") or {}).get("ref_id")
                cancel = self._in_flight.get(ref_id)
                if cancel is not None:
                    cancel()
                    logger.info("edge_agent.control.cancelled", extra={"ref_id": ref_id})
                else:
                    logger.info("edge_agent.control.cancel_not_found",
                                extra={"ref_id": ref_id, "in_flight": len(self._in_flight)})
        except json.JSONDecodeError as e:
            logger.warning(
                "edge_agent.control.malformed",
                extra={"subject": msg.subject, "error": str(e)},
            )

    async def _respond_not_implemented(
        self, msg: Msg, request_id: Any, operation: str
    ) -> None:
        """Reply so a `nc.request()` caller gets an error rather than a timeout.

        `reply` is empty when the message was published fire-and-forget, in
        which case there is nothing to answer.
        """
        if not msg.reply:
            return

        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "edge_agent_id": self._config.edge_agent_id,
            "error": {
                "code": _NOT_IMPLEMENTED,
                "message": f"operation {operation!r} is not implemented by this edge agent",
                "data": {"operation": operation,
                         "supported": sorted(_SUPPORTED_OPERATIONS)},
            },
        }
        try:
            await msg.respond(json.dumps(body).encode())
        except Exception as e:  # pragma: no cover - transport-level failure
            logger.error(
                "edge_agent.response.failed",
                extra={"subject": msg.subject, "error": str(e)},
            )

    # -- advertisement -------------------------------------------------------

    def build_advertisement(self) -> dict:
        """The payload the control plane registers this agent from.

        Deliberately assembled field by field. Bow creates Connection rows from
        this, so anything that leaks in becomes a row in someone else's
        database — and the one thing this process exists to withhold is exactly
        what a careless `model_dump()` would include.
        """
        cfg = self._config
        return {
            "edge_agent_id": cfg.edge_agent_id,
            "edge_agent_name": cfg.edge_agent_name,
            "version": __version__,
            "index_timeout_seconds": cfg.index_timeout_seconds,
            "connections": [
                c.advertised(
                    cfg.default_query_timeout_seconds,
                    catalog_identity_available=self._catalog_identity_available(c),
                )
                for c in cfg.connections
            ],
        }

    def _catalog_identity_available(self, conn: ConnectionConfig) -> bool:
        """Ask the connection's client whether it can crawl a catalog.

        Uses the cached system-credential client (constructing one is lazy —
        no connection is opened until an operation runs). A client that cannot
        even be constructed is reported as True: the control plane's default,
        and the advertisement must still go out so the misconfiguration shows
        up in Bow rather than silently hiding the connection.
        """
        try:
            client = self._get_client(conn.name, None)
        except Exception as e:
            logger.debug(
                "edge_agent.advertise.client_unavailable",
                extra={"connection": conn.name, "error": str(e)},
            )
            return True
        return bool(getattr(client, "catalog_identity_available", True))

    async def advertise(self) -> None:
        """Publish the advertisement.

        Note there is no `org_id` field. Tenancy is carried by the *subject* —
        `tunnel.<org_id>.advertisements` — because core NATS gives a subscriber
        no publisher identity, so a payload field would be something any agent
        could forge. The broker refuses a subject outside this agent's grant;
        it cannot refuse a JSON key.
        """
        if self._nc is None:
            logger.warning("edge_agent.advertise.skipped", extra={"reason": "not connected"})
            return

        payload = self.build_advertisement()
        subject = self._config.advertisement_subject
        try:
            await self._nc.publish(subject, json.dumps(payload).encode())
            await self._nc.flush()
        except Exception as e:
            logger.error(
                "edge_agent.advertise.failed",
                extra={"subject": subject, "error": str(e)},
            )
            return

        logger.info(
            "edge_agent.advertised",
            extra={
                "subject": subject,
                "connections": [c["name"] for c in payload["connections"]],
            },
        )

    async def advertise_forever(self, stop: asyncio.Event) -> None:
        """Re-publish on a timer until asked to stop.

        A single advertisement is lost if the control plane happens to be
        restarting when it lands, and re-publishing only on NATS reconnect
        would miss that case entirely — the agent's connection is fine, it is
        Bow that went away. Repeating turns registration into a converging
        state sync instead of a one-shot event.
        """
        interval = self._config.advertise_interval_seconds
        while not await _sleep_or_stop(stop, interval):
            await self.advertise()

    async def heartbeat(self) -> None:
        """Publish a liveness ping. Fire-and-forget: a missed one just delays
        the control plane's next status update, healed by the following tick."""
        if self._nc is None:
            return
        subject = self._config.heartbeat_subject
        payload = {"edge_agent_id": self._config.edge_agent_id, "version": __version__}
        try:
            await self._nc.publish(subject, json.dumps(payload).encode())
        except Exception as e:
            logger.debug("edge_agent.heartbeat.failed", extra={"error": str(e)})

    async def heartbeat_forever(self, stop: asyncio.Event) -> None:
        """Publish a heartbeat on a timer until asked to stop."""
        interval = self._config.heartbeat_interval_seconds
        while not await _sleep_or_stop(stop, interval):
            await self.heartbeat()

    # -- admin-UI connection lifecycle (design C4) ---------------------------

    @property
    def audit(self) -> AuditLog:
        return self._audit

    def get_connection(self, name: str) -> Optional[ConnectionConfig]:
        return next((c for c in self._config.connections if c.name == name), None)

    def list_connections(self) -> list[ConnectionConfig]:
        return list(self._config.connections)

    async def apply_connection(self, conn: ConnectionConfig) -> None:
        """Add or replace a served connection at runtime, then re-advertise.

        Called by the admin UI after a save. Subject membership follows the
        connection name: a new name gets its own subscription; editing an
        existing one keeps the subject and just drops the cached client so the
        next request rebuilds it with the new config/credentials.
        """
        existing = self.get_connection(conn.name)
        if existing is not None:
            self._config.connections.remove(existing)
        self._config.connections.append(conn)

        # Any pooled client for this name is now stale (host/creds may differ).
        self._clients.pop(conn.name, None)

        if self._nc is not None and conn.name not in self._conn_subs:
            subject = self._config.connection_subject(conn.name)
            sub = await self._nc.subscribe(
                subject, cb=self._make_request_handler(conn.name)
            )
            self._subscriptions.append(sub)
            self._conn_subs[conn.name] = sub
            logger.info(
                "edge_agent.nats.subscribed",
                extra={"subject": subject, "connection": conn.name, "type": conn.type},
            )
        logger.info(
            "edge_agent.connection.applied",
            extra={"connection": conn.name, "type": conn.type,
                   "action": "updated" if existing is not None else "added"},
        )
        await self.advertise()

    async def remove_connection(self, name: str) -> bool:
        """Stop serving a connection at runtime, then re-advertise its absence.

        The control plane deactivates a connection it stops seeing in the
        advertisement (register_advertisement withdraws it), so re-advertising
        after the drop is what tells Bow the source is gone.
        """
        existing = self.get_connection(name)
        if existing is None:
            return False
        self._config.connections.remove(existing)
        self._clients.pop(name, None)

        sub = self._conn_subs.pop(name, None)
        if sub is not None:
            self._subscriptions = [s for s in self._subscriptions if s is not sub]
            try:
                await sub.unsubscribe()
            except Exception as e:  # pragma: no cover - best effort
                logger.debug("edge_agent.unsubscribe.failed",
                             extra={"connection": name, "error": str(e)})
        logger.info("edge_agent.connection.removed", extra={"connection": name})
        await self.advertise()
        return True

    async def test_connection_config(self, conn: ConnectionConfig) -> dict[str, Any]:
        """Build a throwaway client and run its test_connection off the loop.

        Used by the admin UI's Test button, including for a connection that has
        not been saved yet, so it takes a ConnectionConfig rather than a name
        and never touches the client cache.
        """
        def _run() -> dict[str, Any]:
            client = construct_client(conn.type, conn.client_params())
            return client.test_connection()

        try:
            return await asyncio.to_thread(_run)
        except Exception as e:
            return {"success": False, "message": str(e)}

    def status_snapshot(self) -> dict[str, Any]:
        """A point-in-time view of the agent for the admin dashboard."""
        cfg = self._config
        return {
            "edge_agent_id": cfg.edge_agent_id,
            "edge_agent_name": cfg.edge_agent_name,
            "org_id": cfg.org_id,
            "version": __version__,
            "nats": {
                "connected": self.is_connected,
                "url": cfg.tunnel_endpoint_url,
            },
            "connections_served": len(cfg.connections),
            "in_flight": len(self._in_flight),
            "advertise_interval_seconds": cfg.advertise_interval_seconds,
            "heartbeat_interval_seconds": cfg.heartbeat_interval_seconds,
        }

    async def close(self) -> None:
        """Drain if connected, close either way.

        Called on two paths: normal shutdown, and shutdown while `connect()`
        was still retrying. In the second the client exists but was never
        connected, so there is nothing to drain and everything to close —
        skipping it leaks the transport's session.
        """
        if self._nc is None:
            return
        try:
            # Bounded, because neither path is guaranteed to finish. drain()
            # waits on in-flight replies, and close() on a client whose connect
            # was cancelled mid-retry waits on tasks that will never complete.
            # Cleanup that can block termination is worse than an unclean exit.
            if self._nc.is_connected:
                await asyncio.wait_for(self._nc.drain(), timeout=_SHUTDOWN_TIMEOUT)
                logger.info("edge_agent.nats.drained")
            else:
                await asyncio.wait_for(self._nc.close(), timeout=_SHUTDOWN_TIMEOUT)
                logger.debug("edge_agent.nats.closed_unconnected")
        except asyncio.TimeoutError:
            logger.warning(
                "edge_agent.nats.close_timeout",
                extra={"seconds": _SHUTDOWN_TIMEOUT, "was_connected": self._nc.is_connected},
            )
        except Exception as e:  # pragma: no cover
            logger.warning("edge_agent.nats.close_failed", extra={"error": str(e)})
        finally:
            self._nc = None
            self._subscriptions.clear()
            self._conn_subs.clear()

    # -- connection lifecycle callbacks --------------------------------------

    async def _on_error(self, e: Exception) -> None:
        logger.error("edge_agent.nats.error", extra={"error": str(e)})

    async def _on_disconnected(self) -> None:
        logger.warning("edge_agent.nats.disconnected")

    async def _on_reconnected(self) -> None:
        # nats-py restores the subscriptions itself, so only the advertisement
        # has to be resent (A10: "on reconnect the edge agent re-subscribes and
        # re-advertises"). Doing it now rather than waiting for the next timer
        # tick matters because a reconnect is exactly when the control plane may
        # have just marked this agent stale: re-advertising immediately
        # re-registers it instead of leaving it dropped for up to
        # advertise_interval_seconds. advertise() logs and swallows its own
        # failures, so this never breaks the reconnect callback.
        logger.info("edge_agent.nats.reconnected")
        await self.advertise()

    async def _on_closed(self) -> None:
        logger.info("edge_agent.nats.closed")


def _fire(cancel_box: dict) -> None:
    """Run the source-side cancel hook, if the operation registered one."""
    fn = cancel_box.get("cancel")
    if fn:
        try:
            fn()
        except Exception:
            pass


def _redact(params: dict) -> dict:
    """Strip per-user credentials before anything reaches a log line."""
    if "user_credentials" not in params:
        return params
    return {**params, "user_credentials": "<redacted>"}


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Wait `seconds`, or return True early if `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def _close_quietly(client: NATSClient) -> None:
    """Release a client that never finished connecting.

    `close()` alone does not free the websocket transport's aiohttp session on
    a client whose connect failed — it stays open and surfaces later as
    "Unclosed client session". One per failed attempt, and an unattended agent
    retrying through a long outage attempts every few seconds, so this is an
    accumulating leak rather than a cosmetic warning. Reach for the transport
    directly when the public path leaves one behind.
    """
    try:
        await asyncio.wait_for(client.close(), timeout=_SHUTDOWN_TIMEOUT)
    except Exception:
        pass

    # WebSocketTransport builds an aiohttp.ClientSession in its constructor and
    # only closes it in wait_closed(), which a failed connect never reaches.
    session = getattr(getattr(client, "_transport", None), "_client", None)
    if session is None or getattr(session, "closed", True):
        return
    try:
        await asyncio.wait_for(session.close(), timeout=_SHUTDOWN_TIMEOUT)
    except Exception:
        pass
