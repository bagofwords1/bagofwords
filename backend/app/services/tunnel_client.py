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
import json
import logging
from typing import Optional

import nats
from nats.aio.client import Client as NATSClient
from nats.aio.msg import Msg

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
