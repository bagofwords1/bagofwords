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

# Inbox prefix root. The worker's reply subjects are scoped per-org beneath it —
# "_INBOX.bow.<org_id>" — so the worker subscribes only to its own org's inbox
# (grant "_INBOX.bow.<org_id>.>"), not the shared "_INBOX.bow.>". That closes a
# cross-tenant leak: several single-org workers on one NATS account would each
# otherwise receive every other's replies (design A11). The edge agent needs no
# inbox grant — it replies via allow_responses to whatever reply-to it received.
_INBOX_PREFIX_ROOT = "_INBOX.bow"

# Org-scoped subscriptions: the worker connection is per-org — its NATS grant is
# tunnel.<org>.> — so it subscribes only to its own org's advertisements and
# heartbeats, never a cross-org wildcard (a global subscribe would be denied by
# the broker). `{org}` is filled from the connection's org_id. The org is still
# read back off the subject on receipt (A10); the change is only in what the
# worker is allowed to, and does, subscribe to.
_ADVERTISEMENT_SUBJECT = "tunnel.{org}.advertisements"
# Per-agent liveness: tunnel.<org>.<edge_agent_id>.heartbeat (A10).
_HEARTBEAT_SUBJECT = "tunnel.{org}.*.heartbeat"

# Leave room for the JSON envelope around a request when checking it against the
# broker's max_payload (design A6/B3).
_ENVELOPE_HEADROOM = 4096


def _build_tls_context(tls_ca=None, tls_cert=None, tls_key=None, tls_verify=True):
    """Build the mTLS SSLContext for the worker's NATS connection (design A11).

    mTLS is the only supported authentication, so a client certificate is
    mandatory: `tls_cert` and `tls_key` must both be set or this raises. A
    private CA path makes that CA the only trust anchor; an empty `tls_ca` falls
    back to the system roots. `tls_verify=False` disables server verification
    (local experiments only). Returns an `ssl.SSLContext` with the client cert
    loaded — pass it as `tls=` to `nats.connect`.
    """
    if not (tls_cert and tls_key):
        raise ValueError(
            "tunnel authentication is mTLS-only: set NATS_TLS_CERT and "
            "NATS_TLS_KEY (paths to the worker's client certificate and its key)"
        )
    import ssl

    if tls_verify:
        ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=tls_ca or None)
    else:
        ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
    return ctx


class TunnelClient:
    """NATS request/reply transport. One per worker process."""

    def __init__(self) -> None:
        self._nc: Optional[NATSClient] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._org_id: Optional[str] = None
        self._ad_sub = None
        self._hb_sub = None

    @property
    def is_connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected

    async def connect(self, nats_url: str, *, org_id: str, tls_ca=None,
                      tls_cert=None, tls_key=None, tls_verify=True) -> None:
        """Open the shared connection.

        The connection is per-org (`org_id`): its NATS identity is scoped to
        tunnel.<org_id>.>, so the listeners subscribe only within that org.

        Authentication is mTLS only (design A11): the worker presents the client
        certificate in `tls_cert`/`tls_key`, which the broker maps to a scoped
        user (verify_and_map). Both are required — building the context raises
        otherwise, so a misconfigured worker fails fast here rather than
        connecting unauthenticated. `nats_url` must be TLS-bearing (tls://…:4222)
        for the certificate to be exchanged in the handshake, and its host must
        match a name in the broker's server cert (SAN) for verification to pass.

        Captures the running loop in the same statement that creates the
        connection, so `self._loop` and the connection's owning loop cannot
        diverge (B3).
        """
        ssl_ctx = _build_tls_context(tls_ca, tls_cert, tls_key, tls_verify)
        self._org_id = org_id
        self._loop = asyncio.get_running_loop()
        # Per-org inbox: reply subjects live under _INBOX.bow.<org_id>, matching
        # the worker's org-scoped grant so replies from other orgs never reach it.
        inbox_prefix = f"{_INBOX_PREFIX_ROOT}.{org_id}"
        self._nc = await nats.connect(
            nats_url,
            tls=ssl_ctx,
            inbox_prefix=inbox_prefix,
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
        subject = _ADVERTISEMENT_SUBJECT.format(org=self._org_id)
        self._ad_sub = await self._nc.subscribe(subject, cb=self._on_advertisement)
        logger.info("tunnel.advertisement.listening", extra={"subject": subject})

    async def start_heartbeat_listener(self) -> None:
        """Subscribe to agent heartbeats and record liveness. Leader-gated by
        the caller (a plain subscribe fans to every worker; B4)."""
        if self._nc is None:
            raise RuntimeError("connect() must be called before start_heartbeat_listener()")
        subject = _HEARTBEAT_SUBJECT.format(org=self._org_id)
        self._hb_sub = await self._nc.subscribe(subject, cb=self._on_heartbeat)
        logger.info("tunnel.heartbeat.listening", extra={"subject": subject})

    async def _on_heartbeat(self, msg: Msg) -> None:
        # tunnel.<org_id>.<edge_agent_id>.heartbeat
        parts = msg.subject.split(".")
        if len(parts) < 4:
            return
        org_id, edge_agent_id = parts[1], parts[2]
        try:
            from app.services.tunnel_registration_service import record_heartbeat
            await record_heartbeat(org_id, edge_agent_id)
        except Exception:
            logger.debug("tunnel.heartbeat.record_failed", exc_info=True)

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

        watcher = None
        if cancel_check is not None:
            watcher = asyncio.create_task(self._watch_cancel(base, ref_id, cancel_check))
        try:
            return await self.invoke(
                connection, operation, kwargs, ref_id=ref_id, timeout=timeout,
                user_credentials=user_credentials,
            )
        finally:
            if watcher is not None:
                watcher.cancel()
            if sub is not None:
                await sub.unsubscribe()

    async def _watch_cancel(self, base, ref_id, cancel_check, interval=1.0):
        """Bridge a local cancel_check callable to a remote cancel (A9).

        cancel_check is a plain callable on this side (indexing passes a
        threading.Event's is_set); the edge agent cannot read it. Poll it and
        publish one cancel to the control subject when it first returns true —
        without this the UI reports cancelled while the operation runs to
        completion on the source.
        """
        while True:
            await asyncio.sleep(interval)
            try:
                fired = cancel_check()
            except Exception:
                return
            if fired:
                try:
                    await self._nc.publish(
                        f"{base}.control",
                        json.dumps({"method": "cancel",
                                    "params": {"ref_id": ref_id}}).encode(),
                    )
                except Exception:
                    logger.debug("tunnel.cancel.publish_failed", exc_info=True)
                return

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
            self._hb_sub = None

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
