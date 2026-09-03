"""Control-plane NATS transport for the secure data tunnel.

`TunnelClient` owns the single NATS connection a worker uses to reach edge
agents (design B3/B4): one per worker process, shared by everything in that
worker. This phase implements the connection and the advertisement listener
that logs what each edge agent serves; creating Connection rows from those
advertisements (A10) is a later phase.

The connection lives on the loop that created it — captured in the same
statement as the connection itself, so the owning loop can never disagree with
the loop a caller later targets (B3, "Loop ownership").
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import uuid
from typing import Optional

import nats
from nats.aio.client import Client as NATSClient
from nats.aio.msg import Msg

from app.services.tunnel_errors import (
    RemoteError,
    TunnelNotConnectedError,
    TunnelOwnershipError,
    TunnelPayloadTooLarge,
    translate_remote_error,
)

logger = logging.getLogger(__name__)

_RECONNECT_SECONDS = 2
_DRAIN_TIMEOUT = 10

# The inbox prefix must be "_INBOX.bow" so an edge agent's NATS grant can allow
# replies to it without opening the whole default "_INBOX.>" space (design A11).
_INBOX_PREFIX = "_INBOX.bow"

# Advertisements from every org land here. The org is read from the subject,
# never the payload, because core NATS gives a subscriber no publisher identity
# and the broker only attests the subject (design A10).
_ADVERTISEMENT_SUBJECT = "tunnel.*.advertisements"

# Leave room for the JSON envelope around a request when checking it against the
# broker's max_payload (design A6/B3).
_ENVELOPE_HEADROOM = 4096


class TunnelClient:
    """NATS request/reply transport. One per worker process."""

    def __init__(self) -> None:
        self._nc: Optional[NATSClient] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ad_sub = None

    @property
    def is_connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected

    async def connect(self, nats_url: str, token: str = "") -> None:
        """Open the shared connection.

        Captures the running loop in the same statement that creates the
        connection, so `self._loop` and the connection's owning loop cannot
        diverge (B3).
        """
        self._loop = asyncio.get_running_loop()
        self._nc = await nats.connect(
            nats_url,
            token=token or None,
            inbox_prefix=_INBOX_PREFIX,
            reconnect_time_wait=_RECONNECT_SECONDS,
            max_reconnect_attempts=-1,  # control plane retries forever
            error_cb=self._on_error,
            disconnected_cb=self._on_disconnected,
            reconnected_cb=self._on_reconnected,
            closed_cb=self._on_closed,
        )
        logger.info(
            "tunnel.nats.connected",
            extra={"url": nats_url, "max_payload": self._nc.max_payload},
        )

    async def start_advertisement_listener(self) -> None:
        """Subscribe to advertisements and log what each edge agent serves.

        Leader-gated by the caller: a plain NATS subscribe is fanned out to
        every worker, so registration must run in exactly one of them (B4).
        """
        if self._nc is None:
            raise RuntimeError("connect() must be called before start_advertisement_listener()")
        self._ad_sub = await self._nc.subscribe(
            _ADVERTISEMENT_SUBJECT, cb=self._on_advertisement
        )
        logger.info("tunnel.advertisement.listening", extra={"subject": _ADVERTISEMENT_SUBJECT})

    async def _on_advertisement(self, msg: Msg) -> None:
        # org_id is the second subject token: tunnel.<org_id>.advertisements.
        parts = msg.subject.split(".")
        org_id = parts[1] if len(parts) >= 3 else "<unknown>"

        try:
            payload = json.loads(msg.data.decode())
        except (ValueError, UnicodeDecodeError) as e:
            logger.warning(
                "tunnel.advertisement.unparseable",
                extra={"subject": msg.subject, "error": str(e)},
            )
            return

        connections = payload.get("connections", []) or []
        edge_agent_id = payload.get("edge_agent_id", "<unknown>")
        edge_agent_name = payload.get("edge_agent_name")

        logger.info(
            "tunnel.advertisement.received",
            extra={
                "org_id": org_id,
                "edge_agent_id": edge_agent_id,
                "edge_agent_name": edge_agent_name,
                "connection_count": len(connections),
            },
        )
        for conn in connections:
            # Keys must avoid reserved LogRecord attributes ("name", "type" is
            # fine but "name" is not): logging raises if extra shadows one.
            logger.info(
                "tunnel.advertisement.data_source",
                extra={
                    "org_id": org_id,
                    "edge_agent_id": edge_agent_id,
                    "connection_name": conn.get("name"),
                    "connection_type": conn.get("type"),
                    "connection_label": conn.get("label"),
                },
            )

        # Persist (design A10/D2). Runs only in the leader worker, so no
        # cross-worker race. Its own session — the handler runs on a background
        # loop, not inside a request — and failures must not kill the listener.
        try:
            from app.services.tunnel_registration_service import register_advertisement
            await register_advertisement(org_id, payload)
        except Exception:
            logger.exception(
                "tunnel.advertisement.persist_failed",
                extra={"org_id": org_id, "edge_agent_id": edge_agent_id},
            )

    # -- request/reply transport (design B3) ---------------------------------

    def _conn_subject(self, connection) -> str:
        return (
            f"tunnel.{connection.organization_id}."
            f"{connection.edge_agent_id}.conn.{connection.name}"
        )

    async def invoke(self, connection, operation, kwargs, *, ref_id=None,
                     timeout=60.0, user_credentials=None):
        """One request/reply to the edge agent for `connection`.

        Runs on the loop that owns the NATS connection. Callers on another loop
        (the sandbox pool thread, the indexing runner) must bridge to
        `self._loop` via run_coroutine_threadsafe — TunneledClient does this.
        """
        if self._nc is None:
            raise TunnelNotConnectedError("tunnel is not connected")

        payload = {
            "jsonrpc": "2.0",
            "id": ref_id or str(uuid.uuid4()),
            "method": "invoke",
            "params": {
                "connection_name": connection.name,
                "operation": operation,
                "kwargs": kwargs,
                "timeout_ms": int(timeout * 1000),
            },
        }
        if user_credentials:
            payload["params"]["user_credentials"] = user_credentials

        encoded = json.dumps(payload).encode()
        if len(encoded) > self._nc.max_payload - _ENVELOPE_HEADROOM:
            raise TunnelPayloadTooLarge(len(encoded), self._nc.max_payload, operation)

        # The control-plane wait is a backstop above the edge agent's own budget
        # (design C3): it fires only when the agent is unreachable.
        msg = await self._nc.request(self._conn_subject(connection), encoded,
                                     timeout=timeout + 5)
        return self._unwrap(msg, connection, operation)

    async def invoke_streaming(self, connection, operation, kwargs, *, timeout,
                               progress_callback=None, cancel_check=None,
                               user_credentials=None):
        """Long operations (get_schemas, warm_all): progress flows back on a
        per-request subject; the final reply is the return value."""
        ref_id = str(uuid.uuid4())
        base = f"tunnel.{connection.organization_id}.{connection.edge_agent_id}"

        sub = None
        if progress_callback is not None:
            # Subscribe before publishing so the first notifications don't race.
            async def _on_progress(msg: Msg) -> None:
                try:
                    params = json.loads(msg.data).get("params", {})
                    progress_callback(**params)
                except Exception:  # a bad progress frame must not kill the op
                    logger.debug("tunnel.progress.bad_frame", exc_info=True)

            sub = await self._nc.subscribe(f"{base}.progress.{ref_id}", cb=_on_progress)
        try:
            return await self.invoke(
                connection, operation, kwargs, ref_id=ref_id, timeout=timeout,
                user_credentials=user_credentials,
            )
        finally:
            if sub is not None:
                await sub.unsubscribe()

    def _unwrap(self, msg: Msg, connection, operation):
        body = json.loads(msg.data)

        # A3 runtime guard: the responder must be the expected owner.
        if body.get("edge_agent_id") != connection.edge_agent_id:
            raise TunnelOwnershipError(
                f"{connection.name} answered by {body.get('edge_agent_id')!r}, "
                f"expected {connection.edge_agent_id!r}"
            )
        if "error" in body:
            raise translate_remote_error(body["error"], operation)

        result = body.get("result")
        if isinstance(result, dict) and "dataframe_b64" in result:
            import pandas as pd  # local import: keep pandas off the hot import path
            return pd.read_parquet(io.BytesIO(base64.b64decode(result["dataframe_b64"])))
        return result

    async def drain(self) -> None:
        """Finish in-flight replies, then close. Safe to call unconnected."""
        if self._nc is None:
            return
        try:
            if self._nc.is_connected:
                await asyncio.wait_for(self._nc.drain(), timeout=_DRAIN_TIMEOUT)
                logger.info("tunnel.nats.drained")
            else:
                await asyncio.wait_for(self._nc.close(), timeout=_DRAIN_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("tunnel.nats.drain_timeout", extra={"seconds": _DRAIN_TIMEOUT})
        except Exception as e:  # pragma: no cover
            logger.warning("tunnel.nats.drain_failed", extra={"error": str(e)})
        finally:
            self._nc = None
            self._ad_sub = None

    async def _on_error(self, e: Exception) -> None:
        logger.error("tunnel.nats.error", extra={"error": str(e)})

    async def _on_disconnected(self) -> None:
        logger.warning("tunnel.nats.disconnected")

    async def _on_reconnected(self) -> None:
        logger.info("tunnel.nats.reconnected")

    async def _on_closed(self) -> None:
        logger.info("tunnel.nats.closed")


# Module accessor (design B4): construction sites have no route to app.state, so
# the single per-worker client is reached through here. Returns None before
# startup completes, which callers treat as "tunnel not connected".
_tunnel_client: Optional[TunnelClient] = None


def set_tunnel_client(client: Optional[TunnelClient]) -> None:
    global _tunnel_client
    _tunnel_client = client


def get_tunnel_client() -> Optional[TunnelClient]:
    return _tunnel_client
