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
from typing import Any, Optional

from nats.aio.client import Client as NATSClient
from nats.aio.msg import Msg

from . import __version__
from .config import AgentConfig

logger = logging.getLogger(__name__)

# JSON-RPC: -32601 is "method not found". Phase 1 answers every operation with
# it, which is honest — the operation exists in the protocol, not yet here.
_NOT_IMPLEMENTED = -32601

# Shutdown must complete even if the transport will not cooperate.
_SHUTDOWN_TIMEOUT = 5.0
_RECONNECT_SECONDS = 5.0
_CONNECT_TIMEOUT = 5.0


class EdgeAgentTunnel:
    """Owns the agent's NATS connection and its subscriptions."""

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._nc: Optional[NATSClient] = None
        self._subscriptions: list[Any] = []

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

        while True:
            attempt += 1
            logger.info(
                "edge_agent.nats.connecting",
                extra={
                    "url": cfg.nats_url,
                    "edge_agent_id": cfg.edge_agent_id,
                    "attempt": attempt,
                },
            )
            client = NATSClient()
            try:
                await client.connect(
                    servers=[cfg.nats_url],
                    token=cfg.nats_token,
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
                        "url": cfg.nats_url,
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
                    "url": cfg.nats_url,
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
        try:
            payload = json.loads(msg.data)
            request_id = payload.get("id")
            params = payload.get("params") or {}
            operation = params.get("operation", "<missing>")

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
        except json.JSONDecodeError as e:
            logger.warning(
                "edge_agent.request.malformed",
                extra={"subject": msg.subject, "error": str(e), "bytes": len(msg.data)},
            )

        await self._respond_not_implemented(msg, request_id, operation)

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
                "message": (
                    f"operation {operation!r} is not implemented yet "
                    "(data edge agent phase 1: transport only)"
                ),
                "data": {"operation": operation, "phase": 1},
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
                c.advertised(cfg.default_query_timeout_seconds)
                for c in cfg.connections
            ],
        }

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
